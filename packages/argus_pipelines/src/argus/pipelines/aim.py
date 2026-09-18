"""Has the camera moved? (ADR-0029)

The doorway pipeline's line is a fixed configuration constant, so a camera that
gets nudged, re-aimed by a supervisor, or knocked by a cleaner keeps producing
confident crossings against a line that no longer matches the door. That is a
silent wrong answer, and it is the failure mode ADR-0029 exists for.

Detection is FFT phase correlation against a stored reference frame, at 160x90
greyscale: cheap enough to run every 30 seconds, and insensitive to lighting
because the phase of the cross-power spectrum ignores magnitude.

The reference frame is **never updated automatically**. An auto-updating
reference tracks the drift it exists to detect, one small step at a time.
Recalibration is a deliberate write with a named human on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import numpy as np

log = logging.getLogger(__name__)

State = Literal["ok", "drifting", "drifted", "recovered"]

REFERENCE_WIDTH = 160
REFERENCE_HEIGHT = 90


@dataclass(frozen=True, slots=True)
class AimOffset:
    dx: float
    dy: float

    @property
    def magnitude(self) -> float:
        return float(np.hypot(self.dx, self.dy))


def to_reference(frame: np.ndarray) -> np.ndarray:
    """Greyscale, 160x90, float64. Deterministic and tiny."""
    from argus.backends.preprocessing import resize_bilinear

    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError(f"expected HxWx3, got {frame.shape}")
    small = resize_bilinear(frame, REFERENCE_WIDTH, REFERENCE_HEIGHT)
    return small.mean(axis=2).astype(np.float64)


def phase_correlate(reference: np.ndarray, current: np.ndarray) -> AimOffset:
    """Offset of `current` relative to `reference`, in reference pixels.

    A Hann window on both inputs, because an unwindowed FFT of a frame with
    strong edges at the border reports a spurious peak at zero shift -- which
    would make drift detection quietly always say "fine".
    """
    if reference.shape != current.shape:
        raise ValueError(f"shape mismatch: {reference.shape} vs {current.shape}")
    window = np.hanning(reference.shape[0])[:, None] * np.hanning(reference.shape[1])[None, :]
    a = np.fft.rfft2((reference - reference.mean()) * window)
    b = np.fft.rfft2((current - current.mean()) * window)
    cross = a * np.conj(b)
    magnitude = np.abs(cross)
    magnitude[magnitude == 0] = 1e-12
    correlation = np.fft.irfft2(cross / magnitude, s=reference.shape)
    peak = np.unravel_index(int(np.argmax(correlation)), correlation.shape)
    dy = float(peak[0])
    dx = float(peak[1])
    # Wrap to signed offsets: a peak at N-1 is -1, not +(N-1).
    if dy > reference.shape[0] / 2:
        dy -= reference.shape[0]
    if dx > reference.shape[1] / 2:
        dx -= reference.shape[1]
    return AimOffset(dx=dx, dy=dy)


class AimMonitor:
    def __init__(
        self,
        reference: np.ndarray,
        *,
        max_offset_px: float = 4.0,
        consecutive: int = 3,
    ) -> None:
        self.reference = to_reference(reference) if reference.ndim == 3 else reference
        self.max_offset_px = max_offset_px
        self.consecutive = consecutive
        self._strikes = 0
        self._drifted = False
        self.last_offset = AimOffset(0.0, 0.0)

    @property
    def drifted(self) -> bool:
        return self._drifted

    def check(self, frame: np.ndarray) -> State:
        """One measurement. Hysteresis of `consecutive` before declaring drift.

        Three strikes rather than one because a lorry parked in frame, a crowd,
        or a light switching on all move the correlation peak briefly, and a
        pipeline that stops measuring on every one of those is a pipeline
        nobody leaves switched on.
        """
        offset = phase_correlate(self.reference, to_reference(frame))
        self.last_offset = offset
        if offset.magnitude > self.max_offset_px:
            self._strikes += 1
            if self._strikes >= self.consecutive and not self._drifted:
                self._drifted = True
                log.warning(
                    "camera aim drifted by %.1fpx (dx=%.1f dy=%.1f) over %d checks",
                    offset.magnitude,
                    offset.dx,
                    offset.dy,
                    self._strikes,
                )
                return "drifted"
            return "drifted" if self._drifted else "drifting"
        self._strikes = 0
        if self._drifted:
            self._drifted = False
            log.info("camera aim back within %.1fpx of its reference", self.max_offset_px)
            return "recovered"
        return "ok"
