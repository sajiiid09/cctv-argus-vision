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
