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


def load_onnx_session(name: str, providers: list[str]) -> tuple[Any, ModelRef]:
    """Create an onnxruntime InferenceSession over a hash-verified artefact.

    onnxruntime is imported here and nowhere else (enforced by structural test).
    """
    import onnxruntime as ort

    path = artefact_path(name)
    entry = registry_entry(name)
    ref = ModelRef(name=name, sha256=entry["sha256"])
    session = ort.InferenceSession(str(path), providers=providers)
    return session, ref
