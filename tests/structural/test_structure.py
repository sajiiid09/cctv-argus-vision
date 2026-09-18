"""Structural tests: they enforce architecture, not behaviour (AGENTS.md §7).

These properties erode quietly; the checks are deliberately dumb and strict.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CODE_DIRS = ["services", "packages", "rig", "tests", "models", "config", "infra"]
BACKEND_ROOT = "packages/argus_backends/src/argus/backends"
FORBIDDEN_IMPORTS = {
    "onnxruntime": "backends only (ARCHITECTURE.md §5.1)",
    "torch": "backends only (ARCHITECTURE.md §5.1)",
    "tensorrt": "backends only (ARCHITECTURE.md §5.1)",
    "pycuda": "backends only (ARCHITECTURE.md §5.1)",
}
PAYROLL_ROOT = ROOT / "packages" / "argus_payroll" / "src" / "argus" / "payroll"
# Vision, and also the database and the property-testing library. Payroll is a
# pure function of (events, gaps, policy, code version): it must be importable
# and testable with no database at all, and Hypothesis is a test-only dependency
# that has no business inside the package it exercises.
PAYROLL_FORBIDDEN = (
    "numpy",
    "cv2",
    "av",
    "onnx",
    "torch",
    "PIL",
    "psycopg",
    "argus.store",
    "hypothesis",
)
PAYROLL_WALLCLOCK = ("datetime.now", "time.time", "utcnow", "datetime.today")


def _py_files() -> list[Path]:
    out: list[Path] = []
    for d in CODE_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            out.append(p)
    return out


def test_all_code_paths_lowercase() -> None:
    """CI runs on Linux where case matters; the dev Mac does not (§5.8)."""
    bad: list[str] = []
    for d in CODE_DIRS:
        for p in (ROOT / d).rglob("*"):
            if p.is_file() and any(ch.isupper() for ch in p.name):
                bad.append(str(p.relative_to(ROOT)))
    assert not bad, f"uppercase paths break the case-sensitive deploy: {bad}"


def test_no_runtime_imports_outside_backends() -> None:
    pattern = re.compile(
        r"^\s*(import|from)\s+(" + "|".join(FORBIDDEN_IMPORTS) + r")\b", re.MULTILINE
    )
    offenders: list[str] = []
    for path in _py_files():
        rel = str(path.relative_to(ROOT))
        if rel.startswith(BACKEND_ROOT):
            continue
        if pattern.search(path.read_text()):
            offenders.append(f"{rel}: {FORBIDDEN_IMPORTS.get('onnxruntime')}")
    assert not offenders, f"backend runtimes must only be imported in {BACKEND_ROOT}: {offenders}"


def test_payroll_imports_no_vision() -> None:
    offenders: list[str] = []
    for path in PAYROLL_ROOT.rglob("*.py"):
        text = path.read_text()
        for token in PAYROLL_FORBIDDEN:
            if re.search(rf"^\s*(import|from)\s+{token}\b", text, re.MULTILINE):
                offenders.append(f"{path.relative_to(ROOT)} imports {token}")
    assert not offenders, "payroll is pure functions over events (AGENTS.md §4)"


def test_payroll_reads_no_wall_clock() -> None:
    offenders: list[str] = []
    for path in PAYROLL_ROOT.rglob("*.py"):
        text = path.read_text()
        for token in PAYROLL_WALLCLOCK:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)} contains {token}")
    assert not offenders, "time is an input in payroll code (AGENTS.md §7)"


def test_golden_reference_committed() -> None:
    ref = ROOT / "tests" / "golden" / "reference" / "ssd_mobilenet_v1.json"
    assert ref.exists(), "reference outputs must be committed (AGENTS.md §7)"
    text = ref.read_text()
    assert '"model_ref"' in text and "@" in text, "reference must name its artefact hash"


def test_rig_manifests_committed_and_licenced() -> None:
    manifests = list((ROOT / "rig" / "manifests").glob("*.yaml"))
    assert manifests, "rig manifests are the committable part (ARCHITECTURE.md §6)"
    for m in manifests:
        text = m.read_text()
        assert "licence:" in text, f"{m.name} must record its licence"
        assert "consent:" in text, f"{m.name} must record consent status"


def test_non_commercial_artefacts_are_named_in_decisions() -> None:
    """ADR-0030 promises this check; without it the promise is decoration.

    Weights marked `commercial_use: false` are permitted only for this personal
    test environment. Requiring each one to be named in DECISIONS.md is what
    stops an artefact arriving quietly and the demo posture becoming the pilot
    posture.
    """
    import yaml

    registry = yaml.safe_load((ROOT / "models" / "registry.yaml").read_text())
    decisions = (ROOT / "DECISIONS.md").read_text()
    restricted = [
        name
        for name, spec in (registry or {}).get("artifacts", {}).items()
        if spec.get("commercial_use") is False
    ]
    assert restricted, "expected at least one non-commercial artefact (ADR-0030)"
    missing = [name for name in restricted if name not in decisions]
    assert not missing, f"non-commercial artefacts not named in DECISIONS.md: {missing}"


def test_every_artefact_records_a_licence_and_a_hash_or_says_it_is_unresolved() -> None:
    """A registry entry with neither a hash nor an honest `unresolved` is a trap.

    It would look fetchable, download whatever is at the URL today, and the
    parity suite would compare two different model files without noticing
    (ADR-0026).
    """
    import yaml

    registry = yaml.safe_load((ROOT / "models" / "registry.yaml").read_text())
    for name, spec in (registry or {}).get("artifacts", {}).items():
        assert spec.get("licence"), f"{name} must record a licence"
        assert spec.get("file"), f"{name} must name a file"
        resolved = bool(spec.get("sha256")) and bool(spec.get("url"))
        unresolved = spec.get("status") == "unresolved"
        assert resolved != unresolved, (
            f"{name}: either pin url+sha256 or mark status: unresolved, not both or neither"
        )
