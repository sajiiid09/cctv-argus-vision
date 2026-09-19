"""Alignment: the transform, the landmark order, and what it refuses.

Alignment failures are silent. A sheared face or a permuted landmark order
produces a crop that looks like a face and an embedding that matches nobody, so
these are the properties worth pinning down before any threshold is measured.
"""

from __future__ import annotations

import numpy as np
import pytest
from argus.backends.face_align import (
    ARCFACE_112_TEMPLATE,
    align_face,
    resize_crop,
    umeyama_similarity,
)
from argus.backends.types import LANDMARK_ORDER


def _frame(width: int = 200, height: int = 200) -> np.ndarray:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    # a gradient, so a rotation or a flip is visible in the output
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    frame[:, :, 1] = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    frame[:, :, 2] = 64
    return frame


def test_landmark_order_is_documented_and_five_long() -> None:
    assert LANDMARK_ORDER == ("left_eye", "right_eye", "nose", "left_mouth", "right_mouth")
    assert ARCFACE_112_TEMPLATE.shape == (5, 2)


def test_umeyama_recovers_a_known_similarity() -> None:
    src = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.5, 0.5]])
    angle = np.deg2rad(30.0)
    rotation = np.array(
        [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]], dtype=np.float64
    )
    scale, offset = 2.5, np.array([7.0, -3.0])
    dst = (scale * src @ rotation.T) + offset
    matrix = umeyama_similarity(src, dst)
    recovered = (src @ matrix[:, :2].T) + matrix[:, 2]
    assert np.allclose(recovered, dst, atol=1e-4)
    # Similarity, not affine: the linear part is a scaled rotation, so its two
    # singular values are equal. An affine fit would shear.
    singulars = np.linalg.svd(matrix[:, :2], compute_uv=False)
    assert singulars[0] == pytest.approx(singulars[1], rel=1e-5)
    assert singulars[0] == pytest.approx(scale, rel=1e-5)


def test_align_face_returns_the_canonical_crop() -> None:
    frame = _frame()
    landmarks = ARCFACE_112_TEMPLATE + np.array([40.0, 30.0], dtype=np.float32)
    crop = align_face(frame, landmarks)
    assert crop.shape == (112, 112, 3) and crop.dtype == np.uint8
    # The landmarks were the template plus a pure translation, so aligning must
    # undo exactly that translation and nothing else.
    expected = frame[30:142, 40:152]
    assert np.abs(crop.astype(int) - expected.astype(int)).mean() < 2.0


def test_alignment_is_deterministic() -> None:
    frame = _frame()
    landmarks = ARCFACE_112_TEMPLATE * 1.3 + np.array([10.0, 20.0], dtype=np.float32)
    first = align_face(frame, landmarks)
    second = align_face(frame, landmarks)
    assert np.array_equal(first, second)


def test_a_permuted_landmark_order_produces_a_different_crop() -> None:
    """The guard behind the LANDMARK_ORDER contract.

    Swapping the eyes is the plausible mistake -- some detectors emit them the
    other way round -- and this asserts it cannot pass unnoticed.
    """
    frame = _frame()
    landmarks = (ARCFACE_112_TEMPLATE * 1.2 + np.array([25.0, 25.0])).astype(np.float32)
    swapped = landmarks[[1, 0, 2, 3, 4]]
    assert not np.array_equal(align_face(frame, landmarks), align_face(frame, swapped))


def test_align_face_refuses_bad_input() -> None:
    frame = _frame()
    with pytest.raises(ValueError, match="landmarks"):
        align_face(frame, np.zeros((3, 2), dtype=np.float32))
    with pytest.raises(ValueError, match="uint8"):
        align_face(frame.astype(np.float32), ARCFACE_112_TEMPLATE)


def test_resize_crop_exists_separately_from_alignment() -> None:
    """A loose crop is not comparable to an enrolled template, so it is not
    reachable by accident -- different name, different function."""
    crop = resize_crop(_frame(60, 90))
    assert crop.shape == (112, 112, 3) and crop.dtype == np.uint8
