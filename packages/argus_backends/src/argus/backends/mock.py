"""Mock backend: pure-numpy bright-disk detector for tests and capability demos.

Explicitly selectable only (registry name "mock"); it is never in the default
preference order, because silently serving mock detections would be worse than
failing. Deterministic by construction.
"""

from __future__ import annotations

import numpy as np
from argus.backends.types import Box, Detection


class MockDiskDetector:
    name = "mock"
    model_ref = "mock@000000000000"

    def detect(self, frame: np.ndarray, score_threshold: float = 0.5) -> list[Detection]:
        if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
            raise ValueError(f"expected HxWx3 uint8, got {frame.shape} {frame.dtype}")
        gray = frame.mean(axis=2)
        mask = gray > 120
        if not mask.any():
            return []
        ys, xs = np.nonzero(mask)
        detections: list[Detection] = []
        # greedy clustering by proximity; deterministic scan order
        points = list(zip(xs.tolist(), ys.tolist(), strict=True))
        remaining = set(range(len(points)))
        while remaining:
            seed = min(remaining)
            cluster = [seed]
            remaining.discard(seed)
            sx, sy = points[seed]
            changed = True
            while changed:
                changed = False
                for i in list(remaining):
                    x, y = points[i]
                    if abs(x - sx) < 40 and abs(y - sy) < 40:
                        cluster.append(i)
                        remaining.discard(i)
                        changed = True
            if len(cluster) < 80:
                continue
            cxs = [points[i][0] for i in cluster]
            cys = [points[i][1] for i in cluster]
            x1, x2 = min(cxs), max(cxs)
            y1, y2 = min(cys), max(cys)
            brightness = float(gray[y1 : y2 + 1, x1 : x2 + 1].mean()) / 255.0
            if brightness < score_threshold:
                continue
            detections.append(
                Detection(
                    box=Box(x1, y1, x2, y2),
                    score=round(brightness, 4),
                    label="disk",
                    label_id=1,
                )
            )
        return detections
