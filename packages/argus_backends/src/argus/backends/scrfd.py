"""Face detection with landmarks over an SCRFD ONNX export (ADR-0010).

UNVERIFIED against a real artefact (see yolo.py). SCRFD's heads emit, per
stride in (8, 16, 32): a score map, a distance-encoded bbox map, and a
distance-encoded 5-landmark map -- nine outputs. An export with six outputs is
the no-landmark variant and is refused outright: without landmarks there is no
alignment, and an unaligned crop is not comparable to an enrolled template.
"""

from __future__ import annotations

import numpy as np
from argus.backends.nms import nms
from argus.backends.onnx_model import OnnxModel
from argus.backends.preprocessing import letterbox
from argus.backends.types import Box, FaceDetection

STRIDES = (8, 16, 32)
ANCHORS_PER_CELL = 2
DEFAULT_INPUT = (640, 640)


class ScrfdFaceDetector(OnnxModel):
    kind = "face_detector"

    def __init__(
        self,
        *,
        name: str = "onnx-cpu",
        providers: list[str] | None = None,
        artefact: str = "scrfd_10g_bnkps",
        iou_threshold: float = 0.4,
    ) -> None:
        super().__init__(artefact, name=name, providers=providers or ["CPUExecutionProvider"])
        self._iou = iou_threshold
        self._height, self._width = self._input_hw(DEFAULT_INPUT)
        if len(self._out_names) != 9:
            raise self._refuse(
                f"{len(self._out_names)} outputs; expected 9 (score, bbox and landmark "
                "maps for strides 8, 16 and 32). A 6-output export has no landmarks, "
                "and without landmarks there is no alignment"
            )

    def detect_faces(
        self, frame: np.ndarray, *, score_threshold: float = 0.5
    ) -> list[FaceDetection]:
        if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
            raise ValueError(f"face detector expects HxWx3 uint8, got {frame.shape} {frame.dtype}")
        tensor, scale, pad_x, pad_y = letterbox(frame, self._width, self._height)
        outs = [np.asarray(o) for o in self._run(tensor)]
        boxes: list[np.ndarray] = []
        scores: list[float] = []
        landmarks: list[np.ndarray] = []
        for i, stride in enumerate(STRIDES):
            score_map = outs[i].reshape(-1)
            bbox_map = outs[i + 3].reshape(-1, 4) * stride
            kps_map = outs[i + 6].reshape(-1, 5, 2) * stride
            centres = _anchor_centres(self._height, self._width, stride)
            if len(centres) != len(score_map):
                raise self._refuse(
                    f"stride {stride}: {len(score_map)} scores for {len(centres)} anchors"
                )
            for j, score in enumerate(score_map):
                if score < score_threshold:
                    continue
                cx, cy = centres[j]
                boxes.append(
                    np.array(
                        [
                            cx - bbox_map[j, 0],
                            cy - bbox_map[j, 1],
                            cx + bbox_map[j, 2],
                            cy + bbox_map[j, 3],
                        ],
                        dtype=np.float32,
                    )
                )
                scores.append(float(score))
                landmarks.append((np.array([cx, cy], dtype=np.float32) + kps_map[j]).copy())
        if not boxes:
            return []
        box_arr = np.stack(boxes)
        score_arr = np.asarray(scores, dtype=np.float32)
        faces: list[FaceDetection] = []
        for i in nms(box_arr, score_arr, self._iou):
            box = box_arr[i]
            pts = landmarks[i].copy()
            pts[:, 0] = (pts[:, 0] - pad_x) / scale
            pts[:, 1] = (pts[:, 1] - pad_y) / scale
            faces.append(
                FaceDetection(
                    box=Box(
                        x1=(float(box[0]) - pad_x) / scale,
                        y1=(float(box[1]) - pad_y) / scale,
                        x2=(float(box[2]) - pad_x) / scale,
                        y2=(float(box[3]) - pad_y) / scale,
                    ),
                    score=round(float(score_arr[i]), 4),
                    landmarks=pts.astype(np.float32),
                )
            )
        return faces


def _anchor_centres(height: int, width: int, stride: int) -> np.ndarray:
    """Anchor centres for one stride, in model-space pixels.

    Two anchors per cell, interleaved in the order the heads emit them, which is
    why each centre is repeated rather than the grid being tiled.
    """
    rows = height // stride
    cols = width // stride
    ys, xs = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
    centres = np.stack([xs.ravel(), ys.ravel()], axis=1).astype(np.float32) * stride
    return np.repeat(centres, ANCHORS_PER_CELL, axis=0)
