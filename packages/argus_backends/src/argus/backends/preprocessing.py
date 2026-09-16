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
    fx = (xs - x0)[:, None]
    fy = (ys - y0)[:, None]
    a = arr[y0][:, x0].astype(np.float32)
    b = arr[y0][:, x1].astype(np.float32)
    c = arr[y1][:, x0].astype(np.float32)
    d = arr[y1][:, x1].astype(np.float32)
    top = a + (b - a) * fx
    bottom = c + (d - c) * fx
    return top + (bottom - top) * fy
