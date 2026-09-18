"""The canteen pipeline, driven frame by frame with fakes.

No docker, no rig, no artefacts: the point of these is the failure behaviour,
which is the part of this pipeline that matters. A clip that cannot be extracted
must still produce an event, because dropping the event would lose evidence and
*reduce* the flag count -- the day would look clean.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
import pytest
from argus.backends.mock import MockDiskDetector
from argus.common.clock import FixedClock
from argus.common.config import CameraConfig, CanteenConfig
from argus.pipelines import metrics as m
from argus.pipelines.aim import AimMonitor
from argus.pipelines.canteen import CanteenPipeline
from argus.pipelines.metrics import InMemoryMetrics
from argus.pipelines.runtime import SessionRuntime

T0 = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)


def _camera(**kw) -> CameraConfig:
    params = dict(
        camera_id="canteen_door_01",
        role="canteen_door",
        space_id="canteen",
        door_id="c1",
        source_uri="rtsp://localhost:8554/canteen_door_01",
        is_virtual=True,
        door_line=(0.5, 0.0, 0.5, 1.0),
        inside_sign=1,
    )
    params.update(kw)
    return CameraConfig(**params)


class _Frame:
    def __init__(self, ts: datetime, image: np.ndarray, ts_media: float | None = None) -> None:
        self.ts_server = ts
        self.ts_media = ts_media
        self.image = image


class _Source:
    def __init__(self, camera: CameraConfig) -> None:
        self.camera = camera

    def subscribe(self):  # pragma: no cover - handle() is driven directly
        raise AssertionError("not used")

    def unsubscribe(self, q) -> None:  # pragma: no cover
        raise AssertionError("not used")


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.gaps: list[tuple[str, str]] = []
        self.closed: list[UUID] = []
        self.clips_registered: list[tuple[str, str]] = []

    async def insert_doorway_event(self, camera_id, door_id, ts_utc, direction, **kw):
        row = {
            "event_id": uuid4(),
            "camera_id": camera_id,
            "door_id": door_id,
            "ts_utc": ts_utc,
            "direction": direction,
            **kw,
        }
        self.events.append(row)

        class _Row:
            event_id = row["event_id"]

        return _Row()

    async def insert_clip(self, camera_id, rel_path, start_utc, end_utc, **kw):
        self.clips_registered.append((camera_id, rel_path))
        return uuid4()

    async def open_gap(self, camera_id: str, from_utc: datetime, cause: str) -> UUID:
        self.gaps.append((camera_id, cause))
        return uuid4()

    async def close_gap(self, gap_id: UUID, to_utc: datetime) -> None:
        self.closed.append(gap_id)


class _Clips:
    def __init__(self, fail: bool = False, root: Path | None = None) -> None:
        self.fail = fail
        self.root = root or Path("var/clips")
        self.calls = 0

    async def extract(self, source, start_utc, end_utc, *, timeout_s: float = 10.0) -> Path:
        self.calls += 1
        if self.fail:
            raise RuntimeError("no packets for that range")
        return self.root / source.camera.camera_id / f"{start_utc.timestamp():.0f}.mp4"


# A walk across the door line at 20 px per sample. The step matters: at 40 px
# the jump between samples exceeds the tracker's travel gate and the walk splits
# into two tracks, which is the tracker working -- a person cannot cross half the
# frame in one sample period, so something that appears to is two people.
WALK_EAST = [200.0 + 20.0 * i for i in range(13)]
WALK_WEST = list(reversed(WALK_EAST))


def _frame(x: float, ts: datetime) -> _Frame:
    image = np.full((360, 640, 3), 44, dtype=np.uint8)
    ys, xs = np.ogrid[:360, :640]
    image[(xs - int(x)) ** 2 + (ys - 200) ** 2 <= 16**2] = 190
    return _Frame(ts, image)


async def _pipeline(**kw):
    camera = kw.pop("camera", _camera())
    sink = kw.pop("events", _Sink())
    detector: SessionRuntime = SessionRuntime("detector", MockDiskDetector, max_queue=4)
    pipeline = CanteenPipeline(
        camera,
        _Source(camera),
        sink,
        detector=detector,
        clock=FixedClock(T0),
        cfg=kw.pop("cfg", CanteenConfig(band=0.03, min_samples_per_side=2)),
        metrics=kw.pop("metrics", InMemoryMetrics()),
        **kw,
    )
    return pipeline, sink, detector


async def _walk(pipeline, xs: list[float], start: datetime = T0, step_s: float = 0.1):
    written = []
    for i, x in enumerate(xs):
        written.extend(await pipeline.handle(_frame(x, start + timedelta(seconds=i * step_s))))
    return written


async def test_a_walk_through_the_door_writes_one_enter_event() -> None:
    pipeline, sink, runtime = await _pipeline(clips=_Clips())
    try:
        # The crossing is reported as the walk happens, not when the track ends.
        written = await _walk(pipeline, WALK_EAST)
        assert [e.direction for e in written] == ["enter"]
        assert [e["direction"] for e in sink.events] == ["enter"]
        assert sink.events[0]["person_id"] is None
        assert sink.events[0]["clip_ref"].startswith("canteen_door_01/")
        # The file is registered too: the console resolves a clip_id, never a
        # path, so an unregistered clip is one nobody can open.
        assert sink.clips_registered == [("canteen_door_01", sink.events[0]["clip_ref"])]
    finally:
        await runtime.aclose()


async def test_a_failed_clip_still_writes_the_event() -> None:
    """ADR-0008: no clip, no deduction -- but the event is the evidence that the
    day must be flagged, so dropping it would be a failure in the punitive
    direction."""
    clips = _Clips(fail=True)
    pipeline, sink, runtime = await _pipeline(clips=clips)
    try:
        await _walk(pipeline, WALK_EAST)
        await pipeline.handle(_frame(-100, T0 + timedelta(seconds=3)))
        assert len(sink.events) == 1
        assert sink.events[0]["clip_ref"] is None
        assert "missing_clip" in pipeline.emitted[0].flags
        assert clips.calls == 1
    finally:
        await runtime.aclose()


async def test_faces_disabled_means_unknown_and_says_so() -> None:
    metrics = InMemoryMetrics()
    pipeline, sink, runtime = await _pipeline(metrics=metrics)
    try:
        await _walk(pipeline, WALK_EAST)
        await pipeline.handle(_frame(-100, T0 + timedelta(seconds=3)))
        assert sink.events[0]["person_id"] is None
        assert pipeline.emitted[0].flags == ("faces_disabled",)
        assert (
            metrics.get(m.UNKNOWN_FACE, camera_id="canteen_door_01", reason="faces_disabled") == 1
        )
        assert metrics.get(m.CROSSINGS, camera_id="canteen_door_01", direction="enter") == 1
    finally:
        await runtime.aclose()


async def test_a_camera_without_a_door_line_is_refused_at_construction() -> None:
    camera = _camera(door_line=None)
    detector: SessionRuntime = SessionRuntime("detector", MockDiskDetector)
    try:
        with pytest.raises(ValueError, match="no door_line"):
            CanteenPipeline(camera, _Source(camera), _Sink(), detector=detector)
    finally:
        await detector.aclose()


async def test_a_drifted_camera_stops_emitting_and_opens_a_gap() -> None:
    """ADR-0029. Crossings against a line that no longer matches the door are
    confident and wrong, which is worse than no crossings."""
    rng = np.random.default_rng(3)
    reference = rng.integers(0, 255, size=(360, 640, 3), dtype=np.uint8)
    aim = AimMonitor(reference, max_offset_px=2.0, consecutive=1)
    cfg = CanteenConfig(aim_check_interval_s=0.0)
    pipeline, sink, runtime = await _pipeline(aim=aim, cfg=cfg)
    try:
        moved = np.roll(reference, 40, axis=1)
        pipeline._recent_frames.clear()
        await pipeline.handle(_Frame(T0, moved))
        assert sink.gaps == [("canteen_door_01", "aim_changed")]
        # while drifted, frames are not analysed at all
        before = len(sink.events)
        await _walk(pipeline, WALK_EAST, start=T0 + timedelta(seconds=1))
        assert len(sink.events) == before
    finally:
        await runtime.aclose()


async def test_a_second_detection_of_one_crossing_points_back_at_the_first() -> None:
    """In-process dedup reads the same window payroll reads (ADR-0032).

    The later row carries duplicate_of pointing at the earlier one, which is the
    row that survives -- the first detection is the better estimate of when the
    person actually crossed.
    """
    pipeline, sink, runtime = await _pipeline(faces=_AlwaysMatches("p1"))
    try:
        first = await pipeline._emit(_stub_track(), _crossing(T0, "enter"))
        soon = await pipeline._emit(_stub_track(), _crossing(T0 + timedelta(seconds=2), "enter"))
        later = await pipeline._emit(_stub_track(), _crossing(T0 + timedelta(seconds=30), "enter"))
        assert first.duplicate_of is None
        assert soon.duplicate_of == first.event_id
        assert "deduplicated" in soon.flags
        # well outside the window: a second visit, not a second detection
        assert later.duplicate_of is None
        assert [e["person_id"] for e in sink.events] == ["p1", "p1", "p1"]
    finally:
        await runtime.aclose()


async def test_an_unknown_person_is_never_deduplicated() -> None:
    """Two unknown crossings a second apart might be two different people."""
    pipeline, _sink, runtime = await _pipeline()
    try:
        first = await pipeline._emit(_stub_track(), _crossing(T0, "enter"))
        second = await pipeline._emit(_stub_track(), _crossing(T0 + timedelta(seconds=1), "enter"))
        assert first.duplicate_of is None and second.duplicate_of is None
    finally:
        await runtime.aclose()


class _AlwaysMatches:
    """A face stage that always recognises the same person."""

    def __init__(self, person_id: str) -> None:
        self.person_id = person_id

    def identify(self, frames, person_box=None):
        from argus.pipelines.faces import FaceAttempt, Identification

        return FaceAttempt(
            Identification(self.person_id, 0.9, 0.1, "matched"), None, len(list(frames))
        )


def _stub_track():
    from argus.backends.types import Box
    from argus.pipelines.tracking import Track, TrackSample

    track = Track(track_id=1)
    track.samples.append(
        TrackSample(ts_server=T0, point=(320.0, 200.0), box=Box(300, 150, 340, 200), score=0.9)
    )
    return track


def _crossing(ts: datetime, direction: str):
    from argus.pipelines.doorway import Crossing

    return Crossing(
        ts_utc=ts, direction=direction, track_ref=1, samples_before=2, samples_after=2, quality=0.9
    )


async def test_a_walk_back_out_is_an_exit() -> None:
    pipeline, sink, runtime = await _pipeline()
    try:
        await _walk(pipeline, WALK_WEST)
        await pipeline.handle(_frame(-100, T0 + timedelta(seconds=3)))
        assert [e["direction"] for e in sink.events] == ["exit"]
    finally:
        await runtime.aclose()


async def test_loitering_in_the_doorway_produces_nothing() -> None:
    """The rig includes a slow loiterer for exactly this case."""
    pipeline, sink, runtime = await _pipeline()
    try:
        await _walk(pipeline, [318.0, 320.0, 322.0, 320.0, 318.0])
        await pipeline.handle(_frame(-100, T0 + timedelta(seconds=3)))
        assert sink.events == []
    finally:
        await runtime.aclose()


async def test_sustained_dropping_opens_an_overload_gap() -> None:
    """A pipeline analysing one frame in ten is claiming to measure something
    it is not (ADR-0033)."""
    pipeline, sink, runtime = await _pipeline(
        cfg=CanteenConfig(max_drop_rate=0.5, drop_rate_window=4)
    )
    try:
        pipeline._analysed = 1
        pipeline._dropped = 3
        await pipeline._check_drop_rate()
        assert sink.gaps == [("canteen_door_01", "overload")]
    finally:
        await runtime.aclose()
