"""Regenerate golden reference outputs (AGENTS.md §7).

The reference backend is ONNX Runtime CPU fp32 — available everywhere
including GPU-less CI, and numerically the most boring (ARCHITECTURE.md §5.3).
Run on the reference machine only; the output JSON is committed.

    ARGUS_MODELS_ROOT=models uv run --no-sync python tests/golden/make_reference.py
    ARGUS_MODELS_ROOT=models uv run --no-sync python tests/golden/make_reference.py yolo26m

One reference **per artefact**, named after it. A reference is a claim about one
model file: `reference/ssd_mobilenet_v1.json` says nothing about `yolo26m`, and
comparing a YOLO run against SSD's numbers would fail on the artefact-hash
assertion first, which is the suite working rather than the suite being awkward.

Regenerating a committed reference is a decision, not a fix. If a reference
stops matching and the artefact hash has not changed, something in
preprocessing or decoding moved and that is the thing to look at — overwriting
the file makes the divergence permanent and invisible.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pngio
from argus.backends.registry import get_detector

THRESHOLD = 0.5

# artefact -> the CPU backend name that loads it. The reference leg is always
# CPU (ARCHITECTURE.md §5.3); the CUDA and CoreML legs compare against what
# this writes.
CPU_BACKEND = {
    "ssd_mobilenet_v1": "onnx-cpu",
    "yolo26m": "onnx-cpu-yolo",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artefact",
        nargs="?",
        default="ssd_mobilenet_v1",
        choices=sorted(CPU_BACKEND),
        help="which model artefact to produce a reference for",
    )
    args = parser.parse_args()

    detector = get_detector(CPU_BACKEND[args.artefact])
    frames_dir = Path(__file__).resolve().parent / "frames"
    reference: dict[str, object] = {
        "backend": detector.name,
        "model_ref": detector.model_ref,
        "score_threshold": THRESHOLD,
        "frames": {},
    }
    frames = reference["frames"]
    assert isinstance(frames, dict)
    for path in sorted(frames_dir.glob("*.png")):
        arr = pngio.read_png(path)
        dets = detector.detect(arr, score_threshold=THRESHOLD)
        frames[path.stem] = {
            "shape": list(arr.shape),
            "detections": [
                {
                    "box": [d.box.x1, d.box.y1, d.box.x2, d.box.y2],
                    "score": round(d.score, 6),
                    "label": d.label,
                    "label_id": d.label_id,
                }
                for d in dets
            ],
        }
        print(f"{path.stem}: {len(dets)} detections")
    out = Path(__file__).resolve().parent / "reference" / f"{args.artefact}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(reference, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
