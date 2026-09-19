"""The mock backends, which every pipeline test depends on.

They are not a convenience: they are what lets the doorway geometry, the
tracker and the face stage be tested with no fetched artefacts at all. So their
determinism is a property worth asserting, and their limits are worth writing
down -- they detect bright disks, and the rig draws people as bright disks.
"""

from __future__ import annotations

import numpy as np
import pytest
from argus.backends.face_align import align_face
from argus.backends.mock import (
    MockDiskDetector,
    MockFaceDetector,
    MockFaceEmbedder,
    MockPoseEstimator,
)


def _disk(frame: np.ndarray, cx: int, cy: int, radius: int = 16, value: int = 180) -> np.ndarray:
    ys, xs = np.ogrid[: frame.shape[0], : frame.shape[1]]
    frame[(xs - cx) ** 2 + (ys - cy) ** 2 <= radius**2] = value
    return frame


def _scene(*centres: tuple[int, int]) -> np.ndarray:
    frame = np.full((180, 320, 3), 44, dtype=np.uint8)
    for cx, cy in centres:
        _disk(frame, cx, cy)
    return frame


def test_detector_finds_one_box_per_disk() -> None:
    dets = MockDiskDetector().detect(_scene((80, 90), (240, 90)))
    assert len(dets) == 2
    assert [round(d.box.x1) for d in dets] == [64, 224]
    assert all(d.label == "disk" for d in dets)


def test_detector_is_deterministic_and_honours_the_threshold() -> None:
    scene = _scene((80, 90))
    detector = MockDiskDetector()
    assert detector.detect(scene) == detector.detect(scene)
    assert detector.detect(scene, score_threshold=0.99) == []


def test_detector_ignores_a_dim_door_line() -> None:
    """The rig draws its door line at (110, 110, 110), below the cutoff.

    The replay test depends on that, so it is asserted here rather than relied
    on: a line bright enough to cluster would become a permanent extra person
    standing in the doorway.
    """
    frame = np.full((180, 320, 3), 44, dtype=np.uint8)
    frame[:, 158:162] = 110
    assert MockDiskDetector().detect(frame) == []


def test_face_detector_returns_landmarks_that_align() -> None:
    scene = _scene((160, 90))
    faces = MockFaceDetector().detect_faces(scene)
    assert len(faces) == 1
    face = faces[0]
    assert face.landmarks.shape == (5, 2)
    # eyes level and above the nose, nose above the mouth: enough structure for
    # alignment to produce a stable crop
    assert face.landmarks[0, 1] == pytest.approx(face.landmarks[1, 1])
    assert face.landmarks[0, 1] < face.landmarks[2, 1] < face.landmarks[3, 1]
    crop = align_face(scene, face.landmarks)
    assert crop.shape == (112, 112, 3)


def test_embedder_is_deterministic_normalised_and_crop_sensitive() -> None:
    embedder = MockFaceEmbedder()
    first = embedder.embed(np.full((112, 112, 3), 120, dtype=np.uint8))
    again = embedder.embed(np.full((112, 112, 3), 120, dtype=np.uint8))
    other = embedder.embed(np.full((112, 112, 3), 121, dtype=np.uint8))
    assert np.array_equal(first, again)
    assert float(np.linalg.norm(first)) == pytest.approx(1.0, abs=1e-6)
    # cosine similarity is a dot product because the vectors are normalised
    assert abs(float(first @ other)) < 0.5


def test_pose_estimator_returns_seventeen_keypoints_per_person() -> None:
    people = MockPoseEstimator().estimate(_scene((80, 90), (240, 90)))
    assert len(people) == 2
    for kpts in people:
        assert kpts.shape == (17, 3)
        # head above hips above ankles
        assert kpts[0, 1] < kpts[11, 1] < kpts[15, 1]


def test_clustering_is_fast_enough_for_a_replay() -> None:
    """The previous formulation walked a Python set per bright pixel.

    Two disks are ~1600 bright pixels, which was ~2.5M iterations per frame and
    made a 75-second rig replay unusable. This is a floor, not a benchmark: it
    only fails if the vectorised version is replaced by something quadratic
    again.
    """
    import time

    scene = _scene((60, 60), (160, 90), (260, 120))
    detector = MockDiskDetector()
    started = time.perf_counter()
    for _ in range(20):
        detector.detect(scene)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"20 frames took {elapsed:.2f}s"
