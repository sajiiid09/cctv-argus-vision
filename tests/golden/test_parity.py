"""Golden-frame parity suite (ARCHITECTURE.md §5.3, TESTING.md §3).

The suite's job is not to prove backends agree; it is to make disagreement
visible and bounded. On this box it runs the CPU leg against the committed
reference; the CUDA leg runs on staging, the CoreML leg on the Mac
(ADR-0022), against the same artefact hash and the same tolerances.

Decisions matter more than numbers: a verification flip fails regardless of
how small the drift was (face embeddings will enforce that at M3).

Run a non-default leg with ``ARGUS_PARITY_BACKEND=onnx-cuda uv run pytest -m golden``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

GOLDEN = Path(__file__).resolve().parent
sys.path.insert(0, str(GOLDEN))

import pngio  # noqa: E402
from argus.backends.mock import MockDiskDetector  # noqa: E402
from argus.backends.onnx_common import ModelArtefactError  # noqa: E402
from argus.backends.registry import get_detector  # noqa: E402
from argus.backends.ssd import SsdOnnxDetector  # noqa: E402
from argus.backends.types import Box  # noqa: E402

pytestmark = pytest.mark.golden

THRESHOLD = 0.5
IOU_MATCH = 0.5  # pairing threshold for matching detections
IOU_TOLERANCE = 0.95  # ARCHITECTURE.md §5.3: matched-box IoU vs reference
SCORE_TOLERANCE = 0.05


# Which backend this run compares against the committed reference. The reference
# is produced on the CPU backend (ARCHITECTURE.md §5.3), so the default keeps CI
# honest; the CUDA leg on staging and the CoreML leg on the Mac (ADR-0022) run
# the same assertions against the same artefact hash by setting this. Without it
# the CUDA leg -- an outstanding M2 exit criterion -- cannot be run at all.
PARITY_BACKEND = os.environ.get("ARGUS_PARITY_BACKEND", "onnx-cpu")


def _detector():
    try:
        return get_detector(PARITY_BACKEND)
    except Exception as e:
        pytest.skip(f"{PARITY_BACKEND} backend unavailable on this box: {e}")


def _reference():
    ref_path = GOLDEN / "reference" / "ssd_mobilenet_v1.json"
    if not ref_path.exists():
        pytest.skip("no committed reference outputs; run make_reference.py")
    return json.loads(ref_path.read_text())


def _run_all(detector) -> dict[str, list]:
    out = {}
    for path in sorted((GOLDEN / "frames").glob("*.png")):
        arr = pngio.read_png(path)
        out[path.stem] = detector.detect(arr, score_threshold=THRESHOLD)
    return out


def test_artefact_hash_matches_reference() -> None:
    ref = _reference()
    detector = _detector()
    assert detector.model_ref == ref["model_ref"], (
        "reference was produced from a different model file; comparing different "
        "artefacts proves nothing (TESTING.md §3)"
    )


def test_determinism_same_backend() -> None:
    detector = _detector()
    first = _run_all(detector)
    second = _run_all(detector)
    for name in first:
        a = [(d.box, d.score, d.label_id) for d in first[name]]
        b = [(d.box, d.score, d.label_id) for d in second[name]]
        assert a == b, f"non-deterministic output for frame {name}"


def test_parity_against_reference() -> None:
    ref = _reference()
    detector = _detector()
    frames_ref: dict = ref["frames"]
    for name, ref_dets in frames_ref.items():
        arr = pngio.read_png(GOLDEN / "frames" / f"{name}.png")
        current = detector.detect(arr, score_threshold=THRESHOLD)
        ref_boxes = [Box(*d["box"]) for d in ref_dets["detections"]]
        ref_scores = [d["score"] for d in ref_dets["detections"]]

        matched: list[tuple[int, int]] = []
        used = set()
        for i, cbox in enumerate(d.box for d in current):
            best_j, best_iou = None, IOU_MATCH
            for j, rbox in enumerate(ref_boxes):
                if j in used:
                    continue
                iou = cbox.iou(rbox)
                if iou > best_iou:
                    best_j, best_iou = j, iou
            if best_j is not None:
                used.add(best_j)
                matched.append((i, best_j))
        assert len(current) == len(ref_boxes), (
            f"{name}: detection set mismatch — {len(ref_boxes)} reference vs "
            f"{len(current)} current above threshold {THRESHOLD}; a disappeared "
            "detection is a missed event (the failure that matters)"
        )
        assert len(matched) == len(ref_boxes), f"{name}: unmatched boxes remain"
        for i, j in matched:
            iou = current[i].box.iou(ref_boxes[j])
            assert iou >= IOU_TOLERANCE, (
                f"{name}: box IoU {iou:.3f} < {IOU_TOLERANCE} (box moved enough "
                "to change a door-line crossing)"
            )
            assert abs(current[i].score - ref_scores[j]) <= SCORE_TOLERANCE, (
                f"{name}: confidence drift {abs(current[i].score - ref_scores[j]):.3f} "
                f"> {SCORE_TOLERANCE} (can flip a threshold decision)"
            )


def test_real_photo_actually_detects() -> None:
    """The suite must contain at least one positive case, else it only tests
    that both backends find nothing — which proves nothing about boxes."""
    ref = _reference()
    total = sum(len(f["detections"]) for f in ref["frames"].values())
    assert total >= 1
    labels = {d["label"] for f in ref["frames"].values() for d in f["detections"]}
    assert "person" in labels


def test_missing_artefact_is_loud(tmp_path) -> None:
    # an explicitly wrong models root must fail, not silently fall back
    os.environ["ARGUS_MODELS_ROOT"] = str(tmp_path / "empty_models")
    try:
        with pytest.raises(ModelArtefactError):
            SsdOnnxDetector(providers=["CPUExecutionProvider"])
    finally:
        os.environ.pop("ARGUS_MODELS_ROOT", None)


class TestMockDetector:
    def test_finds_disk(self) -> None:
        import numpy as np

        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        yy, xx = np.mgrid[0:120, 0:160]
        frame[((yy - 60) ** 2 + (xx - 80) ** 2) < 20**2] = 220
        dets = MockDiskDetector().detect(frame)
        assert len(dets) == 1
        box = dets[0].box
        assert 60 < box.x1 < 80 and 80 < box.x2 < 100

    def test_empty_scene(self) -> None:
        import numpy as np

        assert MockDiskDetector().detect(np.zeros((120, 160, 3), dtype=np.uint8)) == []
