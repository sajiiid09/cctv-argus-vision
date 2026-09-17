"""ONNX session loading with artefact-hash verification (ADR-0026).

Model binaries are never committed: ``models/registry.yaml`` maps artefact
name → (file, sha256, url, licence), ``models/fetch.py`` downloads and
verifies, and every session load re-verifies the hash so the parity suite can
never compare two different model files by accident.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import distributions
from pathlib import Path
from typing import Any

import yaml


class ModelArtefactError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ModelRef:
    name: str
    sha256: str

    def __str__(self) -> str:
        return f"{self.name}@{self.sha256[:12]}"


def models_root() -> Path:
    env = os.environ.get("ARGUS_MODELS_ROOT")
    if env:
        root = Path(env)
        if not (root / "registry.yaml").is_file():
            # an explicitly-set root that is wrong is an error, not a hint
            raise ModelArtefactError(
                f"ARGUS_MODELS_ROOT={env} has no registry.yaml; "
                "run models/fetch.py from the repo root or unset the variable"
            )
        return root
    candidates = [Path.cwd() / "models"]
    here = Path(__file__).resolve()
    candidates += [p / "models" for p in here.parents]
    for candidate in candidates:
        if (candidate / "registry.yaml").is_file():
            return candidate
    looked = ", ".join(str(c) for c in candidates[:2])
    raise ModelArtefactError(
        f"models root not found (no registry.yaml under {looked}…); "
        "set ARGUS_MODELS_ROOT or run from the repo root"
    )


def registry_entry(name: str, root: Path | None = None) -> dict[str, Any]:
    root = root or models_root()
    data = yaml.safe_load((root / "registry.yaml").read_text())
    entry = (data or {}).get("artifacts", {}).get(name)
    if entry is None:
        raise ModelArtefactError(f"artefact {name!r} not in {root / 'registry.yaml'}")
    return entry


def artefact_path(name: str, root: Path | None = None) -> Path:
    root = root or models_root()
    entry = registry_entry(name, root)
    path = root / entry["file"]
    if not path.exists():
        raise ModelArtefactError(
            f"artefact {name} not fetched; run: python models/fetch.py (ADR-0026)"
        )
    digest = sha256_file(path)
    if digest != entry["sha256"]:
        raise ModelArtefactError(
            f"artefact {name} hash mismatch: expected {entry['sha256'][:12]}, "
            f"got {digest[:12]} — refetch with models/fetch.py"
        )
    return path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_hash(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual != expected:
        raise ModelArtefactError(
            f"{path.name}: sha256 mismatch (expected {expected[:12]}, got {actual[:12]})"
        )


class RuntimeEnvironmentError(Exception):
    pass


ORT_DISTRIBUTIONS = ("onnxruntime", "onnxruntime-gpu", "onnxruntime-openvino")


@lru_cache(maxsize=1)
def check_ort_environment() -> tuple[str, ...]:
    """Refuse to run with more than one onnxruntime distribution installed.

    They all unpack into the same ``onnxruntime`` package directory, so a second
    one overwrites the first and uninstalling either can delete the shared
    directory out from under the survivor. The symptom is never "conflicting
    dependencies" -- it is a CUDA provider that has silently vanished, or an
    ``onnxruntime`` that reports itself installed and refuses to import. Both
    cost an afternoon if they are met without warning.

    Declaring the groups conflicting in ``pyproject.toml`` does not help: uv
    ignores conflict declarations on a workspace root that is not itself a
    package. So the check lives here, where it also catches a pip install.
    """
    installed = tuple(
        sorted(
            name
            for dist in distributions()
            if (name := (dist.metadata["Name"] or "").lower()) in ORT_DISTRIBUTIONS
        )
    )
    if len(set(installed)) > 1:
        raise RuntimeEnvironmentError(
            f"multiple onnxruntime distributions installed: {list(installed)}. "
            "They share one package directory and overwrite each other. Install "
            "exactly one: `uv sync --all-packages --group cpu` on a dev box, "
            "`uv sync --all-packages --group staging` on the NVIDIA box. If you "
            "have already hit this, repair with "
            "`--reinstall-package onnxruntime` -- removing one distribution can "
            "delete the shared directory the other still needs."
        )
    return installed


def load_onnx_session(name: str, providers: list[str]) -> tuple[Any, ModelRef]:
    """Create an onnxruntime InferenceSession over a hash-verified artefact.

    onnxruntime is imported here and nowhere else (enforced by structural test).
    """
    check_ort_environment()
    import onnxruntime as ort

    path = artefact_path(name)
    entry = registry_entry(name)
    ref = ModelRef(name=name, sha256=entry["sha256"])
    session = ort.InferenceSession(str(path), providers=providers)
    return session, ref
