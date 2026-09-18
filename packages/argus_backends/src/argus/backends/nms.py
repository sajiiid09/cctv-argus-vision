"""Deterministic non-maximum suppression, pure numpy.

Deterministic matters more than fast here: the rig-replay comparison and the
golden parity suite both assume that the same frame produces the same boxes in
the same order on every platform. Equal scores are broken by position (y1, then
x1), because argsort's tie order is not part of numpy's contract.
"""

from __future__ import annotations

import numpy as np


def nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float = 0.45) -> list[int]:
    """Indices of kept boxes, highest score first. boxes are (N, 4) x1y1x2y2."""
    if boxes.ndim != 2 or boxes.shape[1] != 4:
        raise ValueError(f"expected (N, 4) boxes, got {boxes.shape}")
    if len(boxes) != len(scores):
        raise ValueError(f"{len(boxes)} boxes but {len(scores)} scores")
    if len(boxes) == 0:
        return []
    order = sorted(
        range(len(scores)),
        key=lambda i: (-float(scores[i]), float(boxes[i, 1]), float(boxes[i, 0])),
    )
    areas = (boxes[:, 2] - boxes[:, 0]).clip(min=0) * (boxes[:, 3] - boxes[:, 1]).clip(min=0)
    kept: list[int] = []
    while order:
        best = order.pop(0)
        kept.append(best)
        if not order:
            break
        rest = np.asarray(order, dtype=np.int64)
        x1 = np.maximum(boxes[best, 0], boxes[rest, 0])
        y1 = np.maximum(boxes[best, 1], boxes[rest, 1])
        x2 = np.minimum(boxes[best, 2], boxes[rest, 2])
        y2 = np.minimum(boxes[best, 3], boxes[rest, 3])
        inter = (x2 - x1).clip(min=0) * (y2 - y1).clip(min=0)
        union = areas[best] + areas[rest] - inter
        iou = np.where(union > 0, inter / union, 0.0)
        order = [
            int(i) for i, keep in zip(rest.tolist(), iou <= iou_threshold, strict=True) if keep
        ]
    return kept
