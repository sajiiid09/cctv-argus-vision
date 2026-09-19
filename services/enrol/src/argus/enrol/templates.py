"""Turning a photograph into a template, and refusing to when it should not be.

Two rules are worth more than the model choice:

* **model_ref comes from the embedder, never from config.** Embeddings are not
  comparable across models or even versions (ARCHITECTURE.md §7.2), so the
  artefact that produced a template is recorded with it, and a mismatch is a
  startup error everywhere the templates are read.
* **One face, one person.** After embedding, the new template is compared
  against the existing roster; a match to a *different* person is refused. One
  human enrolled under two ids quietly ruins pairing and gate verification at
  once, and it is invisible until somebody audits a report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

import numpy as np
from argus.backends.face_align import align_face
from argus.backends.interfaces import FaceDetector, FaceEmbedder
from argus.store.db import Database

log = logging.getLogger(__name__)


class EnrolmentError(Exception):
    pass


class DuplicateIdentity(EnrolmentError):
    pass


@dataclass(frozen=True, slots=True)
class Candidate:
    embedding: np.ndarray
    quality: float
    image_sha256: str


@dataclass(frozen=True, slots=True)
class StoredTemplate:
    template_id: UUID
    person_id: str
    model_ref: str
    dim: int


def embed_image(
    pixels: np.ndarray,
    image_sha256: str,
    detector: FaceDetector,
    embedder: FaceEmbedder,
    *,
    min_det_score: float = 0.5,
) -> Candidate:
    faces = detector.detect_faces(pixels, score_threshold=min_det_score)
    if not faces:
        raise EnrolmentError(
            f"no face found in {image_sha256[:12]}; enrolment needs a clear, face-on photograph"
        )
    if len(faces) > 1:
        # Refusing is the right answer: guessing which face in a group photo is
        # the person being enrolled is how somebody ends up enrolled as somebody
        # else.
        raise EnrolmentError(
            f"{len(faces)} faces in {image_sha256[:12]}; enrol from a photograph of one person"
        )
    face = faces[0]
    crop = align_face(pixels, face.landmarks)
    embedding = embedder.embed(crop)
    return Candidate(embedding=embedding, quality=face.score, image_sha256=image_sha256)


async def existing_templates(db: Database, model_ref: str) -> list[tuple[str, np.ndarray]]:
    rows = await db.fetch_all(
        "select person_id, embedding, dim from face_template"
        " where retired_at is null and model_ref = %s",
        (model_ref,),
    )
    return [(row[0], np.frombuffer(row[1], dtype=np.float32, count=row[2])) for row in rows]


async def check_not_someone_else(
    db: Database,
    person_id: str,
    candidate: Candidate,
    model_ref: str,
    *,
    threshold: float | None,
) -> None:
    """Refuse a face that already belongs to a different person.

    Skipped when no threshold has been measured (ADR-0010: no defaults). That is
    a real gap and it is stated rather than papered over with a guess -- with no
    threshold there is no way to say whether two embeddings are the same person.
    """
    if threshold is None:
        log.warning(
            "no face.canteen_match_threshold configured, so the duplicate-identity check is "
            "skipped: two ids for one person cannot be detected until a threshold is measured "
            "(ADR-0010)"
        )
        return
    for other_person, embedding in await existing_templates(db, model_ref):
        if other_person == person_id:
            continue
        score = float(embedding @ candidate.embedding)
        if score >= threshold:
            raise DuplicateIdentity(
                f"this face matches {other_person} at {score:.3f} (threshold {threshold:.3f}). "
                f"Enrolling it as {person_id} would give one human two identities, which "
                "breaks pairing and gate verification at the same time. Use --force with a "
                "reason if they really are different people."
            )


async def store_template(
    db: Database,
    person_id: str,
    consent_id: UUID,
    candidate: Candidate,
    *,
    model_ref: str,
    enrolled_by: str,
    source: str,
) -> StoredTemplate:
    template_id = uuid4()
    embedding = np.ascontiguousarray(candidate.embedding, dtype=np.float32)
    await db.execute(
        "insert into face_template (template_id, person_id, consent_id, model_ref, embedding,"
        " dim, quality_score, source, image_sha256, enrolled_by)"
        " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            template_id,
            person_id,
            consent_id,
            model_ref,
            embedding.tobytes(),
            int(embedding.size),
            candidate.quality,
            source,
            candidate.image_sha256,
            enrolled_by,
        ),
    )
    return StoredTemplate(
        template_id=template_id,
        person_id=person_id,
        model_ref=model_ref,
        dim=int(embedding.size),
    )


async def stale_templates(db: Database, live_model_ref: str) -> list[tuple[str, str, int]]:
    """Templates produced by a model other than the live one.

    What `list --stale` shows, and what the startup check refuses to run with.
    """
    return await db.fetch_all(
        "select person_id, model_ref, count(*) from face_template"
        " where retired_at is null and model_ref <> %s"
        " group by person_id, model_ref order by person_id",
        (live_model_ref,),
    )
