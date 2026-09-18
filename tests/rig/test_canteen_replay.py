"""The canteen pipeline against the rig's labelled ground truth.

This is the highest-value test available without cameras: it runs the whole
chain -- RTSP, decode, detect, track, cross, clip, write -- against footage whose
crossings are known, and compares. What it proves is the logic. It proves
nothing about accuracy: the rig draws people as bright disks and the mock
detector finds bright disks, so any number from here describes the rig
(ARCHITECTURE.md §9.2).

Alignment note. The rig publishes with `-stream_loop -1`, so a consumer joins at
an arbitrary point in the loop and wall-clock time cannot be compared with the
manifest's media times directly. Detected crossings are folded into the loop and
a single offset is solved for; `ts_media` is used for that alignment only, never
in an assertion about a number someone would be paid by.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from argus.backends.mock import MockDiskDetector
from argus.common.clock import SystemClock
from argus.common.config import CameraConfig, CanteenConfig, IngestConfig, ReconnectConfig
from argus.ingest.clip import ClipStore
from argus.ingest.streams import RTSPSource
from argus.payroll import DoorEvent, Gap, PairingPolicy, run_pairing
from argus.pipelines.canteen import CanteenPipeline
from argus.pipelines.metrics import InMemoryMetrics
from argus.pipelines.runtime import SessionRuntime

pytestmark = [pytest.mark.rig, pytest.mark.postgres]

ROOT = Path(__file__).resolve().parents[2]
STREAM = "canteen_door_01"
ANALYSIS_FPS = 15.0
# One full 60 s loop plus a margin, so every labelled crossing is in the window.
# The manifest has ten per loop.
REPLAY_S = 66.0
# Nine of the ten are expected. The missing one is the second of a pair that
# cross 1.05 s apart: the mock detector merges two blobs that close into one, so
# they become one track and one crossing. That is a limit of the mock, not of
# the geometry -- and tailgating at real crowd density is on the list of things
# ARCHITECTURE.md §9.2 says cannot be validated without real footage.
MIN_MATCHED = 9


def _manifest() -> dict:
    return yaml.safe_load((ROOT / "rig" / "manifests" / f"{STREAM}.yaml").read_text())


def _camera() -> CameraConfig:
    return CameraConfig(
        camera_id=STREAM,
        role="canteen_door",
        space_id="canteen",
        door_id="c1",
        direction_hint="east",
        source_uri=f"rtsp://localhost:8554/{STREAM}",
        is_virtual=True,
        # Matches the manifest's door_line_x: 320 on a 640-wide frame. Inside is
        # east, so a person walking east is entering.
        door_line=(0.5, 0.0, 0.5, 1.0),
        inside_sign=1,
        analysis_fps=ANALYSIS_FPS,
    )


def _ingest_cfg() -> IngestConfig:
    return IngestConfig(
        stall_timeout_s=5.0,
        reconnect=ReconnectConfig(initial_s=0.3, max_s=2.0, jitter=0.2),
        ring_buffer_seconds=12.0,
        analysis_fps=ANALYSIS_FPS,
        max_clip_seconds=10.0,
    )


def _canteen_cfg() -> CanteenConfig:
    return CanteenConfig(
        band=0.03,
        min_samples_per_side=2,
        clip_pre_s=2.0,
        clip_post_s=1.0,
        clip_timeout_s=8.0,
        track_max_gap_s=0.5,
    )


async def _replay(
    store, tmp_path: Path, seconds: float, interrupt_after: float | None = None, rigctl=None
):
    camera = _camera()
    await store.upsert_camera(camera)
    clock = SystemClock()
    source = RTSPSource(camera, _ingest_cfg(), clock=clock, gaps=store)
    runtime: SessionRuntime = SessionRuntime("detector", MockDiskDetector, max_queue=4)
    metrics = InMemoryMetrics()
    pipeline = CanteenPipeline(
        camera,
        source,
        store,
        detector=runtime,
        clips=ClipStore(tmp_path),
        cfg=_canteen_cfg(),
        clock=clock,
        metrics=metrics,
    )
    # A second subscriber, only to learn where in the loop we joined.
    probe = source.subscribe()
    source_task = asyncio.create_task(source.run(), name="replay-source")
    killed = False
    try:
        assert await source.wait_until_up(20.0), "rig stream did not come up"
        first = await asyncio.wait_for(probe.get(), timeout=10.0)
        pipeline_task = asyncio.create_task(pipeline.run(), name="replay-pipeline")
        if interrupt_after is not None and rigctl is not None:
            await asyncio.sleep(interrupt_after)
            rigctl.send(rigctl.find(STREAM), signal.SIGKILL)
            killed = True
            await asyncio.sleep(seconds - interrupt_after)
        else:
            await asyncio.sleep(seconds)
        pipeline.stop()
        await asyncio.wait_for(pipeline_task, timeout=20.0)
    finally:
        source.unsubscribe(probe)
        source.stop()
        source_task.cancel()
        await asyncio.gather(source_task, return_exceptions=True)
        await runtime.aclose()
        if killed and rigctl is not None:
            # The rig fixture is session-scoped, so a stream this test killed
            # would stay dead for everything after it. Never leave it down.
            rigctl.spawn(rigctl.find(STREAM))
            await asyncio.sleep(2.0)
    return pipeline, first, metrics


def _fold(
    detected: list[tuple[datetime, str]], origin: datetime, duration: float
) -> list[tuple[float, str]]:
    return [(((ts - origin).total_seconds()) % duration, direction) for ts, direction in detected]


def _best_offset(
    folded: list[tuple[float, str]], truth: list[dict], duration: float, tolerance: float
) -> tuple[float, list[tuple[float, dict]]]:
    """Solve for the single loop offset that explains the most detections.

    Every candidate offset is "the first detection is actually this labelled
    crossing", which is a small finite set -- no fitting slack. A labelled
    crossing may be matched more than once, because watching for longer than one
    loop sees some crossings twice; what must not happen is a detection matching
    nothing.
    """
    best: tuple[float, list[tuple[float, dict]]] = (0.0, [])
    if not folded:
        return best
    first_t = folded[0][0]
    for candidate in truth:
        offset = (first_t - float(candidate["t"])) % duration
        pairs: list[tuple[float, dict]] = []
        for detected_t, _ in folded:
            aligned = (detected_t - offset) % duration
            nearest = min(
                truth,
                key=lambda row: min(
                    abs(aligned - float(row["t"])), duration - abs(aligned - float(row["t"]))
                ),
            )
            delta = abs(aligned - float(nearest["t"]))
            delta = min(delta, duration - delta)
            if delta <= tolerance:
                pairs.append((aligned, nearest))
        if len(pairs) > len(best[1]):
            best = (offset, pairs)
    return best


async def test_the_pipeline_reproduces_the_manifest_crossings(rig, store, tmp_path) -> None:
    manifest = _manifest()
    duration = float(manifest["duration_s"])
    truth = manifest["ground_truth"]["crossings"]
    pipeline, first_frame, metrics = await _replay(store, tmp_path, REPLAY_S)

    detected = [(e.ts_utc, e.direction) for e in pipeline.emitted]
    assert detected, "no crossings detected at all: the pipeline saw nothing"

    # Sampling adequacy is arithmetic, not hope: two samples either side of the
    # band at rig walking speed.
    tolerance = 2.0 / ANALYSIS_FPS + 0.25
    folded = _fold(
        detected, first_frame.ts_server - timedelta(seconds=first_frame.ts_media or 0.0), duration
    )
    offset, pairs = _best_offset(folded, truth, duration, tolerance)

    assert len(pairs) == len(detected), (
        f"{len(detected) - len(pairs)} detected crossing(s) matched no labelled crossing "
        f"(offset={offset:.2f}s, tolerance={tolerance:.2f}s): "
        f"{[round(t, 2) for t, _ in folded]} vs {[row['t'] for row in truth]}"
    )
    matched_times = {round(float(row["t"]), 3) for _, row in pairs}
    missed = [row["t"] for row in truth if round(float(row["t"]), 3) not in matched_times]
    assert len(matched_times) >= MIN_MATCHED, (
        f"only {len(matched_times)} of {len(truth)} labelled crossings were seen; missed {missed}"
    )

    # 1. Direction: zero errors, no tolerance. An enter counted as an exit
    #    inverts a dwell interval while leaving every number plausible.
    wrong = [
        (round(aligned, 2), row["direction"], direction)
        for (aligned, row), (_, direction) in zip(pairs, folded, strict=True)
        if row["direction"] != direction
    ]
    assert not wrong, f"direction errors (t, expected, got): {wrong}"

    # 2. Timing, derived from the configured rate rather than hard-coded.
    worst = max(abs(aligned - float(row["t"])) for aligned, row in pairs)
    assert worst <= tolerance, f"worst timing error {worst:.2f}s exceeds {tolerance:.2f}s"

    # 3. Persistence shape: unknown is the correct outcome with faces disabled,
    #    and asserting it stops a future "helpful" default creeping in.
    rows = await store.list_events(STREAM)
    assert len(rows) == len(detected)
    assert all(row.person_id is None for row in rows)
    assert all(row.door_id == "c1" for row in rows)
    assert metrics.get(
        "canteen_unknown_face_total", camera_id=STREAM, reason="faces_disabled"
    ) == len(detected)

    # 4. Clips: the ones that were written must exist; the ones that failed are
    #    counted, not hidden.
    with_clips = [row for row in rows if row.clip_ref]
    assert with_clips, "no clip was written for any crossing"
    for row in with_clips:
        assert (tmp_path / row.clip_ref).is_file(), f"{row.clip_ref} was recorded but not written"


async def test_a_stream_loss_mid_replay_fails_open(rig, store, tmp_path) -> None:
    """The whole point, end to end: stop the stream, and the day pays nothing.

    Identity does not exist yet, so every event the pipeline writes is unknown
    and payroll would attribute none of them. To exercise the payroll leg the
    events are relabelled to one person *in memory* -- the stored rows are
    untouched evidence -- which tests exactly the part that is real: a gap
    overlapping an interval forces the day to zero (ADR-0023).
    """
    _pipeline, _first, _metrics = await _replay(
        store, tmp_path, 25.0, interrupt_after=12.0, rigctl=rig
    )
    gaps = await store.list_gaps(STREAM)
    assert gaps, "a killed stream must open a gap: 'we saw nothing' is a fact"
    assert gaps[0].cause in ("offline", "decode_error", "stall", "crash")

    rows = await store.list_events(STREAM)
    if len(rows) < 2:
        pytest.skip("fewer than two crossings before the stream was killed")

    policy = PairingPolicy(allowance_s=1, min_dwell_s=1, max_dwell_s=3600)
    events = [
        DoorEvent(
            event_id=row.event_id,
            ts_utc=row.ts_utc,
            space_id="canteen",
            camera_id=row.camera_id,
            direction=row.direction,
            door_id=row.door_id,
            person_id="p-replay",
            clip_ref=row.clip_ref,
        )
        for row in rows
    ]
    payroll_gaps = [
        Gap(
            camera_id=g.camera_id,
            from_utc=g.from_utc,
            to_utc=g.to_utc,
            cause=g.cause,
            space_id="canteen",
        )
        for g in gaps
    ]

    class _At:
        def __init__(self, when: datetime) -> None:
            self._when = when

        def now_utc(self) -> datetime:
            return self._when

    result = run_pairing(events, payroll_gaps, policy, _At(datetime.now(UTC)))
    assert result.days, "expected at least one day to be computed"
    assert all(day.overage_s == 0 for day in result.days), (
        "a day overlapping a stream gap must charge nothing"
    )
    assert any(day.day_state == "flagged" for day in result.days)
