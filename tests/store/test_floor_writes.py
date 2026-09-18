"""Occupancy samples and violence candidates, written for real.

Two properties matter more than the writes themselves: neither table can name a
person, and a candidate is a queue item rather than a conclusion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from argus.common.clock import FixedClock
from argus.common.config import CameraConfig, OccupancyConfig, SeatConfig, ViolenceConfig
from argus.ingest.floor import FloorRunner
from argus.pipelines.runtime import SessionRuntime

pytestmark = pytest.mark.postgres

T0 = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)


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


def _camera() -> CameraConfig:
    return CameraConfig(
        camera_id="floor_view",
        role="floor",
        source_uri="rtsp://localhost:8554/floor_view",
        is_virtual=True,
    )


def _floor_frame(ts: datetime, seated: bool = True) -> _Frame:
    image = np.full((360, 640, 3), 50, dtype=np.uint8)
    if seated:
        image[100:200, 40:120] = 190  # a bright blob over seat a1
    return _Frame(ts, image)


async def _camera_row(store) -> None:
    await store.db.execute(
        "insert into camera (camera_id, role, source_uri, is_virtual)"
        " values ('floor_view', 'floor', 'rtsp://localhost:8554/floor_view', true)"
        " on conflict (camera_id) do nothing"
    )


def _seats() -> list[SeatConfig]:
    """Two small regions, the size a seat actually is in a wide floor shot.

    Small matters here for a mock-shaped reason too: the mock detector splits a
    blob wider than its cluster radius, so a seat region the size of the whole
    blob would be only partly covered by each piece.
    """
    return [
        SeatConfig("a1", (0.07, 0.30, 0.14, 0.48), "line-a"),
        SeatConfig("a2", (0.40, 0.30, 0.47, 0.48), "line-a"),
    ]


async def test_occupancy_samples_are_written_without_a_person(store) -> None:
    await _camera_row(store)
    from argus.backends.mock import MockDiskDetector

    detector: SessionRuntime = SessionRuntime("detector", MockDiskDetector)
    camera = _camera()
    runner = FloorRunner(
        camera,
        _Source(camera),
        store.db,
        detector=detector,
        pose=None,
        occupancy_cfg=OccupancyConfig(
            enabled=True, seats=_seats(), smoothing_samples=1, sample_interval_s=1.0
        ),
        violence_cfg=ViolenceConfig(),
        clock=FixedClock(T0),
    )
    try:
        await runner.handle(_floor_frame(T0))
        rows = await store.db.fetch_all(
            "select seat_id, line_id, occupied, window_s from occupancy_sample order by seat_id"
        )
        assert rows == [("a1", "line-a", True, 1), ("a2", "line-a", False, 1)]
        columns = await store.db.fetch_all(
            "select column_name from information_schema.columns"
            " where table_name = 'occupancy_sample'"
        )
        assert "person_id" not in {row[0] for row in columns}
    finally:
        await detector.aclose()


async def test_the_slow_cadence_is_respected(store) -> None:
    await _camera_row(store)
    from argus.backends.mock import MockDiskDetector

    detector: SessionRuntime = SessionRuntime("detector", MockDiskDetector)
    camera = _camera()
    runner = FloorRunner(
        camera,
        _Source(camera),
        store.db,
        detector=detector,
        pose=None,
        occupancy_cfg=OccupancyConfig(
            enabled=True, seats=_seats(), smoothing_samples=1, sample_interval_s=30.0
        ),
        violence_cfg=ViolenceConfig(),
        clock=FixedClock(T0),
    )
    try:
        for offset in (0, 1, 2, 3):
            await runner.handle(_floor_frame(T0 + timedelta(seconds=offset)))
        rows = await store.db.fetch_all("select count(*) from occupancy_sample")
        assert rows == [(2,)], "four frames in four seconds is one sample, not four"
    finally:
        await detector.aclose()


async def test_a_violence_candidate_is_a_queue_item_with_no_verdict(store) -> None:
    await _camera_row(store)
    from argus.backends.mock import MockPoseEstimator

    pose: SessionRuntime = SessionRuntime("pose", MockPoseEstimator)
    camera = _camera()
    runner = FloorRunner(
        camera,
        _Source(camera),
        store.db,
        detector=None,
        pose=pose,
        occupancy_cfg=OccupancyConfig(),
        violence_cfg=ViolenceConfig(enabled=True, trigger_threshold=0.05, cooldown_s=30.0),
        clock=FixedClock(T0),
    )
    try:
        # Two blobs close together: the mock pose estimator lays a skeleton over
        # each, which is enough structure for the proximity feature.
        image = np.full((360, 640, 3), 50, dtype=np.uint8)
        ys, xs = np.ogrid[:360, :640]
        for cx in (300, 340):
            image[(xs - cx) ** 2 + (ys - 200) ** 2 <= 16**2] = 190
        await runner.handle(_Frame(T0, image))
        rows = await store.db.fetch_all(
            "select camera_id, trigger_score, review_state, reviewed_by, clip_id,"
            " features ? 'proximity' from violence_candidate"
        )
        assert len(rows) == 1
        camera_id, score, state, reviewed_by, clip_id, has_features = rows[0]
        assert camera_id == "floor_view"
        assert 0 < score <= 1
        assert state == "pending" and reviewed_by is None
        assert clip_id is None, "no clip store was given, and that is visible rather than implied"
        assert has_features is True, "a reviewer should see why they were shown this"
    finally:
        await pose.aclose()


async def test_a_floor_camera_cannot_write_a_doorway_event(store) -> None:
    """Identity lives at doorways only (ADR-0002), so the sink cannot do it."""
    from argus.ingest.floor import _GapSink

    sink = _GapSink(store.db)
    with pytest.raises(AssertionError, match="ADR-0002"):
        await sink.insert_doorway_event("floor_view", None, T0, "enter")


async def test_no_occupancy_code_path_reaches_a_payroll_table(store) -> None:
    """PLAN.md M5 asks for this by name. Belt: the import graph. Braces: the
    foreign keys the table actually has."""
    rows = await store.db.fetch_all(
        "select ccu.table_name from information_schema.table_constraints tc"
        " join information_schema.constraint_column_usage ccu"
        "   on ccu.constraint_name = tc.constraint_name"
        " where tc.table_name = 'occupancy_sample' and tc.constraint_type = 'FOREIGN KEY'"
    )
    referenced = {row[0] for row in rows}
    assert referenced == {"camera"}, f"occupancy_sample references {referenced}"
