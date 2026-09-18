"""Person detection over a YOLO-family ONNX export (ADR-0010, ADR-0030).

UNVERIFIED against a real artefact: the weights are non-commercial and no
upstream-published ONNX export has been pinned yet, so `models/registry.yaml`
carries the entry with `status: unresolved`. Everything here is exercised
against synthetic session outputs; the first run against real weights must
re-measure parity tolerances rather than inherit SSD's (AGENTS.md §7).

Two output layouts are supported because the family ships both, and which one
an export produces is a property of the export rather than of the model:

  end-to-end  (1, N, 6)  -> x1, y1, x2, y2, score, class   (NMS already applied)
  raw         (1, 84, N) -> cx, cy, w, h + 80 class scores (NMS applied here)

Anything else is refused at construction with the observed shape in the
message, because a guessed layout produces boxes that are wrong in a way no
test notices.
"""

from __future__ import annotations

import numpy as np
from argus.backends.nms import nms
from argus.backends.onnx_model import OnnxModel
from argus.backends.preprocessing import letterbox
from argus.backends.types import Box, Detection

# COCO class ids as the detection heads emit them (0-based, unlike the SSD
# artefact's 1-based ids). Only the classes this project reads are named.
COCO80_LABELS: dict[int, str] = {0: "person"}

DEFAULT_INPUT = (640, 640)


class YoloOnnxDetector(OnnxModel):
    kind = "detector"

    def __init__(
        self,
        *,
        name: str = "onnx-cpu",
        providers: list[str] | None = None,
        artefact: str = "yolo26m",
        iou_threshold: float = 0.45,
    ) -> None:
        super().__init__(artefact, name=name, providers=providers or ["CPUExecutionProvider"])
        self._iou = iou_threshold
        self._height, self._width = self._input_hw(DEFAULT_INPUT)
        self._layout = self._detect_layout()

    def _detect_layout(self) -> str:
        shape = list(self._outputs[0].shape)
        if len(shape) != 3:
            raise self._refuse(f"output 0 has shape {shape}, expected 3 dimensions")
        last, middle = shape[2], shape[1]
        if isinstance(last, int) and last == 6:
            return "e2e"
        if isinstance(middle, int) and middle >= 5:
            return "raw"
        if isinstance(last, int) and last >= 5:
            return "raw_t"
        raise self._refuse(f"output 0 has shape {shape}, which is neither (1,N,6) nor (1,84,N)")

    def detect(self, frame: np.ndarray, *, score_threshold: float = 0.5) -> list[Detection]:
        if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
            raise ValueError(f"detector expects HxWx3 uint8 rgb, got {frame.shape} {frame.dtype}")
        tensor, scale, pad_x, pad_y = letterbox(frame, self._width, self._height)
        raw = self._run(tensor)[0]
        boxes, scores, classes = self._decode(np.asarray(raw))
        keep = [i for i, s in enumerate(scores) if s >= score_threshold]
        if self._layout != "e2e" and keep:
            keep = [keep[i] for i in nms(boxes[keep], scores[keep], self._iou)]
        height, width = frame.shape[:2]
        detections: list[Detection] = []
        for i in keep:
            x1, y1, x2, y2 = self._to_source(boxes[i], scale, pad_x, pad_y, width, height)
            label_id = int(classes[i])
            detections.append(
                Detection(
                    box=Box(x1=x1, y1=y1, x2=x2, y2=y2),
                    score=round(float(scores[i]), 4),
                    label=COCO80_LABELS.get(label_id, f"class_{label_id}"),
                    label_id=label_id,
                )
            )
        # Sorted, not "whatever the model emitted": the tracker's association is
        # deterministic only if its input order is.
        detections.sort(key=lambda d: (-d.score, d.box.y1, d.box.x1))
        return detections

    def _decode(self, raw: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if raw.ndim != 3:
            raise self._refuse(f"inference returned shape {raw.shape}")
        rows = raw[0]
        if self._layout == "e2e":
            if rows.shape[1] != 6:
                raise self._refuse(f"expected (N, 6) rows, got {rows.shape}")
            return rows[:, :4].astype(np.float32), rows[:, 4].astype(np.float32), rows[:, 5]
        table = rows if self._layout == "raw_t" else rows.T  # -> (N, 4 + classes)
        if table.shape[1] < 5:
            raise self._refuse(f"expected at least 5 columns per row, got {table.shape}")
        xywh = table[:, :4].astype(np.float32)
        class_scores = table[:, 4:].astype(np.float32)
        classes = class_scores.argmax(axis=1)
        scores = class_scores.max(axis=1)
        half_w = xywh[:, 2] / 2.0
        half_h = xywh[:, 3] / 2.0
        boxes = np.stack(
            [
                xywh[:, 0] - half_w,
                xywh[:, 1] - half_h,
                xywh[:, 0] + half_w,
                xywh[:, 1] + half_h,
            ],
            axis=1,
        )
        return boxes, scores, classes

    @staticmethod
    def _to_source(
        box: np.ndarray, scale: float, pad_x: float, pad_y: float, width: int, height: int
    ) -> tuple[float, float, float, float]:
        x1 = (float(box[0]) - pad_x) / scale
        y1 = (float(box[1]) - pad_y) / scale
        x2 = (float(box[2]) - pad_x) / scale
        y2 = (float(box[3]) - pad_y) / scale
        return (
            max(0.0, min(x1, width - 1.0)),
            max(0.0, min(y1, height - 1.0)),
            max(0.0, min(x2, width - 1.0)),
            max(0.0, min(y2, height - 1.0)),
        )
