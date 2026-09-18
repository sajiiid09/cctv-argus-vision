"""The clip-access log, against a real database.

RISKS.md §5 names casual clip browsing as the misuse most likely to happen and
least likely to be reported. §10 says this log is built regardless of how good
the authentication turns out to be -- so it is the control that actually
addresses the threat, and it gets the postgres tier.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from argus.ui.audit import log_clip_access
from argus.ui.queries import clip_is_payroll_evidence, clip_row

from helpers import insert_person

pytestmark = pytest.mark.postgres

T0 = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)


async def _clip(store, rel_path: str = "canteen_door_01/a.mp4"):
    await store.db.execute(
        "insert into camera (camera_id, role, source_uri, space_id, door_id, is_virtual)"
        " values ('canteen_door_01','canteen_door','rtsp://x','canteen','c1',true)"
        " on conflict (camera_id) do nothing"
    )
    clip_id = uuid4()
    await store.db.execute(
        "insert into clip (clip_id, camera_id, rel_path, start_utc, end_utc, keyframe_utc,"
        " is_virtual) values (%s,'canteen_door_01',%s,%s,%s,%s,true)",
        (clip_id, rel_path, T0, T0 + timedelta(seconds=10), T0),
    )
    return clip_id


async def test_a_viewing_is_logged_once_per_minute(store) -> None:
    """A <video> element issues many Range requests for one viewing."""
    clip_id = await _clip(store)
    first = await log_clip_access(
        store.db,
        actor="a human",
        role="reviewer",
        clip_id=clip_id,
        context="review",
        outcome="served",
    )
    second = await log_clip_access(
        store.db,
        actor="a human",
        role="reviewer",
        clip_id=clip_id,
        context="review",
        outcome="served",
    )
    assert first is True and second is False
    rows = await store.db.fetch_all("select count(*) from clip_access_log")
    assert rows == [(1,)]


async def test_a_second_actor_is_logged_separately(store) -> None:
    clip_id = await _clip(store)
    await log_clip_access(
        store.db,
        actor="first",
        role="reviewer",
        clip_id=clip_id,
        context="review",
        outcome="served",
    )
    await log_clip_access(
        store.db,
        actor="second",
        role="reviewer",
        clip_id=clip_id,
        context="review",
        outcome="served",
    )
    rows = await store.db.fetch_all("select actor from clip_access_log order by actor")
    assert rows == [("first",), ("second",)]


async def test_a_refusal_is_logged_and_kept_apart_from_a_viewing(store) -> None:
    """The refused attempt is the interesting signal."""
    clip_id = await _clip(store)
    await log_clip_access(
        store.db,
        actor="a human",
        role="payroll",
        clip_id=clip_id,
        context="canteen_audit",
        outcome="denied",
        reason="not referenced by an interval",
    )
    await log_clip_access(
        store.db,
        actor="a human",
        role="payroll",
        clip_id=clip_id,
        context="canteen_audit",
        outcome="served",
    )
    rows = await store.db.fetch_all("select outcome, reason from clip_access_log order by outcome")
    assert rows == [
        ("denied", "not referenced by an interval"),
        ("served", None),
    ]


async def test_the_log_cannot_be_edited(store) -> None:
    clip_id = await _clip(store)
    await log_clip_access(
        store.db,
        actor="a human",
        role="admin",
        clip_id=clip_id,
        context="review",
        outcome="served",
    )
    with pytest.raises(Exception, match="append-only"):
        await store.db.execute("update clip_access_log set actor = 'somebody else'")


async def test_payroll_evidence_means_referenced_by_an_interval(store) -> None:
    """ADR-0028's scope, in SQL: the payroll tier reaches a clip through the
    line that references it."""
    clip_id = await _clip(store)
    loose_clip = await _clip(store, "canteen_door_01/unreferenced.mp4")
    person = await insert_person(store.db, "p-evidence")

    assert await clip_is_payroll_evidence(store.db, clip_id) is False

    enter = await store.insert_doorway_event(
        "canteen_door_01",
        "c1",
        T0,
        "enter",
        person_id=person,
        clip_ref="canteen_door_01/a.mp4",
    )
    exit_ = await store.insert_doorway_event(
        "canteen_door_01", "c1", T0 + timedelta(minutes=20), "exit", person_id=person
    )
    run_id = uuid4()
    await store.db.execute(
        "insert into pairing_run (pairing_run_id, computed_at, logic_version,"
        " policy_fingerprint, policy_json, policy_description, window_from_utc, window_to_utc,"
        " lead_in_s, space_ids, event_count, gap_count)"
        " values (%s,%s,'pairing-1.0.0','abc','{}'::jsonb,'allowance 1 hour per local day',"
        " %s,%s,0,array['canteen'],2,0)",
        (run_id, T0, T0, T0 + timedelta(days=1)),
    )
    await store.db.execute(
        "insert into dwell_interval (interval_id, pairing_run_id, person_id, space_id,"
        " local_day, enter_event_id, exit_event_id, start_utc, end_utc, duration_s, state)"
        " values (%s,%s,%s,'canteen','2026-09-18',%s,%s,%s,%s,1200,'resolved')",
        (uuid4(), run_id, person, enter.event_id, exit_.event_id, T0, T0 + timedelta(minutes=20)),
    )

    assert await clip_is_payroll_evidence(store.db, clip_id) is True
    assert await clip_is_payroll_evidence(store.db, loose_clip) is False


async def test_the_clip_row_carries_what_the_player_needs(store) -> None:
    clip_id = await _clip(store)
    row = await clip_row(store.db, clip_id)
    assert row is not None
    assert row["rel_path"] == "canteen_door_01/a.mp4"
    assert row["keyframe_utc"] <= row["start_utc"]
    assert row["is_virtual"] is True, "a clip from the rig must be identifiable as such"
