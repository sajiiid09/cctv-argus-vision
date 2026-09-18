"""Pose estimation over a YOLO-family pose ONNX export.

Used by the violence trigger only (ADR-0012: a cheap, recall-biased trigger
that sends a clip to a human -- never a verdict). UNVERIFIED against a real
artefact for the same reason as yolo.py.

Raw layout: (1, 56, N) where each column is cx, cy, w, h, score and then 17
COCO keypoints as x, y, confidence. NMS is applied here on the boxes, and the
keypoints of the surviving rows are returned in source pixels.
"""

from __future__ import annotations

import numpy as np
from argus.backends.nms import nms
from argus.backends.onnx_model import OnnxModel
from argus.backends.preprocessing import letterbox

KEYPOINTS = 17
COCO_KEYPOINT_NAMES = (
    "nose",
    "left_eye",
    "right_eye",
    "left_ear",
    "right_ear",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
)
DEFAULT_INPUT = (640, 640)
ROW_WIDTH = 5 + KEYPOINTS * 3  # 56


class YoloPoseOnnxEstimator(OnnxModel):
    kind = "pose"

    def __init__(
        self,
        *,
        name: str = "onnx-cpu",
        providers: list[str] | None = None,
        artefact: str = "yolo26m_pose",
        score_threshold: float = 0.35,
        iou_threshold: float = 0.45,
    ) -> None:
        super().__init__(artefact, name=name, providers=providers or ["CPUExecutionProvider"])
        self._score = score_threshold
        self._iou = iou_threshold
        self._height, self._width = self._input_hw(DEFAULT_INPUT)
        shape = list(self._outputs[0].shape)
        if len(shape) != 3:
            raise self._refuse(f"output 0 has shape {shape}, expected 3 dimensions")
        if ROW_WIDTH not in {shape[1], shape[2]}:
            raise self._refuse(
                f"output 0 has shape {shape}; expected {ROW_WIDTH} "
                f"(4 box + 1 score + {KEYPOINTS} keypoints x 3) on one axis"
            )
        self._transposed = shape[1] == ROW_WIDTH

    def estimate(self, frame: np.ndarray) -> list[np.ndarray]:
        if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
            raise ValueError(f"pose expects HxWx3 uint8 rgb, got {frame.shape} {frame.dtype}")
        tensor, scale, pad_x, pad_y = letterbox(frame, self._width, self._height)
        raw = np.asarray(self._run(tensor)[0])
        if raw.ndim != 3:
            raise self._refuse(f"inference returned shape {raw.shape}")
        table = raw[0].T if self._transposed else raw[0]
        if table.shape[1] != ROW_WIDTH:
            raise self._refuse(f"expected rows of {ROW_WIDTH}, got {table.shape}")
        scores = table[:, 4].astype(np.float32)
        keep = [i for i, s in enumerate(scores) if s >= self._score]
        if keep:
            boxes = np.stack(
                [
                    table[keep, 0] - table[keep, 2] / 2.0,
                    table[keep, 1] - table[keep, 3] / 2.0,
                    table[keep, 0] + table[keep, 2] / 2.0,
                    table[keep, 1] + table[keep, 3] / 2.0,
                ],
                axis=1,
            ).astype(np.float32)
            keep = [keep[i] for i in nms(boxes, scores[keep], self._iou)]
        people: list[np.ndarray] = []
        for i in keep:
            kpts = table[i, 5:].astype(np.float32).reshape(KEYPOINTS, 3).copy()
            kpts[:, 0] = (kpts[:, 0] - pad_x) / scale
            kpts[:, 1] = (kpts[:, 1] - pad_y) / scale
            people.append(kpts)
        return people
