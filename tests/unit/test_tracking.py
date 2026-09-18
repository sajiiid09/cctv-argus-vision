"""The tracker: deterministic, short-lived, and geometry-only.

Its limits are the point. ADR-0002 resolves identity at doorways only, so a
tracker that lived longer, remembered appearance or persisted an id would be
re-identification wearing a different name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from argus.backends.types import Box, Detection
from argus.pipelines.tracking import ShortTracker, bottom_centre

T0 = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)


def _det(x: float, y: float = 100.0, score: float = 0.9) -> Detection:
    return Detection(box=Box(x, y, x + 20, y + 60), score=score, label="person", label_id=0)


def test_a_walking_person_keeps_one_track() -> None:
    tracker = ShortTracker()
    for i in range(5):
        tracker.update(T0 + timedelta(seconds=i * 0.1), [_det(100 + i * 12)], 640)
    assert [t.track_id for t in tracker.live] == [1]
    assert len(tracker.live[0].samples) == 5


def test_two_people_keep_two_tracks() -> None:
    tracker = ShortTracker()
    for i in range(4):
        tracker.update(
            T0 + timedelta(seconds=i * 0.1), [_det(100 + i * 10), _det(400 + i * 10)], 640
        )
    assert [t.track_id for t in tracker.live] == [1, 2]


def test_association_does_not_depend_on_detection_order() -> None:
    """Greedy association over an unordered mapping is the classic source of
    run-to-run drift, which would make the rig replay unreproducible.

    Permuting the detector's output changes which track id each person gets --
    ids are assigned in arrival order and mean nothing outside the run -- but it
    must not change how detections are grouped into tracks.
    """
    first = ShortTracker()
    second = ShortTracker()
    for i in range(4):
        ts = T0 + timedelta(seconds=i * 0.1)
        dets = [_det(100 + i * 10), _det(300 + i * 10), _det(500 + i * 10)]
        first.update(ts, dets, 640)
        second.update(ts, list(reversed(dets)), 640)
    assert sorted(bottom_centre(t.last_box) for t in first.live) == sorted(
        bottom_centre(t.last_box) for t in second.live
    )
    assert [len(t.samples) for t in first.live] == [4, 4, 4]
    assert sorted(len(t.samples) for t in second.live) == [4, 4, 4]


def test_a_track_ends_when_nothing_matches_it() -> None:
    tracker = ShortTracker(max_gap_s=0.5)
    tracker.update(T0, [_det(100)], 640)
    ended = tracker.update(T0 + timedelta(seconds=1.0), [], 640)
    assert [t.track_id for t in ended] == [1]
    assert tracker.live == []


def test_max_age_is_a_ceiling_not_a_suggestion() -> None:
    """ "Keeping a track alive to help with pairing" is AGENTS.md §9 territory."""
    tracker = ShortTracker(max_age_s=1.0, max_gap_s=10.0)
    ended: list[int] = []
    for i in range(20):
        finished = tracker.update(T0 + timedelta(seconds=i * 0.2), [_det(100 + i)], 640)
        ended.extend(t.track_id for t in finished)
    assert ended, "a track older than max_age_s must be dropped, not extended"
    assert all(t.age_s(T0 + timedelta(seconds=4)) <= 1.0 + 0.2 for t in tracker.live)


def test_ids_are_process_local_and_monotonic() -> None:
    tracker = ShortTracker(max_gap_s=0.1)
    tracker.update(T0, [_det(100)], 640)
    tracker.update(T0 + timedelta(seconds=1), [_det(500)], 640)
    assert [t.track_id for t in tracker.live] == [2]
    fresh = ShortTracker()
    fresh.update(T0, [_det(100)], 640)
    # A new process starts at 1 again: these ids mean nothing outside this run,
    # which is why nothing stores them.
    assert fresh.live[0].track_id == 1


def test_a_teleporting_detection_starts_a_new_track() -> None:
    tracker = ShortTracker(max_travel_frac=0.1)
    tracker.update(T0, [_det(50)], 640)
    tracker.update(T0 + timedelta(seconds=0.1), [_det(600)], 640)
    assert len(tracker.live) == 2


def test_the_reference_point_is_the_bottom_centre() -> None:
    """A head-centre reference crosses a floor line early or late by height,
    which is a bias by body size, not noise (SOUL.md)."""
    assert bottom_centre(Box(100.0, 50.0, 140.0, 210.0)) == (120.0, 210.0)


def test_flush_ends_everything_because_the_stream_stopped() -> None:
    tracker = ShortTracker()
    tracker.update(T0, [_det(100), _det(400)], 640)
    assert len(tracker.flush(T0 + timedelta(seconds=0.1))) == 2
    assert tracker.live == []


def test_track_count_is_bounded() -> None:
    tracker = ShortTracker(max_tracks=4, max_travel_frac=0.01)
    for i in range(10):
        tracker.update(T0 + timedelta(seconds=i * 0.05), [_det(i * 60)], 640)
    assert len(tracker.live) <= 4
