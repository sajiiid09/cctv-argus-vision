"""The face stage: unknown is cheap, a wrong name is expensive.

Every test here is about refusing to answer. The one thing this file cannot
test is whether two photographs of one person match -- that needs real faces and
a measured threshold, which is exactly why no threshold has a default.
"""

from __future__ import annotations

import numpy as np
import pytest
from argus.backends.mock import MockFaceDetector, MockFaceEmbedder
from argus.backends.types import Box
from argus.pipelines.faces import (
    FaceQuality,
    FaceStage,
    QualityGate,
    Template,
    TemplateModelMismatch,
    TemplateSet,
)


def _unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def _template(person_id: str, vector: np.ndarray, model_ref: str = "arc@abc") -> Template:
    return Template(
        person_id=person_id, template_id=f"t-{person_id}", embedding=vector, model_ref=model_ref
    )


def test_a_clear_match_is_a_match() -> None:
    templates = TemplateSet(
        [_template("p1", _unit(1, 0, 0)), _template("p2", _unit(0, 1, 0))], "arc@abc"
    )
    result = templates.match(_unit(0.98, 0.02, 0), threshold=0.5, near_tie_margin=0.05)
    assert result.person_id == "p1" and result.reason == "matched"


def test_below_the_threshold_is_unknown_not_the_closest_person() -> None:
    templates = TemplateSet([_template("p1", _unit(1, 0, 0))], "arc@abc")
    result = templates.match(_unit(0.3, 1, 0), threshold=0.6, near_tie_margin=0.05)
    assert result.person_id is None and result.reason == "below_threshold"
    assert result.score is not None, "the score is still recorded, just not acted on"


def test_a_near_tie_is_unknown_even_above_the_threshold() -> None:
    """Two people this close is a coin toss with a wage attached."""
    templates = TemplateSet(
        [_template("p1", _unit(1, 0, 0)), _template("p2", _unit(0.99, 0.14, 0))], "arc@abc"
    )
    result = templates.match(_unit(1, 0.07, 0), threshold=0.5, near_tie_margin=0.05)
    assert result.person_id is None and result.reason == "near_tie"
    assert result.runner_up is not None


def test_several_templates_of_one_person_are_alternatives_not_votes() -> None:
    templates = TemplateSet(
        [
            _template("p1", _unit(1, 0, 0)),
            Template("p1", "t-p1b", _unit(0, 1, 0), "arc@abc"),
            _template("p2", _unit(0, 0, 1)),
        ],
        "arc@abc",
    )
    result = templates.match(_unit(0, 1, 0), threshold=0.5, near_tie_margin=0.05)
    assert result.person_id == "p1"


def test_an_empty_template_set_matches_nobody() -> None:
    result = TemplateSet([], "arc@abc").match(_unit(1, 0, 0), threshold=0.1, near_tie_margin=0.01)
    assert result.person_id is None


def test_templates_from_another_model_refuse_to_load() -> None:
    """ARCHITECTURE.md §7.2: embeddings are not comparable across models, so a
    model upgrade invalidates every template and must fail at startup rather
    than produce confident nonsense."""
    with pytest.raises(TemplateModelMismatch, match="re-enrol"):
        TemplateSet([_template("p1", _unit(1, 0, 0), model_ref="arc@old")], "arc@new")


def test_verification_is_a_different_question_from_identification() -> None:
    templates = TemplateSet(
        [_template("p1", _unit(1, 0, 0)), _template("p2", _unit(0, 1, 0))], "arc@abc"
    )
    # 1:1 against the wrong person fails even though 1:N would have matched p1
    assert templates.verify(_unit(1, 0, 0), "p2", threshold=0.5).person_id is None
    assert templates.verify(_unit(1, 0, 0), "p1", threshold=0.5).person_id == "p1"
    # no template for that person: we could not have compared
    assert templates.verify(_unit(1, 0, 0), "p9", threshold=0.5).reason == "no_face"


def test_the_quality_gate_runs_before_the_model() -> None:
    gate = QualityGate(min_det_score=0.5, min_box_px=24.0, min_sharpness=5.0, max_frontality=0.6)
    assert FaceQuality(0.9, 40.0, 50.0, 0.1).passes(gate)
    assert not FaceQuality(0.4, 40.0, 50.0, 0.1).passes(gate)  # detector unsure
    assert not FaceQuality(0.9, 12.0, 50.0, 0.1).passes(gate)  # too few pixels
    assert not FaceQuality(0.9, 40.0, 1.0, 0.1).passes(gate)  # motion blur
    assert not FaceQuality(0.9, 40.0, 50.0, 0.9).passes(gate)  # profile


def test_no_face_is_its_own_outcome() -> None:
    stage = FaceStage(
        MockFaceDetector(),
        MockFaceEmbedder(),
        TemplateSet([], "mock-embed@000000000000"),
        QualityGate(),
        threshold=0.5,
        near_tie_margin=0.05,
    )
    blank = np.zeros((180, 320, 3), dtype=np.uint8)
    attempt = stage.identify([blank])
    assert attempt.identification.reason == "no_face"
    assert attempt.quality is None


def test_the_stage_embeds_once_and_matches_against_the_roster() -> None:
    detector = MockFaceDetector()
    embedder = MockFaceEmbedder()
    frame = np.full((180, 320, 3), 44, dtype=np.uint8)
    ys, xs = np.ogrid[:180, :320]
    frame[(xs - 160) ** 2 + (ys - 90) ** 2 <= 30**2] = 190

    # enrol from the same frame, so the mock embedding matches exactly
    from argus.backends.face_align import align_face

    face = detector.detect_faces(frame)[0]
    enrolled = embedder.embed(align_face(frame, face.landmarks))
    templates = TemplateSet(
        [Template("p1", "t1", enrolled, embedder.model_ref)], embedder.model_ref
    )
    # The mock's "face" is the upper third of a 60px blob, so ~20px of face.
    # A real gate rejects that and should: a 20-pixel face is not identifiable.
    # The gate is lowered here to exercise the stage, not to bless the crop.
    stage = FaceStage(
        detector,
        embedder,
        templates,
        QualityGate(min_box_px=8.0, min_sharpness=0.0),
        threshold=0.9,
        near_tie_margin=0.05,
    )
    attempt = stage.identify([frame])
    assert attempt.identification.person_id == "p1"
    assert attempt.frames_considered == 1


def test_a_face_outside_the_person_box_is_somebody_else() -> None:
    detector = MockFaceDetector()
    embedder = MockFaceEmbedder()
    frame = np.full((180, 320, 3), 44, dtype=np.uint8)
    ys, xs = np.ogrid[:180, :320]
    frame[(xs - 60) ** 2 + (ys - 90) ** 2 <= 25**2] = 190
    stage = FaceStage(
        detector,
        embedder,
        TemplateSet([], embedder.model_ref),
        QualityGate(min_box_px=8.0, min_sharpness=0.0),
        threshold=0.5,
        near_tie_margin=0.05,
    )
    # person box on the far side of the frame: the queue behind them
    attempt = stage.identify([frame], person_box=Box(250.0, 40.0, 300.0, 140.0))
    assert attempt.identification.reason == "no_face"
