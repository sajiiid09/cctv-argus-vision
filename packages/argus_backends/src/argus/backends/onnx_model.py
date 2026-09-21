"""Shared plumbing for ONNX-backed models.

Every wrapper asserts the shape of what it loaded at construction time and
raises naming the observed shape. Guessing a tensor layout produces detections
that are subtly wrong everywhere -- boxes off by a transpose, keypoints in the
wrong order -- and nothing fails, which is exactly the failure mode this project
cannot afford.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from argus.backends.onnx_common import ModelArtefactError, load_onnx_session


class OnnxModel:
    """A hash-verified session plus its identity, and nothing else."""

    kind = "model"

    def __init__(self, artefact: str, *, name: str, providers: list[str]) -> None:
        self.name = name
        self.artefact = artefact
        self._providers = providers
        self._session, self._model_ref = load_onnx_session(artefact, providers)
        self.model_ref = str(self._model_ref)
        self._inputs = self._session.get_inputs()
        self._outputs = self._session.get_outputs()
        self._in_name = self._inputs[0].name
        self._out_names = [o.name for o in self._outputs]
        # What the session GOT, which is not always what was asked for.
        self.providers_active = list(self._session.get_providers())

    def _run(self, tensor: np.ndarray) -> list[Any]:
        return list(self._session.run(self._out_names, {self._in_name: tensor}))

    def _input_hw(self, default: tuple[int, int]) -> tuple[int, int]:
        """(height, width) from the graph, falling back to `default`.

        A static input size is the normal case for these artefacts; a dynamic
        dimension comes back as a string or None, and picking a size for it is
        the caller's decision rather than something to infer silently.
        """
        shape = list(self._inputs[0].shape)
        if len(shape) != 4:
            raise ModelArtefactError(
                f"{self.artefact}: expected a 4D input, got shape {shape} (input {self._in_name!r})"
            )
        height, width = shape[2], shape[3]
        if not isinstance(height, int) or not isinstance(width, int):
            return default
        return height, width

    def _refuse(self, detail: str) -> ModelArtefactError:
        return ModelArtefactError(
            f"{self.artefact} ({self.model_ref}) has an unexpected graph: {detail}. "
            "Refusing to guess a layout -- record the real one in "
            "models/registry.yaml and the wrapper, then re-measure parity "
            "(ARCHITECTURE.md §5.3)."
        )
