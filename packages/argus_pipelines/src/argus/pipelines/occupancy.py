"""Anonymous per-seat occupancy (M5).

This is the pipeline that must never touch pay. `SOUL.md` argues it at length:
occupancy measures presence at a coordinate, and an operator away from their
seat may be at the toilet, collecting bundles, waiting on a mechanic, or
covering another line. The gap between "present at a coordinate" and "working"
is exactly where an unfair deduction would live.

So: no `person_id` anywhere in this module, no import of `argus.payroll`, and
the samples land in a table with no foreign key that could acquire one. Three
tests enforce that -- an import check, a SQL-text check, and PLAN.md M5's "a
test asserts that no occupancy code path can reach a payroll table".

Two smaller decisions:

* **Seat regions are configuration.** Adding a seat must not need a code change
  (PLAN.md M5), because a factory floor is re-laid out more often than software
  is released.
* **The cadence is slow and the state is smoothed.** A person leaning out of
  frame for one sample is not absent, and a fast unsmoothed stream would invite
  reading occupancy as an event log, which it is not.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from argus.backends.types import Box, Detection
from argus.common.config import OccupancyConfig, SeatConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SeatObservation:
    seat_id: str
    line_id: str | None
    occupied: bool
    confidence: float
    at: datetime


def seat_box(seat: SeatConfig, frame_width: int, frame_height: int) -> Box:
    x1, y1, x2, y2 = seat.region
    return Box(x1=x1 * frame_width, y1=y1 * frame_height, x2=x2 * frame_width, y2=y2 * frame_height)


def overlap_fraction(person: Box, seat: Box) -> float:
    """How much of the seat region the person covers.

    Of the *seat*, not of the person: somebody walking past a seat covers a
    sliver of it, and somebody sitting in it covers most of it. Using the
    person's box as the denominator would make a distant passer-by look seated.
    """
    ix1, iy1 = max(person.x1, seat.x1), max(person.y1, seat.y1)
    ix2, iy2 = min(person.x2, seat.x2), min(person.y2, seat.y2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area = (seat.x2 - seat.x1) * (seat.y2 - seat.y1)
    return inter / area if area > 0 else 0.0


@dataclass
class SeatSmoother:
    """Majority vote over the last N observations of one seat."""

    window: int = 3
    _samples: deque[bool] = field(default_factory=lambda: deque(maxlen=3))
    state: bool = False

    def __post_init__(self) -> None:
        self.window = max(1, self.window)
        self._samples = deque(maxlen=self.window)

    def update(self, occupied: bool) -> bool:
        self._samples.append(occupied)
        if len(self._samples) < self.window:
            # Not enough evidence yet: report what we have seen most, but do not
            # pretend to a settled state.
            return sum(self._samples) * 2 > len(self._samples)
        self.state = sum(self._samples) * 2 > len(self._samples)
        return self.state


class OccupancyPipeline:
    """Detections in, anonymous seat samples out."""

    def __init__(self, camera_id: str, cfg: OccupancyConfig) -> None:
        self.camera_id = camera_id
        self.cfg = cfg
        self._smoothers = {
            seat.seat_id: SeatSmoother(window=cfg.smoothing_samples) for seat in cfg.seats
        }
        self._last_sample_at: datetime | None = None

    @property
    def seats(self) -> list[SeatConfig]:
        return list(self.cfg.seats)

    def due(self, now: datetime) -> bool:
        if self._last_sample_at is None:
            return True
        return (now - self._last_sample_at).total_seconds() >= self.cfg.sample_interval_s

    def observe(
        self,
        at: datetime,
        detections: list[Detection],
        frame_shape: tuple[int, int],
    ) -> list[SeatObservation]:
        """One sample per seat. No identity, and nothing to join one to."""
        height, width = frame_shape
        self._last_sample_at = at
        people = [d.box for d in detections]
        observations: list[SeatObservation] = []
        for seat in self.cfg.seats:
            region = seat_box(seat, width, height)
            best = max((overlap_fraction(person, region) for person in people), default=0.0)
            raw = best >= self.cfg.min_overlap
            smoothed = self._smoothers[seat.seat_id].update(raw)
            observations.append(
                SeatObservation(
                    seat_id=seat.seat_id,
                    line_id=seat.line_id,
                    occupied=smoothed,
                    confidence=round(min(1.0, best), 4),
                    at=at,
                )
            )
        return observations

    def rows(self, observations: list[SeatObservation]) -> list[tuple]:
        """Parameters for the occupancy_sample insert.

        Written here rather than in a store module so that the absence of a
        person column is visible in the same file as the rest of the rule.
        """
        return [
            (
                uuid4(),
                self.camera_id,
                observation.seat_id,
                observation.line_id,
                observation.at,
                observation.occupied,
                observation.confidence,
                int(self.cfg.sample_interval_s),
            )
            for observation in observations
        ]


INSERT_SAMPLE = (
    "insert into occupancy_sample (sample_id, camera_id, seat_id, line_id, at, occupied,"
    " confidence, window_s) values (%s,%s,%s,%s,%s,%s,%s,%s)"
)


def line_rates(observations: list[SeatObservation]) -> dict[str, float]:
    """Occupied share per line: what the default view shows (ADR-0019).

    The per-seat view exists and is stored, but it sits behind the admin tier
    and is access-logged, because a seat identifies a person via the roster.
    """
    by_line: dict[str, list[bool]] = {}
    for observation in observations:
        by_line.setdefault(observation.line_id or "unassigned", []).append(observation.occupied)
    return {line: round(sum(states) / len(states), 4) for line, states in sorted(by_line.items())}
