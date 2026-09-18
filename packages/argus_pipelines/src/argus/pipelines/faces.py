"""Identity at a doorway: quality gate, best-of-K, threshold *and* margin.

Everything here is written so that "unknown" is cheap and a wrong name is
expensive. The outcomes are explicit -- matched, below_threshold, near_tie,
no_face, low_quality -- and four of the five mean the doorway event carries no
person_id, which payroll turns into zero deduction and a flag.

Three decisions worth defending:

* **Best-of-K, never average-of-K.** Averaging embeddings across poses moves
  the vector off the manifold and the degradation is silent. Pick the single
  best-quality face near the crossing, align it, embed it once.
* **Quality gate first.** A crop below the gate is never embedded. Embedding a
  bad crop and then trusting the number that comes out is how a 1:N system
  invents a match.
* **Threshold and margin.** A top score above the threshold whose runner-up is
  almost as close is a near-tie, and a near-tie is unknown. ARCHITECTURE.md §4
  requires this and it is the easiest thing in the stack to forget.

There are no default thresholds anywhere in this module. ADR-0010 says the 1:N
canteen threshold and the 1:1 gate threshold must never share a constant, and
the cheapest way to guarantee that is for neither to have a fallback: with faces
enabled and no measured number in config, the process refuses to start.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from argus.backends.face_align import align_face
from argus.backends.interfaces import FaceDetector, FaceEmbedder
from argus.backends.types import Box, FaceDetection

log = logging.getLogger(__name__)

Reason = Literal["matched", "below_threshold", "near_tie", "no_face", "low_quality"]


class TemplateModelMismatch(Exception):
    """Stored templates were produced by a different model than the live one."""


@dataclass(frozen=True, slots=True)
class QualityGate:
    """Minimum face quality worth embedding. PROVISIONAL, and measured on the rig.

    These are not accuracy thresholds -- they are "is there enough pixels and
    enough face here to be worth asking". Real values come from real footage.
    """

    min_det_score: float = 0.5
    min_box_px: float = 24.0
    min_sharpness: float = 5.0
    max_frontality: float = 0.6


@dataclass(frozen=True, slots=True)
class FaceQuality:
    det_score: float
    # Face box size in SOURCE pixels, not analysis pixels: the substream is
    # smaller, and a threshold in analysis pixels would mean something different
    # per camera.
    box_px: float
    sharpness: float
    frontality: float

    def passes(self, gate: QualityGate) -> bool:
        return (
            self.det_score >= gate.min_det_score
            and self.box_px >= gate.min_box_px
            and self.sharpness >= gate.min_sharpness
            and self.frontality <= gate.max_frontality
        )

    def rank(self) -> float:
        return self.det_score * min(1.0, self.box_px / 64.0) * (1.0 - min(1.0, self.frontality))


@dataclass(frozen=True, slots=True)
class Identification:
    person_id: str | None
    score: float | None
    runner_up: float | None
    reason: Reason

    @property
    def is_match(self) -> bool:
        return self.person_id is not None


@dataclass(frozen=True, slots=True)
class Template:
    person_id: str
    template_id: str
    embedding: np.ndarray
    model_ref: str


class TemplateSet:
    """Enrolled templates, and the model they were produced by.

    A plain Python loop over float32 arrays, deliberately: this is a roster of
    tens, not thousands. pgvector and an ANN index would be more machinery than
    the problem has, and both would put the matching arithmetic somewhere the
    parity suite cannot see it.
    """

    def __init__(self, templates: Sequence[Template], model_ref: str) -> None:
        mismatched = sorted({t.model_ref for t in templates if t.model_ref != model_ref})
        if mismatched:
            raise TemplateModelMismatch(
                f"live embedder is {model_ref!r} but stored templates were produced by "
                f"{mismatched}. Embeddings are not comparable across models or versions "
                "(ARCHITECTURE.md §7.2) -- re-enrol before matching. "
                "`argus-enrol list --stale` shows which templates are affected."
            )
        self.model_ref = model_ref
        self._templates = list(templates)
        self._matrix = (
            np.stack([t.embedding for t in self._templates])
            if self._templates
            else np.zeros((0, 0), dtype=np.float32)
        )

    def __len__(self) -> int:
        return len(self._templates)

    @property
    def person_ids(self) -> list[str]:
        return sorted({t.person_id for t in self._templates})

    def match(
        self, embedding: np.ndarray, *, threshold: float, near_tie_margin: float
    ) -> Identification:
        """1:N identification. Cosine similarity, since everything is normalised."""
        if len(self._templates) == 0:
            return Identification(None, None, None, "below_threshold")
        scores = self._matrix @ embedding.astype(np.float32)
        # Best score per person: several templates of one person are alternatives,
        # not votes.
        best_by_person: dict[str, float] = {}
        for template, score in zip(self._templates, scores.tolist(), strict=True):
            current = best_by_person.get(template.person_id)
            if current is None or score > current:
                best_by_person[template.person_id] = float(score)
        ranked = sorted(best_by_person.items(), key=lambda kv: (-kv[1], kv[0]))
        top_person, top_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else None
        if top_score < threshold:
            return Identification(None, top_score, runner_up, "below_threshold")
        if runner_up is not None and (top_score - runner_up) < near_tie_margin:
            # Two people this close is not a match, it is a coin toss with a
            # wage attached.
            return Identification(None, top_score, runner_up, "near_tie")
        return Identification(top_person, top_score, runner_up, "matched")

    def verify(self, embedding: np.ndarray, person_id: str, *, threshold: float) -> Identification:
        """1:1 verification against one person's templates (the gate's question).

        A different threshold from match(), passed in by a caller that read a
        different config field. Same arithmetic, different problem.
        """
        theirs = [t for t in self._templates if t.person_id == person_id]
        if not theirs:
            return Identification(None, None, None, "no_face")
        scores = [float(t.embedding @ embedding.astype(np.float32)) for t in theirs]
        best = max(scores)
        if best < threshold:
            return Identification(None, best, None, "below_threshold")
        return Identification(person_id, best, None, "matched")


def quality_of(face: FaceDetection, frame: np.ndarray, scale_to_source: float = 1.0) -> FaceQuality:
    box = face.box
    width = (box.x2 - box.x1) * scale_to_source
    height = (box.y2 - box.y1) * scale_to_source
    crop = _crop(frame, box)
    sharpness = _sharpness(crop)
    return FaceQuality(
        det_score=face.score,
        box_px=min(width, height),
        sharpness=sharpness,
        frontality=_frontality(face.landmarks),
    )


def _crop(frame: np.ndarray, box: Box) -> np.ndarray:
    height, width = frame.shape[:2]
    x1 = max(0, int(box.x1))
    y1 = max(0, int(box.y1))
    x2 = min(width, int(box.x2) + 1)
    y2 = min(height, int(box.y2) + 1)
    if x2 <= x1 or y2 <= y1:
        return np.zeros((1, 1, 3), dtype=frame.dtype)
    return frame[y1:y2, x1:x2]


def _sharpness(crop: np.ndarray) -> float:
    """Variance of a 3x3 Laplacian, pure numpy.

    Motion blur at a doorway is the failure ARCHITECTURE.md §9.2 names as the
    most likely cause of a lower-than-expected capture rate, and it is invisible
    to a detector score.
    """
    if crop.size < 9:
        return 0.0
    gray = crop.mean(axis=2).astype(np.float64)
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    centre = gray[1:-1, 1:-1]
    laplacian = gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:] - 4.0 * centre
    return float(laplacian.var())


def _frontality(landmarks: np.ndarray) -> float:
    """0 is face-on; larger is more turned.

    Horizontal offset of the nose from the eye midpoint, over the inter-ocular
    distance. Crude, cheap, and enough to reject a profile before embedding it.
    """
    left_eye, right_eye, nose = landmarks[0], landmarks[1], landmarks[2]
    inter_ocular = float(np.linalg.norm(right_eye - left_eye))
    if inter_ocular == 0.0:
        return 1.0
    midpoint = (left_eye + right_eye) / 2.0
    return float(abs(nose[0] - midpoint[0]) / inter_ocular)


@dataclass(frozen=True, slots=True)
class FaceAttempt:
    identification: Identification
    quality: FaceQuality | None
    frames_considered: int


class FaceStage:
    """Detect, gate, align, embed, match -- once per crossing."""

    def __init__(
        self,
        detector: FaceDetector,
        embedder: FaceEmbedder,
        templates: TemplateSet,
        gate: QualityGate,
        *,
        threshold: float,
        near_tie_margin: float,
        best_of_k: int = 3,
    ) -> None:
        if templates.model_ref != getattr(embedder, "model_ref", templates.model_ref):
            raise TemplateModelMismatch(
                f"template set was built for {templates.model_ref!r}, embedder is "
                f"{embedder.model_ref!r}"
            )
        self.detector = detector
        self.embedder = embedder
        self.templates = templates
        self.gate = gate
        self.threshold = threshold
        self.near_tie_margin = near_tie_margin
        self.best_of_k = best_of_k

    def identify(self, frames: Sequence[np.ndarray], person_box: Box | None = None) -> FaceAttempt:
        best: tuple[FaceQuality, FaceDetection, np.ndarray] | None = None
        considered = 0
        for frame in list(frames)[: self.best_of_k]:
            considered += 1
            for face in self.detector.detect_faces(frame, score_threshold=self.gate.min_det_score):
                if person_box is not None and not _inside(face.box, person_box):
                    # A face outside the person box belongs to somebody else --
                    # the person behind them in the queue, most likely.
                    continue
                quality = quality_of(face, frame)
                if not quality.passes(self.gate):
                    continue
                if best is None or quality.rank() > best[0].rank():
                    best = (quality, face, frame)
        if best is None:
            return FaceAttempt(Identification(None, None, None, "no_face"), None, considered)
        quality, face, frame = best
        crop = align_face(frame, face.landmarks)
        embedding = self.embedder.embed(crop)
        identification = self.templates.match(
            embedding, threshold=self.threshold, near_tie_margin=self.near_tie_margin
        )
        return FaceAttempt(identification, quality, considered)


def _inside(inner: Box, outer: Box) -> bool:
    centre_x = (inner.x1 + inner.x2) / 2.0
    centre_y = (inner.y1 + inner.y2) / 2.0
    return outer.x1 <= centre_x <= outer.x2 and outer.y1 <= centre_y <= outer.y2
