"""The onnxruntime environment guard.

onnxruntime, onnxruntime-gpu and onnxruntime-openvino all unpack into the same
``onnxruntime`` package directory. Installing two of them leaves whichever synced
last in place; uninstalling one can delete the directory the other still needs.
Neither failure announces itself as a dependency problem -- one presents as a
CUDA provider that is mysteriously missing, the other as a distribution that
reports itself installed and will not import.

This was observed, not theorised: syncing the cpu and staging groups together
replaced onnxruntime with onnxruntime-gpu, and removing the latter left
onnxruntime's metadata behind with no module under it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from argus.backends import onnx_common
from argus.backends.onnx_common import (
    RuntimeEnvironmentError,
    check_ort_environment,
)


def _fake_distributions(names: list[str]):
    def factory():
        return [SimpleNamespace(metadata={"Name": n}) for n in names]

    return factory


@pytest.fixture(autouse=True)
def _clear_cache():
    check_ort_environment.cache_clear()
    yield
    check_ort_environment.cache_clear()


def test_single_runtime_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        onnx_common, "distributions", _fake_distributions(["onnxruntime", "numpy", "av"])
    )
    assert check_ort_environment() == ("onnxruntime",)


def test_gpu_only_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        onnx_common, "distributions", _fake_distributions(["onnxruntime-gpu", "numpy"])
    )
    assert check_ort_environment() == ("onnxruntime-gpu",)


def test_two_runtimes_raise_with_the_repair_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        onnx_common,
        "distributions",
        _fake_distributions(["onnxruntime", "onnxruntime-gpu"]),
    )
    with pytest.raises(RuntimeEnvironmentError) as exc:
        check_ort_environment()
    message = str(exc.value)
    assert "onnxruntime-gpu" in message
    # the message must carry the fix, not just the diagnosis: the repair needs
    # --reinstall-package, which nobody guesses
    assert "--reinstall-package" in message
    assert "--group cpu" in message and "--group staging" in message


def test_no_runtime_installed_is_not_this_guards_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing runtime surfaces as the registry's own 'no backend available'
    error, which already names the sync command. Raising here would replace a
    good message with a worse one."""
    monkeypatch.setattr(onnx_common, "distributions", _fake_distributions(["numpy"]))
    assert check_ort_environment() == ()


def test_name_matching_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        onnx_common,
        "distributions",
        _fake_distributions(["ONNXRuntime", "onnxruntime-GPU"]),
    )
    with pytest.raises(RuntimeEnvironmentError):
        check_ort_environment()


def test_missing_name_metadata_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some distributions have no Name in their metadata; that must not crash a
    guard whose whole job is to produce a clear message."""
    monkeypatch.setattr(
        onnx_common,
        "distributions",
        lambda: [
            SimpleNamespace(metadata={"Name": None}),
            SimpleNamespace(metadata={"Name": "onnxruntime"}),
        ],
    )
    assert check_ort_environment() == ("onnxruntime",)
