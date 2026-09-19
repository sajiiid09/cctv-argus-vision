"""Occupancy: anonymous, smoothed, and unable to reach a payroll table.

The last one is the reason this file exists. `SOUL.md`'s "two separate numbers"
is the moral centre of the project, and occupancy is the number that must never
drift toward pay.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from argus.backends.types import Box, Detection
from argus.common.config import ConfigError, OccupancyConfig, SeatConfig
from argus.pipelines.occupancy import (
    INSERT_SAMPLE,
    OccupancyPipeline,
    line_rates,
    overlap_fraction,
    seat_box,
)

T0 = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)
FRAME = (360, 640)


def _seats() -> list[SeatConfig]:
    return [
        SeatConfig(seat_id="a1", region=(0.05, 0.1, 0.20, 0.5), line_id="line-a"),
        SeatConfig(seat_id="a2", region=(0.30, 0.1, 0.45, 0.5), line_id="line-a"),
        SeatConfig(seat_id="b1", region=(0.60, 0.1, 0.75, 0.5), line_id="line-b"),
    ]


def _person(x1: float, y1: float, x2: float, y2: float) -> Detection:
    return Detection(box=Box(x1, y1, x2, y2), score=0.9, label="person", label_id=0)


def _pipeline(**kw) -> OccupancyPipeline:
    cfg = OccupancyConfig(seats=_seats(), **kw)
    return OccupancyPipeline("floor_view", cfg)


def test_a_seated_person_occupies_their_seat_and_nobody_elses() -> None:
    pipeline = _pipeline(smoothing_samples=1)
    seated = _person(35, 40, 125, 175)  # over seat a1
    observations = pipeline.observe(T0, [seated], FRAME)
    occupied = {o.seat_id: o.occupied for o in observations}
    assert occupied == {"a1": True, "a2": False, "b1": False}


def test_somebody_walking_past_does_not_occupy_a_seat() -> None:
    """Overlap is measured against the seat's area, not the person's: a
    passer-by clips the edge of a region, a seated operator fills it."""
    pipeline = _pipeline(smoothing_samples=1, min_overlap=0.25)
    passing = _person(120, 40, 135, 300)  # a thin sliver over a1's right edge
    observations = {o.seat_id: o.occupied for o in pipeline.observe(T0, [passing], FRAME)}
    assert observations["a1"] is False


def test_state_is_smoothed_over_several_samples() -> None:
    """A person leaning out of frame for one sample is not absent."""
    pipeline = _pipeline(smoothing_samples=3)
    seated = _person(35, 40, 125, 175)
    at = T0
    for _ in range(3):
        pipeline.observe(at, [seated], FRAME)
        at += timedelta(seconds=30)
    assert pipeline.observe(at, [], FRAME)[0].occupied is True, "one empty sample is not absence"
    at += timedelta(seconds=30)
    pipeline.observe(at, [], FRAME)
    at += timedelta(seconds=30)
    assert pipeline.observe(at, [], FRAME)[0].occupied is False


def test_the_cadence_is_slow_and_asked_about_rather_than_assumed() -> None:
    pipeline = _pipeline(sample_interval_s=30.0)
    assert pipeline.due(T0) is True
    pipeline.observe(T0, [], FRAME)
    assert pipeline.due(T0 + timedelta(seconds=5)) is False
    assert pipeline.due(T0 + timedelta(seconds=31)) is True


def test_a_sample_row_carries_no_person_and_no_way_to_find_one() -> None:
    """SOUL.md "two separate numbers", at the point of writing."""
    pipeline = _pipeline(smoothing_samples=1)
    rows = pipeline.rows(pipeline.observe(T0, [_person(35, 40, 125, 175)], FRAME))
    assert len(rows) == 3
    for row in rows:
        assert "person" not in str(row).lower()
    assert "person" not in INSERT_SAMPLE
    assert "occupancy_sample" in INSERT_SAMPLE


def test_the_default_view_is_line_level() -> None:
    """ADR-0019: per-seat stored, aggregate shown, per-seat behind admin."""
    pipeline = _pipeline(smoothing_samples=1)
    observations = pipeline.observe(T0, [_person(35, 40, 125, 175)], FRAME)
    assert line_rates(observations) == {"line-a": 0.5, "line-b": 0.0}


def test_seats_are_configuration_not_code() -> None:
    """PLAN.md M5: adding a seat must not need a code change."""
    cfg = OccupancyConfig(seats=[*_seats(), SeatConfig("c1", (0.8, 0.1, 0.9, 0.4), "line-c")])
    pipeline = OccupancyPipeline("floor_view", cfg)
    assert [seat.seat_id for seat in pipeline.seats] == ["a1", "a2", "b1", "c1"]


def test_seat_regions_are_fractions_and_refuse_nonsense() -> None:
    with pytest.raises(ConfigError, match="fractions of the frame"):
        SeatConfig("bad", (10.0, 0.1, 20.0, 0.5))
    with pytest.raises(ConfigError, match="positive area"):
        SeatConfig("flat", (0.5, 0.5, 0.5, 0.9))
    with pytest.raises(ConfigError, match="repeated"):
        OccupancyConfig(
            seats=[SeatConfig("a1", (0.1, 0.1, 0.2, 0.2)), SeatConfig("a1", (0.3, 0.1, 0.4, 0.2))]
        )


def test_seat_boxes_scale_with_the_frame() -> None:
    seat = SeatConfig("a1", (0.5, 0.0, 1.0, 0.5))
    small = seat_box(seat, 640, 360)
    large = seat_box(seat, 2304, 1296)
    assert (small.x1, small.y2) == (320.0, 180.0)
    assert (large.x1, large.y2) == (1152.0, 648.0)


def test_overlap_is_of_the_seat_area() -> None:
    seat = Box(0, 0, 100, 100)
    assert overlap_fraction(Box(0, 0, 100, 100), seat) == pytest.approx(1.0)
    assert overlap_fraction(Box(0, 0, 50, 100), seat) == pytest.approx(0.5)
    assert overlap_fraction(Box(200, 200, 300, 300), seat) == 0.0


def test_the_module_cannot_reach_payroll() -> None:
    """PLAN.md M5 asks for this test by name."""
    from pathlib import Path

    import argus.pipelines.occupancy as module

    text = Path(module.__file__).read_text()
    for token in ("dwell_day", "payroll_line", "dwell_interval", "run_pairing"):
        assert token not in text
