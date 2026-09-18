"""Fetch and verify model artefacts (ADR-0026).

python models/fetch.py                 # fetch everything missing/changed
python models/fetch.py --list
python models/fetch.py --verify-only

Two things this refuses to do quietly.

An artefact marked ``status: unresolved`` has no pinned URL or sha256 yet, so it
is not downloaded and not treated as present. Fetching "whatever is at that URL
today" is how a parity suite ends up comparing two different model files.

An artefact marked ``commercial_use: false`` prints a warning of its own and is
counted again in a closing summary, because a warning at the top of a long
download scroll is not a warning. ADR-0030 permits those weights for this
personal, non-commercial test environment only.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent


def entries() -> dict[str, dict]:
    data = yaml.safe_load((ROOT / "registry.yaml").read_text())
    return (data or {}).get("artifacts", {})


def is_unresolved(spec: dict) -> bool:
    return spec.get("status") == "unresolved" or not spec.get("sha256") or not spec.get("url")


def fetch(name: str, spec: dict, verify_only: bool) -> bool:
    if is_unresolved(spec):
        print(
            f"{name}: UNRESOLVED — no pinned url/sha256 in registry.yaml, so nothing to "
            "fetch. Backends that need it will refuse to construct (ADR-0030).",
            file=sys.stderr,
        )
        return False
    dest = ROOT / spec["file"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        ok = _check(dest, spec, name)
        if ok or verify_only:
            return ok
        print(f"{name}: hash mismatch — refetching")
    if verify_only:
        print(f"{name}: MISSING (verify-only mode)")
        return False
    url = spec["url"]
    print(f"{name}: downloading {url} …")
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                while chunk := resp.read(1 << 20):
                    tmp.write(chunk)
            tmp_path = Path(tmp.name)
        except BaseException:
            tmp_path = Path(tmp.name)
            tmp_path.unlink(missing_ok=True)
            raise
    if not _check(tmp_path, spec, name, quiet=True):
        tmp_path.unlink(missing_ok=True)
        print(f"{name}: downloaded file does not match the pinned sha256", file=sys.stderr)
        return False
    tmp_path.replace(dest)
    print(f"{name}: verified sha256 {spec['sha256'][:12]}… -> {dest}")
    return True


def _check(path: Path, spec: dict, name: str, quiet: bool = False) -> bool:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    if h.hexdigest() != spec["sha256"]:
        if not quiet:
            print(f"{name}: HASH MISMATCH for {path}", file=sys.stderr)
        return False
    return True


def _licence_warning(name: str, spec: dict) -> None:
    print(
        f"{name}: NON-COMMERCIAL — licence {spec.get('licence')!r}. Permitted for this "
        "personal test environment only (ADR-0030); it may not ship commercially.",
        file=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    arts = entries()
    restricted = [n for n, spec in arts.items() if spec.get("commercial_use") is False]
    unresolved = [n for n, spec in arts.items() if is_unresolved(spec)]

    if args.list:
        for name, spec in arts.items():
            digest = (spec.get("sha256") or "")[:12] or "UNRESOLVED  "
            flag = "" if spec.get("commercial_use", True) else "  [non-commercial]"
            print(f"{name:24s} {digest}…  {spec['licence']}  {spec['file']}{flag}")
        _summary(restricted, unresolved)
        return

    for name in restricted:
        _licence_warning(name, arts[name])

    ok = True
    resolved = 0
    for name, spec in arts.items():
        if is_unresolved(spec):
            fetch(name, spec, args.verify_only)
            continue
        resolved += 1
        ok = fetch(name, spec, args.verify_only) and ok

    print(f"{resolved} resolved artefact(s) present and hash-verified")
    _summary(restricted, unresolved)
    if not ok:
        sys.exit(1)


def _summary(restricted: list[str], unresolved: list[str]) -> None:
    if restricted:
        print(
            f"non-commercial weights in this tree: {', '.join(restricted)} "
            "(ADR-0030 — not for a commercial deployment)",
            file=sys.stderr,
        )
    if unresolved:
        print(
            f"unresolved artefacts: {', '.join(unresolved)} — no pinned url/sha256 yet, "
            "so anything that needs them cannot run",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
