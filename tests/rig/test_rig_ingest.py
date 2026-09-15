"""Integration over RTSP from the virtual camera rig (TESTING.md §4).

Every fault here has a corresponding expected behaviour (ARCHITECTURE.md §7),
and the invariant behind all of them: **a gap is always recorded**.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime, timedelta

import pytest
from argus.common.config import IngestConfig, ReconnectConfig
from argus.ingest.streams import RTSPSource

from conftest import make_door_camera

pytestmark = [pytest.mark.rig, pytest.mark.postgres]


async def _collect(q, n: int, timeout: float = 30.0) -> list:
    frames = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(frames) < n and loop.time() < deadline:
        try:
            frames.append(q.get_nowait())
        except asyncio.QueueEmpty:
            await asyncio.sleep(0.05)
    return frames


async def _wait_cause(src: RTSPSource, cause: str, timeout: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if src.status.open_cause == cause:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"cause {cause!r} not observed; status={src.status.as_line()}")


async def _wait_gaps_closed(store, camera_id: str, timeout: float = 15.0) -> None:
    from argus.store.store import Store

    assert isinstance(store, Store)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        gaps = await store.list_gaps(camera_id)
        if gaps and all(g.to_utc is not None for g in gaps):
            return
        await asyncio.sleep(0.2)
    gaps = await store.list_gaps(camera_id)
    raise AssertionError(f"gaps not closed: {gaps}")


async def test_frames_flow_and_timestamps(source, rig):
    task = asyncio.create_task(source.run())
    try:
        assert await source.wait_until_up(timeout=30), source.status.as_line()
        q = source.subscribe()
        frames = await _collect(q, 45)
        assert len(frames) == 45
        ts = [f.ts_server for f in frames]
        assert ts == sorted(ts), "server timestamps must be monotonic"
        assert all(f.image.ndim == 3 and f.image.shape[2] == 3 for f in frames)
        assert all(f.ts_media is not None for f in frames)
        assert source.status.frames >= 45
    finally:
        source.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_stream_drop_records_gap_and_recovers(source, store, rig):
    task = asyncio.create_task(source.run())
    try:
        assert await source.wait_until_up(timeout=30)
        frames_before = source.status.frames
        rig.send(rig.find("canteen_door_01"), signal.SIGKILL)
        await _wait_cause(source, "offline")
        gaps_open = await store.list_gaps("canteen_door_01")
        assert len(gaps_open) == 1 and gaps_open[0].to_utc is None

        rig.spawn(rig.find("canteen_door_01"))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 45
        while loop.time() < deadline and source.status.frames <= frames_before:
            await asyncio.sleep(0.2)
        assert source.status.frames > frames_before, "no frames after recovery"
        await _wait_gaps_closed(store, "canteen_door_01")

        gaps = await store.list_gaps("canteen_door_01")
        assert len(gaps) == 1
        assert gaps[0].to_utc is not None, "recovered gap must close"
        assert gaps[0].from_utc <= gaps[0].to_utc
    finally:
        source.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_stall_detected_not_hung(source, store, rig):
    """SIGSTOP = connection open, no frames: the WiFi failure mode (TESTING.md §4)."""
    task = asyncio.create_task(source.run())
    try:
        assert await source.wait_until_up(timeout=30)
        rig.send(rig.find("canteen_door_01"), signal.SIGSTOP)
        await _wait_cause(source, "stall", timeout=20)
        gaps = await store.list_gaps("canteen_door_01")
        assert any(g.cause == "stall" and g.to_utc is None for g in gaps)

        rig.send(rig.find("canteen_door_01"), signal.SIGCONT)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 45
        while loop.time() < deadline and source.status.open_cause is not None:
            await asyncio.sleep(0.2)
        assert source.status.open_cause is None, "gap should close after resume"
        await _wait_gaps_closed(store, "canteen_door_01")
        gaps = await store.list_gaps("canteen_door_01")
        assert all(g.cause == "stall" for g in gaps)
    finally:
        rig.send(rig.find("canteen_door_01"), signal.SIGCONT)  # never leave it paused
        source.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_clip_extraction_plays(source, rig):
    from argus.ingest.clip import ClipStore

    task = asyncio.create_task(source.run())
    try:
        assert await source.wait_until_up(timeout=30)
        q = source.subscribe()
        await _collect(q, 60)  # fill the ring buffer with real frames
        start = datetime.now(UTC) - timedelta(seconds=3)
        end = datetime.now(UTC) + timedelta(milliseconds=500)
        clips = ClipStore("var/clips")
        path = await clips.extract(source, start, end, timeout_s=8)
        assert path.exists() and path.suffix == ".mp4"
        assert path.stat().st_size > 10_000

        import av

        container = await asyncio.to_thread(av.open, str(path))
        try:
            stream = container.streams.video[0]
            n = sum(1 for _ in container.decode(stream))
            duration = float(stream.duration * stream.time_base) if stream.duration else 0.0
        finally:
            container.close()
        assert n >= 40, f"clip too short: {n} frames"
        assert duration >= 1.0, f"clip duration {duration}s"
    finally:
        source.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_never_connected_camera_surfaces_loudly(store, rig):
    """No frames ever: nothing to bound a gap from — must surface, not record
    a bogus interval (THREAT_MODEL.md: 'we saw nothing' vs 'nothing happened')."""
    cam = make_door_camera(camera_id="ghost_camera", uri="rtsp://localhost:8554/nonexistent")
    await store.upsert_camera(cam)
    cfg = IngestConfig(
        stall_timeout_s=2.0,
        reconnect=ReconnectConfig(initial_s=0.2, max_s=1.0, jitter=0.1),
    )
    src = RTSPSource(cam, cfg, gaps=store)
    task = asyncio.create_task(src.run())
    try:
        await asyncio.sleep(6)
        assert src.status.open_cause == "offline"
        assert src.status.frames == 0
        assert await store.list_gaps("ghost_camera") == []
    finally:
        src.stop()
        await asyncio.wait_for(task, timeout=15)
