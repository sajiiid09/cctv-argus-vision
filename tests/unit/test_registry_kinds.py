"""The (kind, name) registry: selection, refusal, and the two error messages.

The messages matter as much as the behaviour. "Unknown backend" and "registered
but its probe failed" send whoever hit them to different places -- a typo versus
a missing runtime -- and collapsing them into one message costs an afternoon.
"""

from __future__ import annotations

import pytest
from argus.backends.registry import (
    DEFAULT_PREFERENCE,
    BackendRegistry,
    RegistryError,
    get_face_detector,
    get_face_embedder,
    get_pose_estimator,
)


class _Fake:
    def __init__(self, name: str) -> None:
        self.name = name
        self.model_ref = f"{name}@000000000000"


def _reg() -> BackendRegistry:
    registry = BackendRegistry()
    registry.register("face_embedder", "onnx-cpu", lambda: _Fake("onnx-cpu"), lambda: True)
    registry.register("face_embedder", "onnx-cuda", lambda: _Fake("onnx-cuda"), lambda: False)
    registry.register("face_embedder", "mock", lambda: _Fake("mock"), lambda: True)
    return registry


def test_preference_order_skips_unavailable_backends() -> None:
    assert _reg().get("face_embedder").name == "onnx-cpu"


def test_mock_is_never_preferred_but_can_be_asked_for() -> None:
    """Silently serving mock embeddings would be worse than failing."""
    assert "mock" not in DEFAULT_PREFERENCE
    assert _reg().get("face_embedder", "mock").name == "mock"


def test_unknown_name_lists_what_is_registered() -> None:
    with pytest.raises(RegistryError, match="unknown face_embedder backend 'onnx-tpu'"):
        _reg().get("face_embedder", "onnx-tpu")


def test_registered_but_unavailable_says_so_separately() -> None:
    with pytest.raises(RegistryError, match="capability probe failed"):
        _reg().get("face_embedder", "onnx-cuda")


def test_nothing_available_names_the_sync_command() -> None:
    registry = BackendRegistry()
    registry.register("pose", "onnx-cuda", lambda: _Fake("onnx-cuda"), lambda: False)
    with pytest.raises(RegistryError, match="uv sync --all-packages"):
        registry.get("pose")


def test_unknown_kind_is_refused_at_registration() -> None:
    with pytest.raises(RegistryError, match="unknown backend kind"):
        BackendRegistry().register("nose", "onnx-cpu", lambda: _Fake("x"), lambda: True)


def test_clip_classifier_has_no_backends_registered() -> None:
    """ADR-0012 permits a trigger and a human, not a classifier."""
    from argus.backends.registry import register_defaults

    assert register_defaults().registered("clip_classifier") == []


def test_every_kind_has_a_mock(caplog) -> None:
    import logging

    with caplog.at_level(logging.INFO):
        backends = [
            get_pose_estimator("mock"),
            get_face_detector("mock"),
            get_face_embedder("mock"),
        ]
    assert [b.name for b in backends] == ["mock", "mock", "mock"]
    # The construction log is the answer to "which backend produced this number".
    assert "pose backend=mock" in caplog.text
    assert "face_embedder backend=mock model=mock-embed@000000000000" in caplog.text
