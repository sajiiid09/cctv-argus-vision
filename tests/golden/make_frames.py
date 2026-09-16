"""Generate the committed golden frames (TESTING.md §3).

Run once (or when the set is deliberately changed):
    uv run --no-sync python tests/golden/make_frames.py

Frames mix synthetic geometry (deterministic, zero detections expected — the
negative parity case) with one real public-domain photograph (exercises real
detections). Provenance and licences live in frames.yaml.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rig" / "synthetic"))

import pngio
from generate import _canteen_frame, _canteen_schedule, _floor_frame, _gate_frame

FRAMES = Path(__file__).resolve().parent / "frames"
ASTRONAUT_URL = (
    "https://raw.githubusercontent.com/scikit-image/scikit-image/v0.25.0/skimage/data/astronaut.png"
)


def main() -> None:
    FRAMES.mkdir(parents=True, exist_ok=True)
    frames = {
        "doorway_empty": _canteen_frame(10.0, _canteen_schedule(0.0)),
        "doorway_single": _canteen_frame(4.0, _canteen_schedule(0.0)),
        "doorway_two_overlap": _canteen_frame(21.6, _canteen_schedule(0.0)),
        "floor_grid": _floor_frame(25.0),
        "gate_closeup": _gate_frame(9.0),
    }
    astronaut_local = Path("/tmp/opencode/astronaut2.png")
    if not astronaut_local.exists():
        urllib.request.urlretrieve(ASTRONAUT_URL, astronaut_local)
    frames["real_person_01"] = pngio.read_png(astronaut_local)
    for name, arr in frames.items():
        assert arr.dtype == np.uint8 and arr.ndim == 3 and arr.shape[2] == 3, name
        pngio.write_png(FRAMES / f"{name}.png", arr)
        print(f"wrote {name}.png {arr.shape}")


if __name__ == "__main__":
    main()
