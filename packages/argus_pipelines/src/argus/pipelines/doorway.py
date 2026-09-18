"""Line crossing: the geometry that decides `enter` or `exit`.

The single highest-consequence line in the pipeline is the direction sign. An
inverted sign turns every entry into an exit and leaves every number downstream
looking entirely reasonable -- plausible dwell times, plausible overage,
attributed to the wrong minutes. So the convention is stated here, tested
longhand against the rig's labelled ground truth, and derived from config rather
than from a constant.

Four decisions, each with a reason:

* **Fractional coordinates, resolved per frame.** ``CameraConfig.door_line`` is
  fractions of width and height because the main and analysis streams differ in
  resolution (ADR-0031); a pixel line would be silently reinterpreted between
  them.
* **Bottom-centre reference point** (see tracking.TrackSample): a head-centre
  reference biases by body height.
* **Hysteresis band, and at least two samples clear of it on each side.** One
  sample either side of a line is a detector jitter away from a phantom
  crossing, and a phantom crossing is a deduction.
* **`ambiguous` is a real output.** In-out-in inside one track, or a track that
  ends inside the band, yields `ambiguous`, which payroll routes to zero and a
  flag. Never a best guess.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from argus.pipelines.tracking import Track, TrackSample

Direction = Literal["enter", "exit", "ambiguous"]
Zone = Literal["inside", "outside", "band"]


@dataclass(frozen=True, slots=True)
class DoorLine:
    """A line in frame fractions, plus which side of it is inside the space."""

    x1: float
    y1: float
    x2: float
    y2: float
    inside_sign: int = 1

    def __post_init__(self) -> None:
        for value in (self.x1, self.y1, self.x2, self.y2):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"door_line values are fractions of the frame, got {value}")
        if (self.x1, self.y1) == (self.x2, self.y2):
            raise ValueError("door_line endpoints must differ")
        if self.inside_sign not in (1, -1):
            raise ValueError(f"inside_sign must be +1 or -1, got {self.inside_sign}")

    @classmethod
    def from_camera(
        cls, door_line: tuple[float, float, float, float], inside_sign: int
    ) -> DoorLine:
        x1, y1, x2, y2 = door_line
        return cls(x1=x1, y1=y1, x2=x2, y2=y2, inside_sign=inside_sign)

    def signed_distance(
        self, point: tuple[float, float], frame_width: int, frame_height: int
    ) -> float:
        """Distance from the line, normalised by the frame diagonal.

        **The convention, written out because everything downstream depends on
        it:** positive is the side to the *right* of walking from (x1, y1)
        towards (x2, y2), and positive means inside when ``inside_sign`` is +1.
        So for a line drawn top-to-bottom -- ``[0.5, 0.0, 0.5, 1.0]``, which is
        what the rig's canteen doors use -- the inside is to the **east**
        (increasing x), and a person walking east through it is entering.

        Normalising by the diagonal means the band threshold is one fraction
        that means the same thing at 640x360 and at 2304x1296, which is the
        whole reason the line is stored as fractions.
        """
        ax, ay = self.x1 * frame_width, self.y1 * frame_height
        bx, by = self.x2 * frame_width, self.y2 * frame_height
        px, py = point
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length == 0.0:
            raise ValueError("degenerate door line")
        cross = (dy * (px - ax) - dx * (py - ay)) / length
        diagonal = math.hypot(frame_width, frame_height)
        return self.inside_sign * cross / diagonal

    def zone(
        self, point: tuple[float, float], frame_width: int, frame_height: int, band: float
    ) -> Zone:
        distance = self.signed_distance(point, frame_width, frame_height)
        if abs(distance) <= band:
            return "band"
        return "inside" if distance > 0 else "outside"


@dataclass(frozen=True, slots=True)
class Crossing:
    ts_utc: datetime
    direction: Direction
    # Process-local, used only to fetch face frames for this crossing. Never
    # stored (ADR-0002).
    track_ref: int
    samples_before: int
    samples_after: int
    quality: float
    reason: str = ""


class DoorwayDetector:
    """Stateless geometry: zones, and the rule that turns runs of them into crossings.

    Crossings are detected **incrementally**, as samples arrive, rather than
    when a track ends. That is not an optimisation. Waiting for the end of a
    track means a crossing is only found if the whole passage fits inside one
    track -- and tracks end, so a slow walker or one who entered from the edge
    of frame loses their crossing at the seam. The rig replay found exactly
    that, three times out of ten.
    """

    def __init__(
        self,
        line: DoorLine,
        *,
        band: float = 0.03,
        min_samples_per_side: int = 2,
    ) -> None:
        if band <= 0:
            raise ValueError("band must be positive: a zero-width band has no hysteresis")
        if min_samples_per_side < 1:
            raise ValueError("min_samples_per_side must be at least 1")
        self.line = line
        self.band = band
        self.min_samples_per_side = min_samples_per_side

    def watcher(self) -> CrossingWatcher:
        return CrossingWatcher(self)

    def crossings(self, track: Track, frame_shape: tuple[int, int]) -> list[Crossing]:
        """Every crossing in a finished track.

        Feeds the track's samples to a fresh watcher, so the batch answer and
        the live answer are the same code and cannot drift apart.
        """
        watcher = self.watcher()
        found: list[Crossing] = []
        for sample in track.samples:
            found.extend(watcher.observe(track.track_id, sample, frame_shape))
        return found


@dataclass(slots=True)
class _Run:
    zone: Zone
    count: int
    score_sum: float
    first: TrackSample
    last: TrackSample

    def add(self, sample: TrackSample) -> None:
        self.count += 1
        self.score_sum += sample.score
        self.last = sample


@dataclass(slots=True)
class _TrackState:
    current: _Run | None = None
    previous: _Run | None = None
    emitted_for_current: bool = False
    crossings: int = 0


class CrossingWatcher:
    """Per-track zone runs and the transitions between them.

    Keeps a few counters per track id and nothing more. The ids are
    process-local and are forgotten when the track ends -- there is nothing
    here to persist even if somebody wanted to (ADR-0002).
    """

    def __init__(self, detector: DoorwayDetector) -> None:
        self._detector = detector
        self._state: dict[int, _TrackState] = {}

    def forget(self, track_id: int) -> None:
        self._state.pop(track_id, None)

    @property
    def tracked(self) -> int:
        return len(self._state)

    def observe(
        self, track_id: int, sample: TrackSample, frame_shape: tuple[int, int]
    ) -> list[Crossing]:
        height, width = frame_shape
        zone = self._detector.line.zone(sample.point, width, height, self._detector.band)
        if zone == "band":
            # Samples inside the band decide nothing. That is the hysteresis:
            # one jittery sample either side of a line is a phantom crossing,
            # and a phantom crossing is a deduction.
            return []
        state = self._state.setdefault(track_id, _TrackState())
        current = state.current
        if current is None:
            state.current = _Run(zone, 1, sample.score, sample, sample)
            return []
        if zone == current.zone:
            current.add(sample)
        else:
            state.previous = current
            state.current = _Run(zone, 1, sample.score, sample, sample)
            state.emitted_for_current = False
        return self._maybe_emit(track_id, state)

    def _maybe_emit(self, track_id: int, state: _TrackState) -> list[Crossing]:
        previous, current = state.previous, state.current
        if previous is None or current is None or state.emitted_for_current:
            return []
        need = self._detector.min_samples_per_side
        if previous.count < need or current.count < need:
            return []
        state.emitted_for_current = True
        state.crossings += 1
        direction: Direction = "enter" if current.zone == "inside" else "exit"
        reason = ""
        if state.crossings > 1:
            # Two transitions in one track is a reversal in a couple of seconds
            # or a detector artefact. Either way we should not claim a
            # direction: payroll routes ambiguous to zero and a flag.
            direction = "ambiguous"
            reason = f"crossing {state.crossings} in one track"
        # The person was in the band when they actually crossed; the midpoint
        # between the last decided sample on one side and the first on the other
        # is the least-wrong single instant available at this sample rate.
        span = current.first.ts_server - previous.last.ts_server
        samples = previous.count + current.count
        return [
            Crossing(
                ts_utc=previous.last.ts_server + span / 2,
                direction=direction,
                track_ref=track_id,
                samples_before=previous.count,
                samples_after=current.count,
                quality=round((previous.score_sum + current.score_sum) / samples, 4),
                reason=reason,
            )
        ]


def sampling_is_adequate(
    *,
    fps: float,
    band: float,
    frame_width: int,
    frame_height: int,
    max_speed_px_s: float,
    min_samples_per_side: int = 2,
) -> bool:
    """Can a walker be sampled often enough to clear the band on both sides?

    ``min_samples_per_side / fps * max_speed <= band_px``. Worth being able to
    compute rather than assume: on the rig at 640x360 with a 3% band the answer
    needs ~6 fps, and the default analysis_fps of 8 is uncomfortably close. On
    real 2304px footage the arithmetic has to be redone, not inherited.
    """
    band_px = band * math.hypot(frame_width, frame_height)
    travel_px = (min_samples_per_side / fps) * max_speed_px_s
    return travel_px <= band_px
