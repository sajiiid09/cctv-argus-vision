"""Backend registry: capability-probed selection, loggable, overridable.

    from argus.backends.registry import get_detector, get_face_embedder
    detector = get_detector()            # preference order, probe-based
    detector = get_detector("onnx-cpu")  # forced ("does this repro on CPU?")

Four kinds live here -- ``detector``, ``pose``, ``face_detector``,
``face_embedder`` -- with ``clip_classifier`` reserved for a violence classifier
that ADR-0012 does not permit yet. One registry rather than four because the
selection rule is the same for all of them and worth having in one place: an
explicit name must be registered *and* pass its probe, otherwise the preference
order decides.

Probes answer "is this runtime usable on this machine", never "is the artefact
fetched". A missing artefact is a `ModelArtefactError` at construction naming
models/fetch.py, which is a better failure than a backend quietly dropping out
of the preference order and something slower serving the request instead.

Every constructed backend logs its kind, name and model hash -- the first
question in any "why do the numbers differ" conversation is which backend each
side ran.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from argus.backends.interfaces import (
    ClipClassifier,
    Detector,
    FaceDetector,
    FaceEmbedder,
    PoseEstimator,
)

log = logging.getLogger(__name__)

Kind = Literal["detector", "pose", "face_detector", "face_embedder", "clip_classifier"]

KINDS: tuple[Kind, ...] = (
    "detector",
    "pose",
    "face_detector",
    "face_embedder",
    "clip_classifier",
)

Factory = Callable[[], Any]
DetectorFactory = Callable[[], Detector]

DEFAULT_PREFERENCE = ("onnx-cuda", "onnx-coreml", "onnx-cpu")


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Entry:
    factory: Factory
    available: Callable[[], bool]


class BackendRegistry:
    def __init__(self) -> None:
        self._entries: dict[Kind, dict[str, Entry]] = {kind: {} for kind in KINDS}

    def register(
        self, kind: Kind, name: str, factory: Factory, available: Callable[[], bool]
    ) -> None:
        if kind not in self._entries:
            raise RegistryError(f"unknown backend kind {kind!r}; expected one of {list(KINDS)}")
        self._entries[kind][name] = Entry(factory=factory, available=available)

    def registered(self, kind: Kind) -> list[str]:
        return sorted(self._entries[kind])

    def available(self, kind: Kind) -> list[str]:
        return [name for name, entry in self._entries[kind].items() if entry.available()]

    def get(
        self,
        kind: Kind,
        name: str | None = None,
        preference: tuple[str, ...] = DEFAULT_PREFERENCE,
    ) -> Any:
        entries = self._entries[kind]
        if name is not None:
            if name not in entries:
                raise RegistryError(
                    f"unknown {kind} backend {name!r}; registered: {self.registered(kind)}"
                )
            if not entries[name].available():
                raise RegistryError(
                    f"{kind} backend {name!r} is registered but its capability probe failed "
                    f"(missing runtime/device?); available: {self.available(kind)}"
                )
            return self._construct(kind, name)
        for candidate in preference:
            if candidate in entries and entries[candidate].available():
                return self._construct(kind, candidate)
        raise RegistryError(
            f"no {kind} backend available; on a dev box run "
            "`uv sync --all-packages --group cpu` (installs onnxruntime CPU), on staging "
            "`uv sync --all-packages --group staging`"
        )

    def _construct(self, kind: Kind, name: str) -> Any:
        backend = self._entries[kind][name].factory()
        # `name` is what was ASKED for; providers_active is what the session got.
        # Logging only the first is how a CPU fallback passes for a GPU run.
        log.info(
            "%s backend=%s model=%s providers=%s",
            kind,
            name,
            backend.model_ref,
            getattr(backend, "providers_active", "n/a"),
        )
        return backend

    # Detector-shaped wrappers kept because the golden parity suite calls them
    # and a signature change there would move the one test that measures
    # cross-platform divergence.
    def register_detector(
        self, name: str, factory: DetectorFactory, available: Callable[[], bool]
    ) -> None:
        self.register("detector", name, factory, available)

    def available_detectors(self) -> list[str]:
        return self.available("detector")

    def get_detector(
        self, name: str | None = None, preference: tuple[str, ...] = DEFAULT_PREFERENCE
    ) -> Detector:
        return cast(Detector, self.get("detector", name, preference))


_registry = BackendRegistry()


def register_defaults() -> BackendRegistry:
    """Register every backend this build knows about.

    ``mock`` is registered for every kind and is deliberately absent from
    DEFAULT_PREFERENCE: silently serving mock detections would be worse than
    failing, so it has to be asked for by name.
    """
    from argus.backends.arcface import ArcFaceEmbedder
    from argus.backends.mock import (
        MockDiskDetector,
        MockFaceDetector,
        MockFaceEmbedder,
        MockPoseEstimator,
    )
    from argus.backends.onnx_backends import (
        OnnxCoremlDetector,
        OnnxCpuDetector,
        OnnxCudaDetector,
        coreml_available,
        cpu_available,
        cuda_available,
        providers_for,
    )
    from argus.backends.scrfd import ScrfdFaceDetector
    from argus.backends.yolo_pose import YoloPoseOnnxEstimator

    probes: dict[str, Callable[[], bool]] = {
        "onnx-cpu": cpu_available,
        "onnx-cuda": cuda_available,
        "onnx-coreml": coreml_available,
    }

    _registry.register("detector", "onnx-cpu", OnnxCpuDetector, cpu_available)
    _registry.register("detector", "onnx-cuda", OnnxCudaDetector, cuda_available)
    _registry.register("detector", "onnx-coreml", OnnxCoremlDetector, coreml_available)
    _registry.register("detector", "mock", MockDiskDetector, lambda: True)

    for backend, probe in probes.items():
        _registry.register(
            "pose",
            backend,
            _bind(YoloPoseOnnxEstimator, backend, providers_for(backend)),
            probe,
        )
        _registry.register(
            "face_detector",
            backend,
            _bind(ScrfdFaceDetector, backend, providers_for(backend)),
            probe,
        )
        _registry.register(
            "face_embedder",
            backend,
            _bind(ArcFaceEmbedder, backend, providers_for(backend)),
            probe,
        )

    _registry.register("pose", "mock", MockPoseEstimator, lambda: True)
    _registry.register("face_detector", "mock", MockFaceDetector, lambda: True)
    _registry.register("face_embedder", "mock", MockFaceEmbedder, lambda: True)
    return _registry


def _bind(cls: Any, name: str, providers: list[str]) -> Factory:
    def factory() -> Any:
        return cls(name=name, providers=providers)

    return factory


# Kept as the pre-(kind, name) name so `register_default_detectors()` callers
# and the golden suite keep working.
register_default_detectors = register_defaults


def _ensure_registered() -> BackendRegistry:
    if not _registry.registered("detector"):
        register_defaults()
    return _registry


def get_detector(
    name: str | None = None, preference: tuple[str, ...] = DEFAULT_PREFERENCE
) -> Detector:
    return cast(Detector, _ensure_registered().get("detector", name, preference))


def get_pose_estimator(
    name: str | None = None, preference: tuple[str, ...] = DEFAULT_PREFERENCE
) -> PoseEstimator:
    return cast(PoseEstimator, _ensure_registered().get("pose", name, preference))


def get_face_detector(
    name: str | None = None, preference: tuple[str, ...] = DEFAULT_PREFERENCE
) -> FaceDetector:
    return cast(FaceDetector, _ensure_registered().get("face_detector", name, preference))


def get_face_embedder(
    name: str | None = None, preference: tuple[str, ...] = DEFAULT_PREFERENCE
) -> FaceEmbedder:
    return cast(FaceEmbedder, _ensure_registered().get("face_embedder", name, preference))


def get_clip_classifier(
    name: str | None = None, preference: tuple[str, ...] = DEFAULT_PREFERENCE
) -> ClipClassifier:
    """Reserved. ADR-0012 permits a trigger and a human, not a classifier.

    Registered by nothing today, so this raises with the list of registered
    names -- which is empty, and says so.
    """
    return cast(ClipClassifier, _ensure_registered().get("clip_classifier", name, preference))
