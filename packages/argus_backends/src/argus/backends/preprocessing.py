"""Deterministic numpy preprocessing shared by ONNX backends.

Pure-numpy bilinear resize on purpose: identical arithmetic on every platform,
no hidden dependency on a particular opencv build. Parity across backends
starts with parity in preprocessing.
"""

from __future__ import annotations

import numpy as np


def resize_bilinear(arr: np.ndarray, width: int, height: int) -> np.ndarray:
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected HxWx3 rgb, got {arr.shape}")
    src_h, src_w = arr.shape[:2]
    if (src_w, src_h) == (width, height):
        return arr.astype(np.float32)
    xs = np.linspace(0.0, src_w - 1.0, width, dtype=np.float32)
    ys = np.linspace(0.0, src_h - 1.0, height, dtype=np.float32)
    x0 = np.clip(np.floor(xs).astype(np.int32), 0, src_w - 1)
    x1 = np.clip(x0 + 1, 0, src_w - 1)
    y0 = np.clip(np.floor(ys).astype(np.int32), 0, src_h - 1)
    y1 = np.clip(y0 + 1, 0, src_h - 1)
    # fx varies along the output width, fy along the output height: the shapes
    # differ so each broadcasts against (height, width, 3) on its own axis. fy
    # needs two trailing axes -- with one it silently only worked on square
    # frames, which is why this had no caller until letterbox().
    fx = (xs - x0)[:, None]
    fy = (ys - y0)[:, None, None]
    a = arr[y0][:, x0].astype(np.float32)
    b = arr[y0][:, x1].astype(np.float32)
    c = arr[y1][:, x0].astype(np.float32)
    d = arr[y1][:, x1].astype(np.float32)
    top = a + (b - a) * fx
    bottom = c + (d - c) * fx
    return top + (bottom - top) * fy


def letterbox(
    arr: np.ndarray, width: int, height: int, *, pad_value: int = 114
) -> tuple[np.ndarray, float, float, float]:
    """Resize preserving aspect ratio, pad, and return the inverse mapping.

    Returns ``(chw_float32, scale, pad_x, pad_y)`` where a model-space point maps
    back to source pixels as ``(x - pad_x) / scale``. Returning the mapping
    rather than applying it later from remembered numbers is the point: a box
    un-letterboxed with the wrong padding is off by a few per cent, which looks
    like a mediocre detector rather than like a bug.

    Built on the same bilinear kernel as resize_bilinear, so preprocessing is
    identical on every platform by construction (ARCHITECTURE.md §5.3).
    """
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected HxWx3 rgb, got {arr.shape}")
    src_h, src_w = arr.shape[:2]
    scale = min(width / src_w, height / src_h)
    new_w = max(1, round(src_w * scale))
    new_h = max(1, round(src_h * scale))
    resized = resize_bilinear(arr, new_w, new_h)
    canvas = np.full((height, width, 3), float(pad_value), dtype=np.float32)
    pad_x = (width - new_w) // 2
    pad_y = (height - new_h) // 2
    canvas[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = resized
    chw = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None] / 255.0, dtype=np.float32)
    return chw, scale, float(pad_x), float(pad_y)
