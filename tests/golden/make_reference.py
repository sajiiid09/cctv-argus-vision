"""Regenerate golden reference outputs (AGENTS.md §7).

The reference backend is ONNX Runtime CPU fp32 — available everywhere
including GPU-less CI, and numerically the most boring (ARCHITECTURE.md §5.3).
Run on the reference machine only; the output JSON is committed.

    ARGUS_MODELS_ROOT=models uv run --no-sync python tests/golden/make_reference.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pngio
from argus.backends.onnx_backends import OnnxCpuDetector

THRESHOLD = 0.5


def main() -> None:
    detector = OnnxCpuDetector()
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
    out = Path(__file__).resolve().parent / "reference" / "ssd_mobilenet_v1.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(reference, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
