"""Mock backends: pure-numpy stand-ins for tests and capability demos.

Explicitly selectable only (registry name "mock" for every kind); never in the
default preference order, because silently serving mock detections would be
worse than failing. Deterministic by construction -- no RNG anywhere, so the
rig-replay comparison is reproducible.

These are what let the whole canteen pipeline, the doorway geometry and the
face-stage plumbing be tested with no fetched artefacts at all. What they cannot
do is say anything about accuracy: they detect bright disks on the synthetic
rig, and the rig renders people as bright disks.
"""

from __future__ import annotations

import hashlib

import numpy as np
from argus.backends.types import Box, Detection, Embedding, FaceDetection

BRIGHTNESS_CUTOFF = 120.0
CLUSTER_RADIUS = 40
MIN_CLUSTER_PIXELS = 80


class MockDiskDetector:
    name = "mock"
    model_ref = "mock@000000000000"
    kind = "detector"

    def detect(self, frame: np.ndarray, *, score_threshold: float = 0.5) -> list[Detection]:
        gray = _gray(frame)
        detections: list[Detection] = []
        for x1, y1, x2, y2 in _clusters(gray):
            brightness = float(gray[y1 : y2 + 1, x1 : x2 + 1].mean()) / 255.0
            if brightness < score_threshold:
                continue
            detections.append(
                Detection(
                    box=Box(float(x1), float(y1), float(x2), float(y2)),
                    score=round(brightness, 4),
                    label="disk",
                    label_id=1,
                )
            )
        return detections


class MockFaceDetector:
    """One "face" in the upper third of each bright cluster, with landmarks.

    The geometry is fake but consistent: eyes above nose above mouth, spaced by
    the cluster width, so alignment produces a stable crop and a test can assert
    that the landmark order survived the round trip.
    """

    name = "mock"
    model_ref = "mock-face@000000000000"
    kind = "face_detector"

    def detect_faces(
        self, frame: np.ndarray, *, score_threshold: float = 0.5
    ) -> list[FaceDetection]:
        gray = _gray(frame)
        faces: list[FaceDetection] = []
        for x1, y1, x2, y2 in _clusters(gray):
            width = x2 - x1
            height = y2 - y1
            if width < 8 or height < 8:
                continue
            face_h = max(6, height // 3)
            fx1, fy1, fx2, fy2 = float(x1), float(y1), float(x2), float(y1 + face_h)
            score = round(float(gray[y1 : y1 + face_h + 1, x1 : x2 + 1].mean()) / 255.0, 4)
            if score < score_threshold:
                continue
            cx = (fx1 + fx2) / 2.0
            eye_dx = max(2.0, width / 5.0)
            eye_y = fy1 + face_h * 0.35
            landmarks = np.array(
                [
                    [cx - eye_dx, eye_y],
                    [cx + eye_dx, eye_y],
                    [cx, fy1 + face_h * 0.55],
                    [cx - eye_dx * 0.8, fy1 + face_h * 0.8],
                    [cx + eye_dx * 0.8, fy1 + face_h * 0.8],
                ],
                dtype=np.float32,
            )
            faces.append(
                FaceDetection(box=Box(fx1, fy1, fx2, fy2), score=score, landmarks=landmarks)
            )
        return faces


class MockFaceEmbedder:
    """A deterministic embedding derived from the crop bytes.

    Two identical crops embed identically and two different crops almost never
    match, which is the only property the face-stage logic needs in order to be
    testable. It says nothing about whether two *photographs of one person*
    match -- that is exactly the question a mock cannot answer, and the reason
    thresholds stay unset until they are measured.
    """

    name = "mock"
    model_ref = "mock-embed@000000000000"
    kind = "face_embedder"
    dim = 64

    def embed(self, aligned_crop: np.ndarray) -> Embedding:
        if aligned_crop.ndim != 3 or aligned_crop.shape[2] != 3 or aligned_crop.dtype != np.uint8:
            raise ValueError(f"expected HxWx3 uint8 crop, got {aligned_crop.shape}")
        digest = hashlib.sha256(np.ascontiguousarray(aligned_crop).tobytes()).digest()
        raw = np.frombuffer(digest * 8, dtype=np.uint8)[: self.dim].astype(np.float32)
        raw = raw - raw.mean()
        norm = float(np.linalg.norm(raw))
        if norm == 0.0:
            raw = np.ones(self.dim, dtype=np.float32)
            norm = float(np.linalg.norm(raw))
        return (raw / norm).astype(np.float32)


class MockPoseEstimator:
    """Seventeen keypoints laid out over each bright cluster.

    Enough structure for the violence trigger's features -- proximity between
    people, limb extension, pose energy between frames -- to be computed and
    unit-tested without a pose artefact.
    """

    name = "mock"
    model_ref = "mock-pose@000000000000"
    kind = "pose"

    def estimate(self, frame: np.ndarray) -> list[np.ndarray]:
        gray = _gray(frame)
        people: list[np.ndarray] = []
        for x1, y1, x2, y2 in _clusters(gray):
            width = max(1.0, float(x2 - x1))
            height = max(1.0, float(y2 - y1))
            cx = (x1 + x2) / 2.0
            # fractions of the box: head, shoulders, elbows, wrists, hips, knees,
            # ankles -- in COCO keypoint order.
            layout = [
                (0.0, 0.05),
                (-0.1, 0.03),
                (0.1, 0.03),
                (-0.2, 0.05),
                (0.2, 0.05),
                (-0.3, 0.25),
                (0.3, 0.25),
                (-0.4, 0.4),
                (0.4, 0.4),
                (-0.45, 0.55),
                (0.45, 0.55),
                (-0.2, 0.6),
                (0.2, 0.6),
                (-0.2, 0.8),
                (0.2, 0.8),
                (-0.2, 0.98),
                (0.2, 0.98),
            ]
            pts = np.array(
                [[cx + dx * width, y1 + dy * height, 0.9] for dx, dy in layout],
                dtype=np.float32,
            )
            people.append(pts)
        return people


def _gray(frame: np.ndarray) -> np.ndarray:
    if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
        raise ValueError(f"expected HxWx3 uint8, got {frame.shape} {frame.dtype}")
    return frame.mean(axis=2)


def _clusters(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of bright blobs, in raster scan order of their seeds.

    A blob is every unclaimed bright pixel within CLUSTER_RADIUS of the seed, in
    both axes. Vectorised deliberately: the previous formulation walked a Python
    set per pixel, which is ~2.5M iterations for two disks and made a 75-second
    rig replay unusable. The boxes it produces are identical -- the golden
    frames assert exact coordinates.
    """
    mask = gray > BRIGHTNESS_CUTOFF
    if not mask.any():
        return []
    ys, xs = np.nonzero(mask)  # raster order, which fixes the seed order
    taken = np.zeros(len(xs), dtype=bool)
    boxes: list[tuple[int, int, int, int]] = []
    while True:
        free = np.nonzero(~taken)[0]
        if len(free) == 0:
            break
        seed = int(free[0])
        sx, sy = int(xs[seed]), int(ys[seed])
        near = (~taken) & (np.abs(xs - sx) < CLUSTER_RADIUS) & (np.abs(ys - sy) < CLUSTER_RADIUS)
        taken |= near
        count = int(near.sum())
        if count < MIN_CLUSTER_PIXELS:
            continue
        cxs = xs[near]
        cys = ys[near]
        boxes.append((int(cxs.min()), int(cys.min()), int(cxs.max()), int(cys.max())))
    return boxes
