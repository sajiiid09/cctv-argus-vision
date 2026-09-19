"""SSD MobileNet v1 detector over ONNX Runtime.

Artefact: ssd_mobilenet_v1_10.onnx (Apache-2.0, ONNX model zoo; ADR-0011).
Input is dynamic-size uint8 NHWC and the graph resizes internally, so frames
are fed at native resolution — no preprocessing step that could diverge
between platforms. Output is TF-style post-NMS: normalized
[ymin, xmin, ymax, xmax], up to 100 detections.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from argus.backends.onnx_common import load_onnx_session
from argus.backends.types import Box, Detection

# COCO 90-class labels (the SSD model's id space; ids with no entry are unused)
COCO_LABELS: dict[int, str] = {
    1: "person",
    2: "bicycle",
    3: "car",
    4: "motorcycle",
    5: "airplane",
    6: "bus",
    7: "train",
    8: "truck",
    9: "boat",
    10: "traffic light",
    11: "fire hydrant",
    13: "stop sign",
    14: "parking meter",
    15: "bench",
    16: "bird",
    17: "cat",
    18: "dog",
    19: "horse",
    20: "sheep",
    21: "cow",
    22: "elephant",
    23: "bear",
    24: "zebra",
    25: "giraffe",
    27: "backpack",
    28: "umbrella",
    31: "handbag",
    32: "tie",
    33: "suitcase",
    34: "frisbee",
    35: "skis",
    36: "snowboard",
    37: "sports ball",
    38: "kite",
    39: "baseball bat",
    40: "baseball glove",
    41: "skateboard",
    42: "surfboard",
    43: "tennis racket",
    44: "bottle",
    46: "wine glass",
    47: "cup",
    48: "fork",
    49: "knife",
    50: "spoon",
    51: "bowl",
    52: "banana",
    53: "apple",
    54: "sandwich",
    55: "orange",
    56: "broccoli",
    57: "carrot",
    58: "hot dog",
    59: "pizza",
    60: "donut",
    61: "cake",
    62: "chair",
    63: "couch",
    64: "potted plant",
    65: "bed",
    67: "dining table",
    70: "toilet",
    72: "tv",
    73: "laptop",
    74: "mouse",
    75: "remote",
    76: "keyboard",
    77: "cell phone",
    78: "microwave",
    79: "oven",
    80: "toaster",
    81: "sink",
    82: "refrigerator",
    84: "book",
    85: "clock",
    86: "vase",
    87: "scissors",
    88: "teddy bear",
    89: "hair drier",
    90: "toothbrush",
}


class SsdOnnxDetector:
    """Shared SSD implementation; subclasses choose execution providers."""

    backend_name = "onnx"

    def __init__(self, providers: list[str]) -> None:
        self._providers = providers
        self._session, self._model_ref = load_onnx_session("ssd_mobilenet_v1", providers)
        self._in_name = self._session.get_inputs()[0].name
        self._out_names = [o.name for o in self._session.get_outputs()]
        self.model_ref = str(self._model_ref)
        # What the session GOT, which is not always what was asked for.
        self.providers_active = list(self._session.get_providers())

    def _raw_detect(self, frame: np.ndarray) -> Any:
        if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
            raise ValueError(f"detector expects HxWx3 uint8 rgb, got {frame.shape} {frame.dtype}")
        return self._session.run(self._out_names, {self._in_name: frame[None]})

    def detect(self, frame: np.ndarray, score_threshold: float = 0.5) -> list[Detection]:
        outs = dict(zip(self._out_names, self._raw_detect(frame), strict=True))
        boxes = outs["detection_boxes:0"][0]
        classes = outs["detection_classes:0"][0]
        scores = outs["detection_scores:0"][0]
        num = int(outs["num_detections:0"][0])
        h, w = frame.shape[:2]
        detections: list[Detection] = []
        for i in range(min(num, len(boxes))):
            score = float(scores[i])
            if score < score_threshold:
                continue
            ymin, xmin, ymax, xmax = (float(v) for v in boxes[i])
            label_id = int(classes[i])
            detections.append(
                Detection(
                    box=Box(x1=xmin * w, y1=ymin * h, x2=xmax * w, y2=ymax * h),
                    score=score,
                    label=COCO_LABELS.get(label_id, f"class_{label_id}"),
                    label_id=label_id,
                )
            )
        return detections
