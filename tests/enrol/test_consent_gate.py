"""Consent, and the three distinct ways there may not be any.

They need three different responses: nobody asked, the permission ran out, the
person withdrew it. Collapsing them into "no consent" loses the only one that
requires deleting something.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from argus.enrol.consent import ConsentExpired, ConsentMissing, ConsentRevoked, active_consent

from conftest import insert_person

pytestmark = pytest.mark.postgres

TODAY = date(2026, 9, 18)


async def _consent(store, person_id: str, expires: date, revoked: datetime | None = None):
    consent_id = uuid4()
    await store.db.execute(
        "insert into consent_record (consent_id, person_id, purpose, granted_on, expires_on,"
        " recorded_by, revoked_at) values (%s,%s,'canteen','2026-09-01',%s,'a human',%s)",
        (consent_id, person_id, expires, revoked),
    )
    return consent_id


async def test_live_consent_is_returned(store) -> None:
    person = await insert_person(store.db, "p-live")
    consent_id = await _consent(store, person, date(2026, 12, 31))
    consent = await active_consent(store.db, person, TODAY)
    assert consent.consent_id == consent_id


async def test_no_consent_names_the_command_that_records_it(store) -> None:
    person = await insert_person(store.db, "p-none")
    with pytest.raises(ConsentMissing, match="argus-enrol consent record"):
        await active_consent(store.db, person, TODAY)


async def test_expired_consent_is_its_own_answer(store) -> None:
    person = await insert_person(store.db, "p-expired")
    await _consent(store, person, date(2026, 9, 1))
    with pytest.raises(ConsentExpired, match="expired on 2026-09-01"):
        await active_consent(store.db, person, TODAY)


async def test_withdrawn_consent_says_to_purge(store) -> None:
    """The one case that requires deleting what was already derived."""
    person = await insert_person(store.db, "p-revoked")
    await _consent(store, person, date(2026, 12, 31), revoked=datetime(2026, 9, 10, tzinfo=UTC))
    with pytest.raises(ConsentRevoked, match="purge --expired"):
        await active_consent(store.db, person, TODAY)


async def test_a_template_without_consent_is_refused_by_the_schema_too(store) -> None:
    """The Python check gives the message; the FK gives the guarantee."""
    person = await insert_person(store.db, "p-schema")
    with pytest.raises(Exception, match="consent_id"):
        await store.db.execute(
            "insert into face_template (template_id, person_id, model_ref, embedding, dim,"
            " source, enrolled_by) values (%s,%s,'m@1',%s,4,'images','a human')",
            (uuid4(), person, b"\x00" * 16),
        )
