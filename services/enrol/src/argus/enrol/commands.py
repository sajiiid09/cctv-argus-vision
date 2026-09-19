"""One function per verb. Every write records who asked for it."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from argus.enrol.consent import active_consent
from argus.enrol.images import delete_person_images, read_image, store_image
from argus.enrol.templates import (
    Candidate,
    EnrolmentError,
    check_not_someone_else,
    embed_image,
    stale_templates,
    store_template,
)
from argus.store.db import Database

log = logging.getLogger(__name__)


@dataclass
class Report:
    """What happened, in lines a human can read, plus whether anything changed."""

    lines: list[str] = field(default_factory=list)
    changed: bool = False

    def say(self, line: str) -> None:
        self.lines.append(line)

    def render(self) -> str:
        return "\n".join(self.lines)


async def _audit(
    db: Database, actor: str, action: str, person_id: str | None, **detail: Any
) -> None:
    import json

    await db.execute(
        "insert into enrolment_action (action_id, actor, action, person_id, detail)"
        " values (%s,%s,%s,%s,%s)",
        (uuid4(), actor, action, person_id, json.dumps(detail, default=str)),
    )


async def person_add(
    db: Database,
    *,
    person_id: str,
    employee_ref: str,
    display_name: str | None,
    line_id: str | None,
    seat_id: str | None,
    active_from: date,
    by: str,
    dry_run: bool,
) -> Report:
    report = Report()
    existing = await db.fetch_one("select person_id from person where person_id = %s", (person_id,))
    if existing is not None:
        report.say(f"{person_id} is already on the roster")
        return report
    report.say(f"add {person_id} (employee_ref {employee_ref}) active from {active_from}")
    if dry_run:
        report.say("--dry-run: nothing written")
        return report
    await db.execute(
        "insert into person (person_id, employee_ref, display_name, line_id, seat_id,"
        " active_from) values (%s,%s,%s,%s,%s,%s)",
        (person_id, employee_ref, display_name, line_id, seat_id, active_from),
    )
    await _audit(db, by, "person_add", person_id, employee_ref=employee_ref)
    report.changed = True
    return report


async def consent_record(
    db: Database,
    *,
    person_id: str,
    purpose: str,
    granted_on: date,
    expires_on: date,
    evidence_note: str | None,
    by: str,
    dry_run: bool,
) -> Report:
    report = Report()
    report.say(f"record consent for {person_id}: {purpose}, {granted_on} to {expires_on}, by {by}")
    if dry_run:
        report.say("--dry-run: nothing written")
        return report
    consent_id = uuid4()
    await db.execute(
        "insert into consent_record (consent_id, person_id, purpose, granted_on, expires_on,"
        " recorded_by, evidence_note) values (%s,%s,%s,%s,%s,%s,%s)",
        (consent_id, person_id, purpose, granted_on, expires_on, by, evidence_note),
    )
    await _audit(db, by, "consent_record", person_id, consent_id=consent_id, expires_on=expires_on)
    report.say(f"consent {consent_id}")
    report.changed = True
    return report


async def consent_revoke(
    db: Database, *, person_id: str, by: str, when: datetime, dry_run: bool
) -> Report:
    report = Report()
    rows = await db.fetch_all(
        "select consent_id from consent_record where person_id = %s and revoked_at is null",
        (person_id,),
    )
    if not rows:
        report.say(f"{person_id} has no active consent to withdraw")
        return report
    report.say(f"withdraw {len(rows)} consent record(s) for {person_id}")
    report.say("their templates and images become purgeable: run `purge --expired` next")
    if dry_run:
        report.say("--dry-run: nothing written")
        return report
    await db.execute(
        "update consent_record set revoked_at = %s where person_id = %s and revoked_at is null",
        (when, person_id),
    )
    await _audit(db, by, "consent_revoke", person_id, count=len(rows))
    report.changed = True
    return report


async def enrol_from_images(
    db: Database,
    *,
    person_id: str,
    directory: Path,
    root: Path,
    detector: Any,
    embedder: Any,
    today: date,
    match_threshold: float | None,
    min_det_score: float,
    by: str,
    force: bool,
    dry_run: bool,
) -> Report:
    report = Report()
    consent = await active_consent(db, person_id, today)
    report.say(f"consent {consent.consent_id} valid to {consent.expires_on}")

    paths = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
    )
    if not paths:
        raise EnrolmentError(f"no images found in {directory}")

    model_ref = embedder.model_ref
    report.say(f"embedder {model_ref}")
    candidates: list[tuple[Candidate, Path]] = []
    for path in paths:
        image = read_image(path)
        try:
            candidate = embed_image(
                image.pixels,
                image.sha256,
                detector,
                embedder,
                min_det_score=min_det_score,
            )
        except EnrolmentError as exc:
            report.say(f"  skip {path.name}: {exc}")
            continue
        if not force:
            await check_not_someone_else(
                db, person_id, candidate, model_ref, threshold=match_threshold
            )
        candidates.append((candidate, path))
        report.say(f"  {path.name} -> template (quality {candidate.quality:.3f})")

    if not candidates:
        raise EnrolmentError("no usable face in any of the supplied images")
    if dry_run:
        report.say(f"--dry-run: {len(candidates)} template(s) not written")
        return report

    for candidate, path in candidates:
        stored_path = store_image(root, person_id, read_image(path), suffix=path.suffix.lower())
        await db.execute(
            "insert into enrol_image (person_id, image_sha256, rel_path, source, added_by)"
            " values (%s,%s,%s,'images',%s) on conflict (person_id, image_sha256) do nothing",
            (person_id, candidate.image_sha256, str(stored_path.relative_to(root)), by),
        )
        stored = await store_template(
            db,
            person_id,
            consent.consent_id,
            candidate,
            model_ref=model_ref,
            enrolled_by=by,
            source="images",
        )
        report.say(f"  template {stored.template_id} dim={stored.dim}")
    await _audit(
        db,
        by,
        "enrol",
        person_id,
        templates=len(candidates),
        model_ref=model_ref,
        forced=force,
    )
    report.changed = True
    return report


async def badge_assign(
    db: Database, *, person_id: str, badge_id: str, when: datetime, by: str, dry_run: bool
) -> Report:
    report = Report()
    report.say(f"assign badge {badge_id} to {person_id} from {when.isoformat()}")
    if dry_run:
        report.say("--dry-run: nothing written")
        return report
    await db.execute(
        "insert into badge_assignment (assignment_id, badge_id, person_id, assigned_from,"
        " assigned_by) values (%s,%s,%s,%s,%s)",
        (uuid4(), badge_id, person_id, when, by),
    )
    await _audit(db, by, "badge_assign", person_id, badge_id=badge_id)
    report.changed = True
    return report


async def badge_unassign(
    db: Database, *, badge_id: str, when: datetime, by: str, dry_run: bool
) -> Report:
    report = Report()
    row = await db.fetch_one(
        "select person_id from badge_assignment where badge_id = %s and assigned_to is null",
        (badge_id,),
    )
    if row is None:
        report.say(f"badge {badge_id} has no active holder")
        return report
    report.say(f"unassign badge {badge_id} from {row[0]} at {when.isoformat()}")
    if dry_run:
        report.say("--dry-run: nothing written")
        return report
    await db.execute(
        "update badge_assignment set assigned_to = %s where badge_id = %s and assigned_to is null",
        (when, badge_id),
    )
    await _audit(db, by, "badge_unassign", row[0], badge_id=badge_id)
    report.changed = True
    return report


async def list_people(
    db: Database,
    *,
    live_model_ref: str | None,
    with_templates: bool,
    stale: bool,
    expiring_within: int | None,
    today: date,
) -> Report:
    report = Report()
    if stale:
        if live_model_ref is None:
            report.say("no embedder available, so nothing can be compared against it")
            return report
        rows = await stale_templates(db, live_model_ref)
        report.say(f"live embedder: {live_model_ref}")
        if not rows:
            report.say("no stale templates")
        for person_id, model_ref, count in rows:
            report.say(f"  {person_id}: {count} template(s) from {model_ref} -- re-enrol")
        return report

    people = await db.fetch_all(
        "select p.person_id, p.employee_ref, p.display_name,"
        " (select count(*) from face_template t"
        "   where t.person_id = p.person_id and t.retired_at is null) as templates,"
        " (select max(c.expires_on) from consent_record c"
        "   where c.person_id = p.person_id and c.revoked_at is null) as consent_to,"
        " (select b.badge_id from badge_assignment b"
        "   where b.person_id = p.person_id and b.assigned_to is null limit 1) as badge"
        " from person p order by p.person_id"
    )
    for person_id, employee_ref, display_name, templates, consent_to, badge in people:
        if expiring_within is not None and (
            consent_to is None or (consent_to - today).days > expiring_within
        ):
            continue
        line = f"{person_id} ({employee_ref})"
        if display_name:
            line += f" {display_name}"
        line += f" templates={templates} badge={badge or '-'}"
        line += f" consent_to={consent_to.isoformat() if consent_to else 'NONE'}"
        if consent_to is not None and consent_to < today:
            line += " EXPIRED"
        report.say(line)
    if with_templates:
        detail = await db.fetch_all(
            "select person_id, template_id, model_ref, enrolled_at, enrolled_by"
            " from face_template where retired_at is null order by person_id, enrolled_at"
        )
        for person_id, template_id, model_ref, enrolled_at, enrolled_by in detail:
            report.say(f"  {person_id} {template_id} {model_ref} by {enrolled_by} {enrolled_at}")
    if not people:
        report.say("nobody is enrolled")
    return report


async def purge_expired(db: Database, *, root: Path, today: date, by: str, dry_run: bool) -> Report:
    """Delete templates AND images for expired or withdrawn consent, together.

    ADR-0027 names the easy mistake: removing the video and leaving the
    embeddings behind. So both go in one transaction, and the audit row records
    counts and person ids -- never an embedding.
    """
    report = Report()
    rows = await db.fetch_all(
        "select distinct p.person_id from person p"
        " where exists (select 1 from face_template t where t.person_id = p.person_id)"
        "   and not exists ("
        "     select 1 from consent_record c where c.person_id = p.person_id"
        "       and c.revoked_at is null and c.expires_on >= %s)"
        " order by p.person_id",
        (today,),
    )
    if not rows:
        report.say("nothing to purge: every template is covered by live consent")
        return report
    for (person_id,) in rows:
        counts = await db.fetch_one(
            "select (select count(*) from face_template where person_id = %s),"
            " (select count(*) from enrol_image where person_id = %s)",
            (person_id, person_id),
        )
        templates, images = counts if counts else (0, 0)
        report.say(f"{person_id}: {templates} template(s), {images} image(s)")
    if dry_run:
        report.say("--dry-run: nothing deleted")
        return report

    async with db.conn.transaction():
        for (person_id,) in rows:
            deleted = delete_person_images(root, person_id)
            async with db.conn.cursor() as cur:
                await cur.execute("delete from enrol_image where person_id = %s", (person_id,))
                await cur.execute("delete from face_template where person_id = %s", (person_id,))
            report.say(f"{person_id}: removed {len(deleted)} file(s) and its templates")
            await _audit(db, by, "purge", person_id, files=len(deleted))
    report.changed = True
    return report
