"""The four narrow interfaces (ARCHITECTURE.md §5.1).

Rules, and they are not stylistic:
1. Application code never imports a backend; it asks the registry.
2. Selection is by capability detection, never by platform check.
3. Backend choice is loggable (model hash) and overridable by config.
4. Interfaces stay narrow: no backend-specific options in signatures.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np
from argus.backends.types import Detection, Embedding

Frame = np.ndarray  # rgb24, HxWx3, uint8


@runtime_checkable
class Detector(Protocol):
    name: str
    model_ref: str  # artefact name + short hash, e.g. "ssd_mobilenet_v1@a1b2c3d4"

    def detect(self, frame: Frame) -> list[Detection]: ...


@runtime_checkable
class PoseEstimator(Protocol):
    name: str
    model_ref: str

    def estimate(self, frame: Frame) -> list[np.ndarray]:
        """One (K,3) array [x, y, confidence] per detected person."""
        ...


@runtime_checkable
class FaceEmbedder(Protocol):
    name: str
    model_ref: str

    def embed(self, aligned_crop: Frame) -> Embedding: ...


@runtime_checkable
class ClipClassifier(Protocol):
    name: str
    model_ref: str

    def classify(self, frames: list[Frame]) -> dict[str, float]: ...
