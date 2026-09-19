"""Aim drift: detect the camera moving, and refuse to paper over it.

ADR-0029: the doorway line is a fixed config constant, so a camera that gets
nudged keeps producing confident crossings against a line that no longer matches
the door. That is a silent wrong answer, which is the whole reason this exists.
"""

from __future__ import annotations

import numpy as np
import pytest
from argus.pipelines.aim import AimMonitor, phase_correlate, to_reference


def _scene(shift_x: int = 0, shift_y: int = 0) -> np.ndarray:
    """A textured frame, optionally translated. Texture matters: phase
    correlation on a flat image has no peak to find."""
    rng = np.random.default_rng(7)
    base = rng.integers(0, 255, size=(360, 640, 3), dtype=np.uint8)
    return np.roll(np.roll(base, shift_y, axis=0), shift_x, axis=1)


def test_a_still_camera_reads_as_no_offset() -> None:
    reference = _scene()
    offset = phase_correlate(to_reference(reference), to_reference(reference))
    assert offset.magnitude == pytest.approx(0.0)


def test_a_translated_frame_reads_as_an_offset() -> None:
    reference = to_reference(_scene())
    current = to_reference(_scene(shift_x=32))
    offset = phase_correlate(reference, current)
    # 32 source pixels of 640 is 8 reference pixels of 160
    assert offset.magnitude >= 4.0
    assert abs(offset.dx) > abs(offset.dy)


def test_drift_needs_three_consecutive_strikes() -> None:
    """A lorry parked in frame moves the peak briefly. A pipeline that stops
    measuring on every one of those is a pipeline nobody leaves switched on."""
    monitor = AimMonitor(_scene(), max_offset_px=2.0, consecutive=3)
    moved = _scene(shift_x=40)
    assert monitor.check(moved) == "drifting"
    assert monitor.check(moved) == "drifting"
    assert monitor.check(moved) == "drifted"
    assert monitor.drifted is True


def test_one_bad_frame_does_not_trip_it() -> None:
    monitor = AimMonitor(_scene(), max_offset_px=2.0, consecutive=3)
    assert monitor.check(_scene(shift_x=40)) == "drifting"
    assert monitor.check(_scene()) == "ok"
    assert monitor.check(_scene(shift_x=40)) == "drifting"
    assert monitor.drifted is False


def test_recovery_is_reported_so_the_gap_can_close() -> None:
    monitor = AimMonitor(_scene(), max_offset_px=2.0, consecutive=1)
    assert monitor.check(_scene(shift_x=40)) == "drifted"
    assert monitor.check(_scene()) == "recovered"
    assert monitor.drifted is False


def test_the_reference_is_never_updated_automatically() -> None:
    """An auto-updating reference tracks the drift it exists to detect."""
    monitor = AimMonitor(_scene(), max_offset_px=2.0, consecutive=1)
    before = monitor.reference.copy()
    moved = _scene(shift_x=40)
    for _ in range(5):
        monitor.check(moved)
    assert np.array_equal(monitor.reference, before)


def test_the_reference_is_small_and_grey() -> None:
    small = to_reference(_scene())
    assert small.shape == (90, 160)
    assert small.dtype == np.float64
