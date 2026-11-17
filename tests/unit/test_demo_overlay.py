"""Unit tests for stairwell counting, false-positive filtering, and face snapshot handoff."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from argus.backends.types import Box, Detection, FaceDetection

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "demo_overlay", ROOT / "rig" / "bin" / "demo_overlay.py"
)
assert SPEC is not None and SPEC.loader is not None
demo_overlay = importlib.util.module_from_spec(SPEC)
sys.modules["demo_overlay"] = demo_overlay
SPEC.loader.exec_module(demo_overlay)


def test_cam10_full_frame_hallucination_is_rejected() -> None:
    """Cam 10 previously produced a 93.8% full-frame false person box; it must be rejected."""
    # Substream 352x288 false positive observed on Cam 10
    bad_sub = Detection(box=Box(6.0, 10.0, 351.0, 286.0), score=0.41, label="person", label_id=0)
    assert not demo_overlay.is_valid_person_detection(bad_sub, 352, 288)

    # Mainstream 960x1088 oversize wall/railing false positive
    bad_main = Detection(box=Box(20.0, 15.0, 920.0, 1050.0), score=0.50, label="person", label_id=0)
    assert not demo_overlay.is_valid_person_detection(bad_main, 960, 1088)

    # Wide horizontal railing artifact
    bad_wide = Detection(
        box=Box(100.0, 400.0, 500.0, 550.0), score=0.60, label="person", label_id=0
    )
    assert not demo_overlay.is_valid_person_detection(bad_wide, 960, 1088)

    # Realistic person walking on stairs (width 130, height 480 in 960x1088)
    good_person = Detection(
        box=Box(420.0, 180.0, 550.0, 660.0), score=0.72, label="person", label_id=0
    )
    assert demo_overlay.is_valid_person_detection(good_person, 960, 1088)


def test_cam12_is_excluded_and_same_person_counted_as_one_across_stairwell(tmp_path: Path) -> None:
    """Cam 12 (id=1) is excluded; across C2..C6 the same person is counted as ONE."""
    tracker = demo_overlay.StairwellGlobalTracker(sim_threshold=0.55, snapshot_dir=tmp_path)

    feat = np.ones(48, dtype=np.float32)
    feat /= float(np.linalg.norm(feat))

    # Cam 12 (cam_id == 1) must be refused by StairwellGlobalTracker
    with pytest.raises(ValueError, match="Cam 12"):
        tracker.match_or_create(1, "Cam 1 (A12)", feat, 10.0)

    # Person enters C2 (Cam 7) -> counted as Person #1
    gid1, is_new1 = tracker.match_or_create(2, "Cam 2 (A7)", feat, 10.0)
    assert (gid1, is_new1, tracker.headcount) == (1, True, 1)

    # Same person moves to C3 (Cam 10), C4 (Cam 5), C5 (Cam 13), C6 (Cam 2)
    for cid, cname, ts in [
        (3, "Cam 3 (A10)", 22.0),
        (4, "Cam 4 (A5)", 35.0),
        (5, "Cam 5 (A13)", 48.0),
        (6, "Cam 6 (A2)", 61.0),
    ]:
        gid, is_new = tracker.match_or_create(cid, cname, feat, ts)
        assert gid == 1
        assert is_new is False
        assert tracker.headcount == 1


def test_face_snapshot_handoff_skips_bad_angle_and_captures_best_score(tmp_path: Path) -> None:
    """If a camera never gets a proper face angle, it skips and hands off to the next one."""
    tracker = demo_overlay.StairwellGlobalTracker(sim_threshold=0.55, snapshot_dir=tmp_path)
    feat = np.ones(48, dtype=np.float32) / np.sqrt(48.0)

    gid, _ = tracker.match_or_create(2, "Cam 2 (A7)", feat, 100.0)
    assert gid == 1

    # C2 (Cam 7) never gets a proper face angle -> marks handoff, takes no snapshot
    handed_off = tracker.mark_camera_no_angle_handoff(2, gid, 108.0)
    assert handed_off is True
    person = tracker.get_person(gid)
    assert person is not None
    assert person.skipped_cameras == [2]
    assert person.best_face_crop is None
    assert tracker.captured_count == 0

    # Person transitions to C3 (Cam 10), which also fails to get a proper angle
    gid_c3, _ = tracker.match_or_create(3, "Cam 3 (A10)", feat, 112.0)
    assert gid_c3 == 1
    tracker.mark_camera_no_angle_handoff(3, gid, 120.0)
    assert person.skipped_cameras == [2, 3]
    assert tracker.captured_count == 0

    # Person reaches C4 (Cam 5) and gets a valid face score 0.42, then a better score 0.68
    crop1 = np.full((160, 160, 3), 120, dtype=np.uint8)
    crop2 = np.full((160, 160, 3), 200, dtype=np.uint8)
    _, updated1 = tracker.update_face_candidate(
        4, "Cam 4 (A5)", gid, 0.42, crop1, None, feat, 125.0
    )
    assert updated1 is True
    assert person.best_face_score == 0.42
    assert person.best_face_cam_id == 4
    assert tracker.captured_count == 1

    # Lower score on a later frame does NOT overwrite the peak snapshot
    _, updated_low = tracker.update_face_candidate(
        4, "Cam 4 (A5)", gid, 0.35, crop1, None, feat, 126.0
    )
    assert updated_low is False
    assert person.best_face_score == 0.42

    # Higher score updates the best zoomed-in snapshot and writes PNG to disk
    _, updated2 = tracker.update_face_candidate(
        4, "Cam 4 (A5)", gid, 0.68, crop2, None, feat, 127.0
    )
    assert updated2 is True
    assert person.best_face_score == 0.68
    assert (tmp_path / "person_001_best.png").is_file()


def test_evaluate_face_visibility_rejects_turned_profile_and_accepts_frontal() -> None:
    """Profile/turned faces fail the angle gate; sharp frontal faces pass."""
    # Create a textured frame so Laplacian sharpness is high
    rng = np.random.default_rng(42)
    frame = rng.integers(40, 220, size=(400, 400, 3), dtype=np.uint8)

    # Frontal face landmarks: symmetric eyes, centered nose, and level mouth corners
    frontal_kps = np.array(
        [[180.0, 150.0], [220.0, 150.0], [200.0, 175.0], [185.0, 200.0], [215.0, 200.0]],
        dtype=np.float32,
    )
    frontal_face = FaceDetection(
        box=Box(160.0, 125.0, 240.0, 225.0),
        score=0.75,
        landmarks=frontal_kps,
    )
    res_good = demo_overlay.evaluate_face_visibility(frontal_face, frame)
    assert res_good.passes_gate is True
    assert res_good.score > 0.40
    assert res_good.zoom_crop is not None
    assert res_good.zoom_crop.shape == (160, 160, 3)

    # Steep side profile: nose shifted far past right eye
    profile_kps = np.array(
        [[180.0, 150.0], [205.0, 150.0], [225.0, 175.0], [190.0, 200.0], [210.0, 200.0]],
        dtype=np.float32,
    )
    profile_face = FaceDetection(
        box=Box(160.0, 125.0, 240.0, 225.0),
        score=0.75,
        landmarks=profile_kps,
    )
    res_bad = demo_overlay.evaluate_face_visibility(profile_face, frame)
    assert res_bad.passes_gate is False
    assert res_bad.zoom_crop is None
