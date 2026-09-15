"""Fetch and verify model artefacts (ADR-0026).

python models/fetch.py                 # fetch everything missing/changed
python models/fetch.py --list
python models/fetch.py --verify-only
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


def fetch(name: str, spec: dict, verify_only: bool) -> bool:
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
    _check(tmp_path, spec, name, quiet=True)
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    arts = entries()
    if args.list:
        for name, spec in arts.items():
            print(f"{name:24s} {spec['sha256'][:12]}…  {spec['licence']}  {spec['file']}")
        return
    ok = True
    for name, spec in arts.items():
        ok = fetch(name, spec, args.verify_only) and ok
    if ok:
        print(f"all {len(arts)} artefact(s) present and hash-verified")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
