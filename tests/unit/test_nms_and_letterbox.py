"""NMS and letterboxing: determinism and the inverse mapping.

Both are shared by every detector wrapper, and both are places where a
plausible-looking implementation produces boxes that are slightly wrong
everywhere. Ties in particular: numpy's argsort order is not part of its
contract, so equal scores must be broken explicitly or the same frame can
produce different output on different platforms.
"""

from __future__ import annotations

import numpy as np
import pytest
from argus.backends.nms import nms
from argus.backends.preprocessing import letterbox, resize_bilinear


def test_nms_suppresses_overlaps_and_keeps_distinct_boxes() -> None:
    boxes = np.array([[0.0, 0.0, 10.0, 10.0], [1.0, 1.0, 11.0, 11.0], [100.0, 100.0, 110.0, 110.0]])
    scores = np.array([0.9, 0.8, 0.7])
    assert nms(boxes, scores, 0.45) == [0, 2]


def test_nms_breaks_ties_by_position_not_by_argsort() -> None:
    boxes = np.array([[50.0, 50.0, 60.0, 60.0], [0.0, 5.0, 10.0, 15.0], [0.0, 0.0, 10.0, 10.0]])
    scores = np.array([0.5, 0.5, 0.5])
    # equal scores: lowest y1 first, then lowest x1
    assert nms(boxes, scores, 0.9) == [2, 1, 0]


def test_nms_handles_the_empty_case_and_bad_shapes() -> None:
    assert nms(np.zeros((0, 4)), np.zeros(0)) == []
    with pytest.raises(ValueError, match=r"\(N, 4\)"):
        nms(np.zeros((3, 5)), np.zeros(3))
    with pytest.raises(ValueError, match="scores"):
        nms(np.zeros((3, 4)), np.zeros(2))


def test_letterbox_preserves_aspect_and_returns_the_inverse_mapping() -> None:
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    tensor, scale, pad_x, pad_y = letterbox(frame, 640, 640)
    assert tensor.shape == (1, 3, 640, 640)
    assert scale == pytest.approx(1.0)
    assert (pad_x, pad_y) == (0.0, 140.0)
    # a point at the top of the source maps to pad_y in model space and back
    model_y = 0.0 * scale + pad_y
    assert (model_y - pad_y) / scale == pytest.approx(0.0)


def test_letterbox_scales_down_a_large_frame() -> None:
    frame = np.zeros((1296, 2304, 3), dtype=np.uint8)
    _, scale, pad_x, pad_y = letterbox(frame, 640, 640)
    assert scale == pytest.approx(640 / 2304)
    assert pad_x == 0.0
    assert pad_y == pytest.approx((640 - round(1296 * scale)) // 2)


def test_letterbox_padding_is_neutral_grey_and_normalised() -> None:
    frame = np.full((100, 200, 3), 255, dtype=np.uint8)
    tensor, _, _, pad_y = letterbox(frame, 200, 200, pad_value=114)
    assert tensor.max() == pytest.approx(1.0)
    assert tensor[0, 0, 0, 0] == pytest.approx(114 / 255.0)
    assert tensor[0, 0, int(pad_y) + 5, 5] == pytest.approx(1.0)


def test_letterbox_uses_the_same_kernel_as_resize_bilinear() -> None:
    """Preprocessing parity across platforms starts here (ARCHITECTURE.md §5.3)."""
    frame = (np.arange(60 * 80 * 3, dtype=np.uint8).reshape(60, 80, 3)) % 255
    tensor, scale, pad_x, pad_y = letterbox(frame, 160, 160)
    direct = resize_bilinear(frame, round(80 * scale), round(60 * scale))
    x0, y0 = int(pad_x), int(pad_y)
    inner = tensor[0, :, y0 : y0 + direct.shape[0], x0 : x0 + direct.shape[1]]
    assert np.allclose(inner, direct.transpose(2, 0, 1) / 255.0, atol=1e-6)


def test_resize_bilinear_handles_non_square_output() -> None:
    """A regression test with a story: fy broadcast only for square frames.

    resize_bilinear had no callers -- SSD feeds frames at native resolution --
    so the bug sat there until letterbox() needed it. Non-square in and out,
    both directions, is the case that catches it.
    """
    frame = (np.arange(40 * 90 * 3, dtype=np.uint8).reshape(40, 90, 3)) % 255
    assert resize_bilinear(frame, 30, 70).shape == (70, 30, 3)
    assert resize_bilinear(frame, 120, 20).shape == (20, 120, 3)
    flat = np.full((10, 20, 3), 77, dtype=np.uint8)
    assert np.allclose(resize_bilinear(flat, 33, 7), 77.0)
