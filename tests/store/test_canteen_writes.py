"""The canteen pipeline against a real store: events, clips, and the link.

The rig replay exercises this over RTSP, which is slow and needs a served
stream. This drives the same writer with a fake clip extractor, so the part that
actually matters here -- that a doorway event, a clip row and the reference
between them all land, in that order -- is checked on every commit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from argus.backends.mock import MockDiskDetector
from argus.common.clock import FixedClock
from argus.common.config import CameraConfig, CanteenConfig
from argus.pipelines.canteen import CanteenPipeline
from argus.pipelines.runtime import SessionRuntime

pytestmark = pytest.mark.postgres

T0 = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)
WALK = [200.0 + 20.0 * i for i in range(13)]


class _Frame:
    def __init__(self, ts: datetime, image: np.ndarray) -> None:
        self.ts_server = ts
        self.ts_media = None
        self.image = image


class _Source:
    def __init__(self, camera: CameraConfig) -> None:
        self.camera = camera

    def subscribe(self):  # pragma: no cover - handle() is driven directly
        raise AssertionError("not used")

    def unsubscribe(self, q) -> None:  # pragma: no cover
        raise AssertionError("not used")


class _Clips:
    """Writes a real file, so the row and the bytes agree."""

    def __init__(self, root: Path, *, fail: bool = False) -> None:
        self.root = root
        self.fail = fail

    async def extract(self, source, start_utc, end_utc, *, timeout_s: float = 10.0) -> Path:
        if self.fail:
            raise RuntimeError("no packets for that range")
        path = self.root / source.camera.camera_id / f"{start_utc.timestamp():.0f}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)
        return path


def _camera() -> CameraConfig:
    return CameraConfig(
        camera_id="canteen_door_01",
        role="canteen_door",
        space_id="canteen",
        door_id="c1",
        source_uri="rtsp://localhost:8554/canteen_door_01",
        is_virtual=True,
        door_line=(0.5, 0.0, 0.5, 1.0),
        inside_sign=1,
    )


def _frame(x: float, ts: datetime) -> _Frame:
    image = np.full((360, 640, 3), 44, dtype=np.uint8)
    ys, xs = np.ogrid[:360, :640]
    image[(xs - int(x)) ** 2 + (ys - 200) ** 2 <= 16**2] = 190
    return _Frame(ts, image)


async def _walk(store, tmp_path: Path, *, fail_clips: bool = False):
    camera = _camera()
    await store.upsert_camera(camera)
    runtime: SessionRuntime = SessionRuntime("detector", MockDiskDetector, max_queue=4)
    pipeline = CanteenPipeline(
        camera,
        _Source(camera),
        store,
        detector=runtime,
        clips=_Clips(tmp_path, fail=fail_clips),
        cfg=CanteenConfig(band=0.03, min_samples_per_side=2),
        clock=FixedClock(T0),
    )
    try:
        written = []
        for index, x in enumerate(WALK):
            written.extend(await pipeline.handle(_frame(x, T0 + timedelta(seconds=index * 0.1))))
        return written
    finally:
        await runtime.aclose()


async def test_a_crossing_writes_an_event_a_clip_row_and_the_link(store, tmp_path) -> None:
    written = await _walk(store, tmp_path)
    assert [e.direction for e in written] == ["enter"]

    events = await store.list_events("canteen_door_01")
    assert len(events) == 1
    event = events[0]
    assert event.direction == "enter"
    assert event.person_id is None, "faces are off, so unknown is the correct outcome"

    clips = await store.db.fetch_all(
        "select rel_path, camera_id, is_virtual, bytes, keyframe_utc <= start_utc from clip"
    )
    assert len(clips) == 1
    rel_path, camera_id, is_virtual, size, keyframe_first = clips[0]
    assert rel_path == event.clip_ref, "the event points at the clip row's path"
    assert camera_id == "canteen_door_01"
    assert is_virtual is True, "rig footage must stay refusable by an export"
    assert size == 2048
    assert keyframe_first is True
    assert (tmp_path / rel_path).is_file()


async def test_a_failed_clip_still_writes_the_event_and_registers_nothing(store, tmp_path) -> None:
    """ADR-0008 from both sides: the day is flagged because the evidence exists
    and the clip does not, so the event must be there and the clip row must not."""
    written = await _walk(store, tmp_path, fail_clips=True)
    assert [e.direction for e in written] == ["enter"]
    assert "missing_clip" in written[0].flags

    events = await store.list_events("canteen_door_01")
    assert len(events) == 1 and events[0].clip_ref is None
    assert await store.db.fetch_all("select count(*) from clip") == [(0,)]


async def test_the_stored_clip_path_is_relative_and_survives_the_schema_check(
    store, tmp_path
) -> None:
    """An absolute path would be unusable on another machine, and the schema
    refuses one -- so if this ever regresses it fails here, loudly."""
    await _walk(store, tmp_path)
    rows = await store.db.fetch_all("select rel_path from clip")
    assert rows and not rows[0][0].startswith("/")
    assert ".." not in rows[0][0]
