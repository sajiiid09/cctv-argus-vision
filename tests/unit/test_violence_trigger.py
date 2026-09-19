"""The violence trigger: features, threshold, cooldown, and what it refuses.

Every test here is about the trigger being a trigger. It produces a clip for a
human; it does not classify, it does not name anybody, and it has no column to
write a classifier score into (ADR-0012).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from argus.common.config import ConfigError, ViolenceConfig
from argus.pipelines.violence import (
    INSERT_CANDIDATE,
    ViolenceTrigger,
    extension_feature,
    proximity_feature,
    shoulder_width,
)

T0 = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)


def _person(
    x: float, y: float, *, width: float = 40.0, reach: float = 10.0, confidence: float = 0.9
) -> np.ndarray:
    """A COCO-shaped skeleton: shoulders `width` apart, wrists `reach` out."""
    person = np.zeros((17, 3), dtype=np.float32)
    person[:, 2] = confidence
    person[0] = (x, y - 40, confidence)  # nose
    person[5] = (x - width / 2, y, confidence)  # left shoulder
    person[6] = (x + width / 2, y, confidence)  # right shoulder
    person[9] = (x - width / 2 - reach, y + 10, confidence)  # left wrist
    person[10] = (x + width / 2 + reach, y + 10, confidence)  # right wrist
    person[11] = (x - width / 4, y + 60, confidence)  # left hip
    person[12] = (x + width / 4, y + 60, confidence)  # right hip
    return person


def _trigger(**kw) -> ViolenceTrigger:
    return ViolenceTrigger("floor_view", ViolenceConfig(**kw))


def test_one_person_standing_still_triggers_nothing() -> None:
    trigger = _trigger(enabled=True)
    assert trigger.observe(T0, [_person(320, 200)]) is None


def test_two_people_far_apart_score_no_proximity() -> None:
    assert proximity_feature([_person(50, 200), _person(600, 200)], 1.5) == 0.0


def test_proximity_is_measured_in_shoulder_widths_not_pixels() -> None:
    """Otherwise everything near the camera looks like a fight.

    Same separation in shoulder widths, different distance from the camera: the
    feature must agree.
    """
    near = proximity_feature([_person(300, 200, width=80), _person(380, 200, width=80)], 1.5)
    far = proximity_feature([_person(300, 200, width=20), _person(320, 200, width=20)], 1.5)
    assert near == pytest.approx(far, abs=0.05)


def test_an_extended_arm_scores_extension() -> None:
    tucked = extension_feature([_person(320, 200, reach=2.0)])
    extended = extension_feature([_person(320, 200, reach=60.0)])
    assert extended > tucked
    assert 0.0 <= tucked <= extended <= 1.0


def test_movement_between_samples_becomes_energy() -> None:
    trigger = _trigger(enabled=True)
    trigger.observe(T0, [_person(300, 200)])
    features = trigger.features_for([_person(360, 200)])
    assert features.energy > 0.5


def test_a_close_lunging_pair_crosses_the_threshold() -> None:
    trigger = _trigger(enabled=True, trigger_threshold=0.5, cooldown_s=30.0)
    # first sample establishes a previous pose for the energy feature
    trigger.observe(T0, [_person(300, 200, reach=5.0), _person(420, 200, reach=5.0)])
    candidate = trigger.observe(
        T0 + timedelta(seconds=0.5),
        [_person(330, 200, reach=60.0), _person(370, 200, reach=60.0)],
    )
    assert candidate is not None
    assert candidate.score >= 0.5
    assert candidate.features.people == 2


def test_a_scuffle_is_one_queue_item_not_forty() -> None:
    """The reviewer's attention is the scarce resource."""
    trigger = _trigger(enabled=True, trigger_threshold=0.3, cooldown_s=30.0)
    pair = [_person(330, 200, reach=60.0), _person(370, 200, reach=60.0)]
    first = trigger.observe(T0, pair)
    during = [trigger.observe(T0 + timedelta(seconds=s), pair) for s in (1, 2, 5, 10)]
    later = trigger.observe(T0 + timedelta(seconds=45), pair)
    assert first is not None
    assert all(candidate is None for candidate in during), "one scuffle, one queue item"
    assert later is not None, "and a fresh incident later is a fresh item"


def test_low_confidence_keypoints_are_ignored() -> None:
    """A pose the model is unsure about must not become an accusation."""
    trigger = _trigger(enabled=True, trigger_threshold=0.3)
    unsure = [
        _person(330, 200, reach=60.0, confidence=0.05),
        _person(370, 200, reach=60.0, confidence=0.05),
    ]
    trigger.observe(T0, unsure)
    assert trigger.observe(T0 + timedelta(seconds=0.5), unsure) is None


def test_the_weights_must_sum_to_one() -> None:
    with pytest.raises(ConfigError, match=r"sum to 1\.0"):
        ViolenceConfig(proximity_weight=0.9, extension_weight=0.9, energy_weight=0.9)


def test_the_candidate_row_has_no_person_and_no_classifier_score() -> None:
    trigger = _trigger(enabled=True, trigger_threshold=0.3)
    pair = [_person(330, 200, reach=60.0), _person(370, 200, reach=60.0)]
    candidate = trigger.observe(T0, pair)
    assert candidate is not None
    row = trigger.row(candidate, clip_id=None)
    assert "person" not in str(row).lower()
    assert "classifier" not in INSERT_CANDIDATE
    assert "person" not in INSERT_CANDIDATE
    assert "trigger_score" in INSERT_CANDIDATE


def test_the_features_are_recorded_so_a_reviewer_can_see_why() -> None:
    trigger = _trigger(enabled=True, trigger_threshold=0.3)
    pair = [_person(330, 200, reach=60.0), _person(370, 200, reach=60.0)]
    candidate = trigger.observe(T0, pair)
    assert candidate is not None
    assert set(candidate.features.as_dict()) == {"proximity", "extension", "energy", "people"}
    # A reviewer should be able to see why they were shown this clip.
    assert candidate.features.proximity > 0 and candidate.features.extension > 0


def test_shoulder_width_needs_both_shoulders() -> None:
    person = _person(320, 200)
    assert shoulder_width(person) == pytest.approx(40.0)
    person[5, 2] = 0.0
    assert shoulder_width(person) == 0.0


def test_the_module_never_reaches_a_verdict() -> None:
    """ADR-0012 and SOUL.md: the output is a queue item, not a conclusion."""
    from pathlib import Path

    import argus.pipelines.violence as module

    # Code shapes rather than words: the module has to be able to *say* it does
    # not classify, which is the same trap AGENTS.md §7 warns about for the
    # payroll wall-clock scan.
    text = Path(module.__file__).read_text()
    for token in (
        "def classify",
        ".classify(",
        "is_violence",
        "send_alert",
        "pg_notify",
        "review_state",
    ):
        assert token not in text, f"{token} has no business in a trigger"
