"""Shared value types for inference results."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

Embedding = np.ndarray  # float32, L2-normalised, shape (d,)


@dataclass(frozen=True, slots=True)
class Box:
    x1: float
    y1: float
    x2: float
    y2: float

    def iou(self, other: Box) -> float:
        ix1, iy1 = max(self.x1, other.x1), max(self.y1, other.y1)
        ix2, iy2 = min(self.x2, other.x2), min(self.y2, other.y2)
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        area_a = (self.x2 - self.x1) * (self.y2 - self.y1)
        area_b = (other.x2 - other.x1) * (other.y2 - other.y1)
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0


@dataclass(frozen=True, slots=True)
class Detection:
    box: Box
    score: float
    label: str
    label_id: int


# The five landmarks, in this order, are a load-bearing contract between the
# face detector and face_align: a permuted order produces aligned crops that
# look plausible and embeddings that are garbage, with no error anywhere.
LANDMARK_ORDER = ("left_eye", "right_eye", "nose", "left_mouth", "right_mouth")


@dataclass(frozen=True, slots=True)
class FaceDetection:
    box: Box
    score: float
    # (5, 2) float32 in SOURCE pixel coordinates, ordered as LANDMARK_ORDER.
    landmarks: np.ndarray

    def __post_init__(self) -> None:
        if self.landmarks.shape != (5, 2):
            raise ValueError(f"landmarks must be (5, 2), got {self.landmarks.shape}")
