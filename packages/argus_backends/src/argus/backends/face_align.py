"""Five-point face alignment, pure numpy.

The alignment template is a property of the embedder -- a different embedder
wants a different one -- which is why this lives behind the backend abstraction
rather than in the pipelines package.

Similarity transform only: scale, rotation, translation. An affine fit to five
noisy landmarks shears the face, and the embedding degrades without anything
failing, which is the worst kind of wrong.
"""

from __future__ import annotations

import numpy as np
from argus.backends.preprocessing import resize_bilinear

# ArcFace's canonical 112x112 landmark positions (left eye, right eye, nose,
# left mouth corner, right mouth corner), as published with the InsightFace
# recognition models. Order matches argus.backends.types.LANDMARK_ORDER.
ARCFACE_112_TEMPLATE = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)


def umeyama_similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares similarity transform src -> dst as a (2, 3) matrix.

    Umeyama's estimate: centre both sets, take the SVD of the cross-covariance,
    and allow a reflection-correcting sign flip. Scale is a single scalar, so
    the result cannot shear.
    """
    if src.shape != dst.shape or src.ndim != 2 or src.shape[1] != 2:
        raise ValueError(f"expected matching (N, 2) point sets, got {src.shape} and {dst.shape}")
    src = src.astype(np.float64)
    dst = dst.astype(np.float64)
    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_c = src - src_mean
    dst_c = dst - dst_mean
    cov = dst_c.T @ src_c / src.shape[0]
    u, s, vt = np.linalg.svd(cov)
    d = np.sign(np.linalg.det(u @ vt))
    correction = np.diag([1.0, d])
    rotation = u @ correction @ vt
    src_var = src_c.var(axis=0).sum()
    scale = 1.0 if src_var == 0 else float((s * np.array([1.0, d])).sum() / src_var)
    translation = dst_mean - scale * rotation @ src_mean
    matrix = np.zeros((2, 3), dtype=np.float32)
    matrix[:, :2] = scale * rotation
    matrix[:, 2] = translation
    return matrix


def align_face(frame: np.ndarray, landmarks: np.ndarray, size: int = 112) -> np.ndarray:
    """Warp a face to the canonical `size` x `size` crop, uint8 rgb.

    Inverse mapping with bilinear sampling: for each output pixel, find where it
    came from in the source. Sampling forwards would leave holes.
    """
    if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
        raise ValueError(f"expected HxWx3 uint8 rgb, got {frame.shape} {frame.dtype}")
    if landmarks.shape != (5, 2):
        raise ValueError(f"expected (5, 2) landmarks, got {landmarks.shape}")
    template = ARCFACE_112_TEMPLATE if size == 112 else ARCFACE_112_TEMPLATE * (size / 112.0)
    matrix = umeyama_similarity(landmarks.astype(np.float32), template)
    return _warp_inverse(frame, matrix, size)


def _warp_inverse(frame: np.ndarray, matrix: np.ndarray, size: int) -> np.ndarray:
    a = matrix[:, :2].astype(np.float64)
    t = matrix[:, 2].astype(np.float64)
    inv = np.linalg.inv(a)
    ys, xs = np.meshgrid(
        np.arange(size, dtype=np.float64), np.arange(size, dtype=np.float64), indexing="ij"
    )
    dst = np.stack([xs.ravel(), ys.ravel()], axis=1)
    src = (dst - t) @ inv.T
    src_h, src_w = frame.shape[:2]
    x = np.clip(src[:, 0], 0, src_w - 1)
    y = np.clip(src[:, 1], 0, src_h - 1)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = np.clip(x0 + 1, 0, src_w - 1)
    y1 = np.clip(y0 + 1, 0, src_h - 1)
    fx = (x - x0)[:, None]
    fy = (y - y0)[:, None]
    src_f = frame.astype(np.float64)
    top = src_f[y0, x0] + (src_f[y0, x1] - src_f[y0, x0]) * fx
    bottom = src_f[y1, x0] + (src_f[y1, x1] - src_f[y1, x0]) * fx
    out = top + (bottom - top) * fy
    return np.clip(np.rint(out), 0, 255).astype(np.uint8).reshape(size, size, 3)


def resize_crop(crop: np.ndarray, size: int = 112) -> np.ndarray:
    """Fallback for a crop with no landmarks: plain resize, no alignment.

    Kept separate and named so a caller cannot reach for it by accident. An
    unaligned crop is not comparable to an aligned template, so this exists for
    tests and diagnostics, never for enrolment or matching.
    """
    return np.clip(np.rint(resize_bilinear(crop, size, size)), 0, 255).astype(np.uint8)
