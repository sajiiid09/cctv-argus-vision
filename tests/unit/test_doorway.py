"""Doorway geometry: the sign convention, the band, and `ambiguous`.

The direction sign is the highest-consequence line in the pipeline. Inverted, it
turns every entry into an exit and leaves every downstream number looking
reasonable, so it is written out longhand here against the rig's convention
(canteen_door_01 faces east; a person moving east through the door is entering).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from argus.backends.types import Box
from argus.pipelines.doorway import DoorLine, DoorwayDetector, sampling_is_adequate
from argus.pipelines.tracking import Track, TrackSample

T0 = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)
FRAME = (360, 640)  # (height, width), matching the rig


def _vertical_line(inside_sign: int = 1) -> DoorLine:
    """A vertical line down the middle, as config/dev.yaml sets for the rig."""
    return DoorLine(x1=0.5, y1=0.0, x2=0.5, y2=1.0, inside_sign=inside_sign)


def _track(xs: list[float], *, start: datetime = T0, step_s: float = 0.1) -> Track:
    track = Track(track_id=7)
    for i, x in enumerate(xs):
        box = Box(x1=x - 10, y1=100.0, x2=x + 10, y2=200.0)
        track.samples.append(
            TrackSample(
                ts_server=start + timedelta(seconds=i * step_s),
                point=(x, 200.0),
                box=box,
                score=0.9,
            )
        )
    return track


def test_a_walk_east_through_the_line_is_an_enter() -> None:
    """With inside_sign +1 and a vertical line, increasing x enters.

    Written out in full: the person starts west of the door (x=200), ends east
    of it (x=440), and that is an `enter`. If this test is ever "fixed" by
    flipping the expectation, every dwell interval in the system inverts.
    """
    detector = DoorwayDetector(_vertical_line(), band=0.03, min_samples_per_side=2)
    crossings = detector.crossings(_track([200, 240, 280, 400, 440, 480]), FRAME)
    assert [c.direction for c in crossings] == ["enter"]


def test_the_same_walk_west_is_an_exit() -> None:
    detector = DoorwayDetector(_vertical_line(), band=0.03, min_samples_per_side=2)
    crossings = detector.crossings(_track([480, 440, 400, 280, 240, 200]), FRAME)
    assert [c.direction for c in crossings] == ["exit"]


def test_inside_sign_flips_the_meaning_and_nothing_else() -> None:
    walk = _track([200, 240, 280, 400, 440, 480])
    assert DoorwayDetector(_vertical_line(1)).crossings(walk, FRAME)[0].direction == "enter"
    assert DoorwayDetector(_vertical_line(-1)).crossings(walk, FRAME)[0].direction == "exit"


def test_the_crossing_time_sits_between_the_two_sides() -> None:
    detector = DoorwayDetector(_vertical_line(), band=0.03, min_samples_per_side=2)
    crossing = detector.crossings(_track([200, 280, 400, 480]), FRAME)[0]
    assert T0 < crossing.ts_utc < T0 + timedelta(seconds=0.4)


def test_one_sample_each_side_is_not_a_crossing() -> None:
    """A single sample either side is a detector jitter away from a phantom
    crossing, and a phantom crossing is a deduction."""
    detector = DoorwayDetector(_vertical_line(), band=0.03, min_samples_per_side=2)
    assert detector.crossings(_track([300, 340]), FRAME) == []


def test_samples_inside_the_band_decide_nothing() -> None:
    line = _vertical_line()
    band = 0.03
    detector = DoorwayDetector(line, band=band, min_samples_per_side=2)
    # A track that only ever loiters in the doorway produces no crossing at all.
    assert detector.crossings(_track([318, 320, 322, 320]), FRAME) == []


def test_a_reversal_inside_one_track_is_ambiguous_not_a_best_guess() -> None:
    """One track is one person passing the door once.

    Two or more transitions within a couple of seconds is a reversal or a
    detector artefact; either way we should not claim a direction, and payroll
    routes ambiguous to zero and a flag.
    """
    detector = DoorwayDetector(_vertical_line(), band=0.03, min_samples_per_side=2)
    crossings = detector.crossings(_track([200, 240, 400, 440, 200, 240, 400, 440]), FRAME)
    assert [c.direction for c in crossings] == ["enter", "ambiguous", "ambiguous"]
    assert "in one track" in crossings[1].reason


def test_a_crossing_is_reported_as_soon_as_the_far_side_is_confirmed() -> None:
    """Incremental, not at track end.

    A track that ends later -- or never, for a slow walker -- must not swallow
    the crossing. The rig replay lost three of ten crossings to exactly that
    before this became incremental.
    """
    detector = DoorwayDetector(_vertical_line(), band=0.03, min_samples_per_side=2)
    watcher = detector.watcher()
    found = []
    for sample in _track([200, 240, 280, 400, 440, 480, 520, 560]).samples:
        found.extend(watcher.observe(7, sample, FRAME))
    assert [c.direction for c in found] == ["enter"]
    # reported on the second sample past the line, not on the eighth
    assert found[0].samples_after == 2
    watcher.forget(7)
    assert watcher.tracked == 0


def test_the_line_is_fractions_so_resolution_does_not_change_the_answer() -> None:
    """The main and analysis streams differ in resolution (ADR-0031)."""
    detector = DoorwayDetector(_vertical_line(), band=0.03, min_samples_per_side=2)
    small = detector.crossings(_track([200, 240, 400, 440]), (360, 640))
    big = detector.crossings(_track([720, 864, 1440, 1584]), (1296, 2304))
    assert [c.direction for c in small] == [c.direction for c in big] == ["enter"]


def test_signed_distance_is_normalised_by_the_diagonal() -> None:
    line = _vertical_line()
    near = line.signed_distance((330, 180), 640, 360)
    far = line.signed_distance((640, 180), 640, 360)
    assert 0 < near < far
    # same fractional offset, different resolution: same normalised distance
    assert line.signed_distance((330, 180), 640, 360) == pytest.approx(
        line.signed_distance((1188, 648), 2304, 1296), rel=1e-6
    )


def test_a_degenerate_line_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="endpoints must differ"):
        DoorLine(x1=0.5, y1=0.5, x2=0.5, y2=0.5)
    with pytest.raises(ValueError, match="fractions of the frame"):
        DoorLine(x1=320.0, y1=0.0, x2=320.0, y2=360.0)
    with pytest.raises(ValueError, match="inside_sign"):
        DoorLine(x1=0.4, y1=0.0, x2=0.6, y2=1.0, inside_sign=0)
    with pytest.raises(ValueError, match="band must be positive"):
        DoorwayDetector(_vertical_line(), band=0.0)


def test_sampling_adequacy_is_computable_not_assumed() -> None:
    """min_samples_per_side / fps * speed <= band_px.

    On the rig (640x360, 3% band, ~70 px/s walkers) two samples a side need
    about 6 fps, so the 8 fps default is uncomfortably close and the rig config
    uses 15.
    """
    common = {"band": 0.03, "frame_width": 640, "frame_height": 360, "max_speed_px_s": 70.0}
    assert sampling_is_adequate(fps=15.0, **common)
    assert not sampling_is_adequate(fps=4.0, **common)
