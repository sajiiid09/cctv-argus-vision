"""Concrete ONNX backends: CPU (reference), CUDA, CoreML.

onnxruntime is imported only in this module tree. Availability is capability-
probed via ``onnxruntime.get_available_providers()`` — never a platform check
(ARCHITECTURE.md §5.1 rule 2).
"""

from __future__ import annotations

from typing import Any

from argus.backends.ssd import SsdOnnxDetector


def ort_available() -> Any | None:
    try:
        import onnxruntime as ort
    except ImportError:
        return None
    return ort


class OnnxCpuDetector(SsdOnnxDetector):
    """Reference backend: CPU EP, available everywhere including CI."""

    name = "onnx-cpu"

    def __init__(self) -> None:
        super().__init__(providers=["CPUExecutionProvider"])


class OnnxCudaDetector(SsdOnnxDetector):
    """Linux/NVIDIA staging and production. CUDA EP with CPU fallback."""

    name = "onnx-cuda"

    def __init__(self) -> None:
        super().__init__(providers=["CUDAExecutionProvider", "CPUExecutionProvider"])


class OnnxCoremlDetector(SsdOnnxDetector):
    """macOS dev machines. CoreML EP with CPU fallback."""

    name = "onnx-coreml"

    def __init__(self) -> None:
        super().__init__(providers=["CoreMLExecutionProvider", "CPUExecutionProvider"])


PROVIDERS: dict[str, list[str]] = {
    "onnx-cpu": ["CPUExecutionProvider"],
    "onnx-cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
    "onnx-coreml": ["CoreMLExecutionProvider", "CPUExecutionProvider"],
}


def providers_for(backend: str) -> list[str]:
    """Execution providers for a registry backend name, CPU always last.

    The fallback is not politeness: a model whose op set the accelerator does
    not cover would otherwise fail to load rather than run slower, and a
    pipeline that refuses to start is worse than one that starts and logs which
    provider it got.
    """
    try:
        return list(PROVIDERS[backend])
    except KeyError:
        raise ValueError(f"unknown backend name {backend!r}; known: {sorted(PROVIDERS)}") from None


def cuda_available() -> bool:
    ort = ort_available()
    if ort is None:
        return False
    return "CUDAExecutionProvider" in ort.get_available_providers()


def coreml_available() -> bool:
    ort = ort_available()
    if ort is None:
        return False
    return "CoreMLExecutionProvider" in ort.get_available_providers()


def cpu_available() -> bool:
    return ort_available() is not None
