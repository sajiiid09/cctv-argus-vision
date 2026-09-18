"""Consent: the gate a template cannot be created without."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from argus.store.db import Database


class ConsentError(Exception):
    """Base for every reason a template may not be created."""


class ConsentMissing(ConsentError):
    pass


class ConsentExpired(ConsentError):
    pass


class ConsentRevoked(ConsentError):
    pass


@dataclass(frozen=True, slots=True)
class Consent:
    consent_id: UUID
    person_id: str
    purpose: str
    granted_on: date
    expires_on: date
    revoked_at: object | None


async def active_consent(db: Database, person_id: str, today: date) -> Consent:
    """The consent a template will be attached to, or a reason there is none.

    Three distinct failures, because they need three different responses: nobody
    asked (go and ask), the permission ran out (ask again), the person withdrew
    it (delete what we have and do not ask again unprompted).
    """
    rows = await db.fetch_all(
        "select consent_id, person_id, purpose, granted_on, expires_on, revoked_at"
        " from consent_record where person_id = %s order by granted_on desc",
        (person_id,),
    )
    if not rows:
        raise ConsentMissing(
            f"no consent recorded for {person_id}. A face template without consent is "
            "not something this system can store (ADR-0027): record it with "
            "`argus-enrol consent record --person ... --expires ... --by ...`"
        )
    revoked = [row for row in rows if row[5] is not None]
    usable = [row for row in rows if row[5] is None and row[4] >= today]
    if usable:
        row = usable[0]
        return Consent(
            consent_id=row[0],
            person_id=row[1],
            purpose=row[2],
            granted_on=row[3],
            expires_on=row[4],
            revoked_at=None,
        )
    if revoked:
        raise ConsentRevoked(
            f"consent for {person_id} was withdrawn at {revoked[0][5]}. "
            "Run `argus-enrol purge --expired --by ...` to remove what was derived from it."
        )
    latest = max(row[4] for row in rows)
    raise ConsentExpired(
        f"consent for {person_id} expired on {latest.isoformat()}. Record a new one before "
        "enrolling; the templates from the old one are purgeable now."
    )
