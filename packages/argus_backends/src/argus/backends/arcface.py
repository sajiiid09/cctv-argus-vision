"""Face embedding over an ArcFace ONNX export (ADR-0010, ADR-0030).

UNVERIFIED against a real artefact (see yolo.py). Two invariants here are worth
more than the model choice:

* The input must be exactly 112x112x3 uint8 -- an aligned crop, produced by
  argus.backends.face_align. Resizing a loose crop inside this method would
  make "did anyone align this?" unanswerable from the outside.
* The embedding is L2-normalised here, once, so cosine similarity is a dot
  product everywhere downstream and no caller can forget.
"""

from __future__ import annotations

import numpy as np
from argus.backends.onnx_model import OnnxModel
from argus.backends.types import Embedding

CROP_SIZE = 112


class ArcFaceEmbedder(OnnxModel):
    kind = "face_embedder"

    def __init__(
        self,
        *,
        name: str = "onnx-cpu",
        providers: list[str] | None = None,
        artefact: str = "glintr100",
    ) -> None:
        super().__init__(artefact, name=name, providers=providers or ["CPUExecutionProvider"])
        height, width = self._input_hw((CROP_SIZE, CROP_SIZE))
        if (height, width) != (CROP_SIZE, CROP_SIZE):
            raise self._refuse(f"input is {height}x{width}, expected {CROP_SIZE}x{CROP_SIZE}")
        shape = list(self._outputs[0].shape)
        if len(shape) != 2 or not isinstance(shape[1], int) or shape[1] < 64:
            raise self._refuse(f"output 0 has shape {shape}, expected (1, d) with d >= 64")
        self.dim = int(shape[1])

    def embed(self, aligned_crop: np.ndarray) -> Embedding:
        if aligned_crop.shape != (CROP_SIZE, CROP_SIZE, 3) or aligned_crop.dtype != np.uint8:
            raise ValueError(
                f"expected an aligned {CROP_SIZE}x{CROP_SIZE}x3 uint8 crop, got "
                f"{aligned_crop.shape} {aligned_crop.dtype}. Align with "
                "argus.backends.face_align.align_face -- this method will not "
                "resize for you, because then nobody could tell whether the "
                "crop was aligned at all"
            )
        # ArcFace's published preprocessing: (x - 127.5) / 128, CHW, batch of 1.
        tensor = (aligned_crop.astype(np.float32) - 127.5) / 128.0
        tensor = np.ascontiguousarray(tensor.transpose(2, 0, 1)[None])
        raw = np.asarray(self._run(tensor)[0]).reshape(-1).astype(np.float32)
        norm = float(np.linalg.norm(raw))
        if norm == 0.0:
            raise ValueError("embedder returned a zero vector; refusing to normalise it")
        return (raw / norm).astype(np.float32)
