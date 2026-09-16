"""Backend registry: capability-probed selection, loggable, overridable.

    from argus.backends.registry import get_detector
    detector = get_detector()            # preference order, probe-based
    detector = get_detector("onnx-cpu")  # forced ("does this repro on CPU?")

Every constructed backend logs its name and model hash — the first question in
any "why do the numbers differ" conversation is which backend each side ran.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from argus.backends.interfaces import Detector

log = logging.getLogger(__name__)

DetectorFactory = Callable[[], Detector]

DEFAULT_PREFERENCE = ("onnx-cuda", "onnx-coreml", "onnx-cpu")


class RegistryError(RuntimeError):
    pass


class BackendRegistry:
    def __init__(self) -> None:
        self._detector_factories: dict[str, DetectorFactory] = {}
        self._detector_available: dict[str, Callable[[], bool]] = {}

    def register_detector(
        self, name: str, factory: DetectorFactory, available: Callable[[], bool]
    ) -> None:
        self._detector_factories[name] = factory
        self._detector_available[name] = available

    def available_detectors(self) -> list[str]:
        return [n for n, avail in self._detector_available.items() if avail()]

    def get_detector(
        self, name: str | None = None, preference: tuple[str, ...] = DEFAULT_PREFERENCE
    ) -> Detector:
        if name is not None:
            if name not in self._detector_factories:
                raise RegistryError(
                    f"unknown detector backend {name!r}; registered: "
                    f"{sorted(self._detector_factories)}"
                )
            if not self._detector_available[name]():
                raise RegistryError(
                    f"backend {name!r} is registered but its capability probe failed "
                    "(missing runtime/device?); available: "
                    f"{self.available_detectors()}"
                )
            return self._construct(name)
        for candidate in preference:
            if candidate in self._detector_factories and self._detector_available[candidate]():
                return self._construct(candidate)
        raise RegistryError(
            "no detector backend available; on a dev box run "
            "`uv sync --all-packages` (installs onnxruntime CPU), on staging "
            "`uv sync --all-packages --group staging`"
        )

    def _construct(self, name: str) -> Detector:
        detector = self._detector_factories[name]()
        log.info("detector backend=%s model=%s", name, detector.model_ref)
        return detector


_registry = BackendRegistry()


def register_default_detectors() -> BackendRegistry:
    from argus.backends.mock import MockDiskDetector
    from argus.backends.onnx_backends import (
        OnnxCoremlDetector,
        OnnxCpuDetector,
        OnnxCudaDetector,
        coreml_available,
        cpu_available,
        cuda_available,
    )

    _registry.register_detector("onnx-cpu", OnnxCpuDetector, cpu_available)
    _registry.register_detector("onnx-cuda", OnnxCudaDetector, cuda_available)
    _registry.register_detector("onnx-coreml", OnnxCoremlDetector, coreml_available)
    _registry.register_detector("mock", MockDiskDetector, lambda: True)
    return _registry


def get_detector(
    name: str | None = None, preference: tuple[str, ...] = DEFAULT_PREFERENCE
) -> Detector:
    if not _registry._detector_factories:
        register_default_detectors()
    return _registry.get_detector(name, preference)
