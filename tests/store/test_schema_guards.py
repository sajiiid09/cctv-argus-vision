"""Each constraint in 0002-0007, exercised until it fires.

A CHECK nobody has tried to violate is decoration: it can be wrong, or absent,
and the schema still looks careful. Every test here writes the row the schema is
supposed to refuse.

Raw SQL rather than the Store API on purpose -- the point is what the database
does when a future writer gets it wrong, not what today's writer happens to
send.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from conftest import insert_person

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 9, 18, 6, 0, tzinfo=UTC)


def _id() -> uuid.UUID:
    return uuid.uuid4()


async def _camera(db, camera_id: str = "canteen_door_01", role: str = "canteen_door") -> str:
    await db.execute(
        "insert into camera (camera_id, role, source_uri, space_id, door_id, is_virtual)"
        " values (%s, %s, %s, 'canteen', 'c1', true) on conflict (camera_id) do nothing",
        (camera_id, role, f"rtsp://localhost:8554/{camera_id}"),
    )
    return camera_id


async def _event(db, camera_id: str, ts: datetime, direction: str = "enter", **kw) -> uuid.UUID:
    event_id = _id()
    await db.execute(
        "insert into doorway_event (event_id, camera_id, door_id, ts_utc, direction,"
        " person_id, clip_ref, duplicate_of) values (%s,%s,'c1',%s,%s,%s,%s,%s)",
        (
            event_id,
            camera_id,
            ts,
            direction,
            kw.get("person_id"),
            kw.get("clip_ref"),
            kw.get("duplicate_of"),
        ),
    )
    return event_id


async def _clip(db, camera_id: str, rel_path: str = "clips/a.mp4") -> uuid.UUID:
    clip_id = _id()
    await db.execute(
        "insert into clip (clip_id, camera_id, rel_path, start_utc, end_utc, keyframe_utc,"
        " is_virtual) values (%s,%s,%s,%s,%s,%s,true)",
        (clip_id, camera_id, rel_path, NOW, NOW + timedelta(seconds=10), NOW),
    )
    return clip_id


async def _consent(db, person_id: str) -> uuid.UUID:
    consent_id = _id()
    await db.execute(
        "insert into consent_record (consent_id, person_id, purpose, granted_on, expires_on,"
        " recorded_by) values (%s,%s,'canteen enrolment','2026-09-01','2026-12-01','a human')",
        (consent_id, person_id),
    )
    return consent_id


async def _tap(db, source: str = "simulated", reader_seq: int | None = 1) -> uuid.UUID:
    tap_id = _id()
    await db.execute(
        "insert into gate_tap (tap_id, reader_id, badge_id, ts_utc, source, reader_seq)"
        " values (%s,'gate_reader_01','badge-1',%s,%s,%s)",
        (tap_id, NOW, source, reader_seq),
    )
    return tap_id


async def _run(db) -> uuid.UUID:
    run_id = _id()
    await db.execute(
        "insert into pairing_run (pairing_run_id, computed_at, logic_version,"
        " policy_fingerprint, policy_json, policy_description, window_from_utc,"
        " window_to_utc, lead_in_s, space_ids, event_count, gap_count)"
        " values (%s,%s,'pairing-1.0.0','abc','{}'::jsonb,'allowance 1 hour per local day',"
        "%s,%s,10800,array['canteen'],0,0)",
        (run_id, NOW, NOW - timedelta(days=1), NOW),
    )
    return run_id


# --- 0002: deduplication points backwards -----------------------------------


async def test_superseded_by_is_gone_and_duplicate_of_exists(store):
    rows = await store.db.fetch_all(
        "select column_name from information_schema.columns"
        " where table_name = 'doorway_event' order by column_name"
    )
    columns = {r[0] for r in rows}
    assert "duplicate_of" in columns
    assert "superseded_by" not in columns


async def test_duplicate_of_must_point_at_an_earlier_event(store):
    """ADR-0032. Pointing forwards keeps the later, worse crossing estimate."""
    cam = await _camera(store.db)
    earlier = await _event(store.db, cam, NOW)
    later_ts = NOW + timedelta(seconds=2)
    # the later row pointing back at the earlier one: allowed
    await _event(store.db, cam, later_ts, duplicate_of=earlier)
    # the reverse, which would invert "the earliest wins": refused
    newest = await _event(store.db, cam, NOW + timedelta(seconds=4))
    with pytest.raises(Exception, match="EARLIER"):
        await _event(store.db, cam, NOW - timedelta(seconds=1), duplicate_of=newest)


async def test_duplicate_of_cannot_be_self(store):
    cam = await _camera(store.db)
    event_id = _id()
    with pytest.raises(Exception, match="not itself"):
        await store.db.execute(
            "insert into doorway_event (event_id, camera_id, ts_utc, direction, duplicate_of)"
            " values (%s,%s,%s,'enter',%s)",
            (event_id, cam, NOW, event_id),
        )


async def test_gap_causes_accept_the_two_new_ones(store):
    cam = await _camera(store.db)
    for cause in ("aim_changed", "overload"):
        await store.open_gap(cam, NOW, cause)
    with pytest.raises(ValueError, match="invalid gap cause"):
        await store.open_gap(cam, NOW, "vibes")


async def test_unknown_person_is_null_not_a_made_up_id(store):
    """The fail-open outcome is representable; a typo'd id is not."""
    cam = await _camera(store.db)
    await _event(store.db, cam, NOW, person_id=None)
    with pytest.raises(Exception, match="doorway_event_person_fk"):
        await _event(store.db, cam, NOW, person_id="not-on-the-roster")


# --- 0003: clips are evidence ------------------------------------------------


async def test_clip_paths_must_be_relative(store):
    cam = await _camera(store.db)
    for bad in ("/etc/passwd", "clips/../../etc/passwd"):
        with pytest.raises(Exception, match="rel_path_is_relative"):
            await _clip(store.db, cam, bad)


async def test_only_clip_deleted_at_may_change(store):
    cam = await _camera(store.db)
    clip_id = await _clip(store.db, cam)
    await store.db.execute("update clip set deleted_at = %s where clip_id = %s", (NOW, clip_id))
    with pytest.raises(Exception, match=r"only clip\.deleted_at"):
        await store.db.execute(
            "update clip set rel_path = 'clips/elsewhere.mp4' where clip_id = %s", (clip_id,)
        )


async def test_a_referenced_clip_cannot_be_deleted(store):
    """RISKS.md §9: retention consults references, not just age."""
    cam = await _camera(store.db)
    clip_id = await _clip(store.db, cam, "clips/referenced.mp4")
    await _event(store.db, cam, NOW, clip_ref="clips/referenced.mp4")
    with pytest.raises(Exception, match="referenced by a doorway event"):
        await store.db.execute("delete from clip where clip_id = %s", (clip_id,))
    unreferenced = await _clip(store.db, cam, "clips/orphan.mp4")
    await store.db.execute("delete from clip where clip_id = %s", (unreferenced,))


async def test_clip_window_must_start_at_or_after_its_keyframe(store):
    cam = await _camera(store.db)
    with pytest.raises(Exception, match="clip_window_ordered"):
        await store.db.execute(
            "insert into clip (clip_id, camera_id, rel_path, start_utc, end_utc, keyframe_utc,"
            " is_virtual) values (%s,%s,'clips/b.mp4',%s,%s,%s,true)",
            (_id(), cam, NOW, NOW + timedelta(seconds=5), NOW + timedelta(seconds=1)),
        )


async def test_camera_uri_cannot_carry_credentials(store):
    with pytest.raises(Exception, match="has_no_credentials"):
        await store.db.execute(
            "insert into camera (camera_id, role, source_uri, is_virtual)"
            " values ('gate_door','gate','rtsp://admin:hunter2@10.0.0.5/main',false)"
        )


async def test_camera_reference_frame_names_who_accepted_it(store):
    cam = await _camera(store.db)
    with pytest.raises(Exception, match="accepted_by_named"):
        await store.db.execute(
            "insert into camera_reference_frame (reference_id, camera_id, captured_at, width,"
            " height, rel_path, sha256, accepted_by)"
            " values (%s,%s,%s,640,360,'ref/a.png','deadbeef','   ')",
            (_id(), cam, NOW),
        )


# --- 0004: identity and consent ---------------------------------------------


async def test_a_template_without_consent_is_unrepresentable(store):
    person_id = await insert_person(store.db, "p-consent")
    with pytest.raises(Exception, match="consent_id"):
        await store.db.execute(
            "insert into face_template (template_id, person_id, model_ref, embedding, dim,"
            " source, enrolled_by) values (%s,%s,'arcface@abc',%s,512,'images','a human')",
            (_id(), person_id, b"\x00" * 8),
        )
    consent_id = await _consent(store.db, person_id)
    await store.db.execute(
        "insert into face_template (template_id, person_id, consent_id, model_ref, embedding,"
        " dim, source, enrolled_by) values (%s,%s,%s,'arcface@abc',%s,512,'images','a human')",
        (_id(), person_id, consent_id, b"\x00" * 8),
    )


async def test_enrolment_action_is_append_only(store):
    action_id = _id()
    await store.db.execute(
        "insert into enrolment_action (action_id, actor, action) values (%s,'a human','enrol')",
        (action_id,),
    )
    with pytest.raises(Exception, match="append-only"):
        await store.db.execute(
            "update enrolment_action set actor = 'someone else' where action_id = %s", (action_id,)
        )


async def test_a_badge_has_one_holder_at_a_time(store):
    first = await insert_person(store.db, "p-badge-1")
    second = await insert_person(store.db, "p-badge-2")
    await store.db.execute(
        "insert into badge_assignment (assignment_id, badge_id, person_id, assigned_from,"
        " assigned_by) values (%s,'badge-9',%s,%s,'a human')",
        (_id(), first, NOW - timedelta(days=10)),
    )
    # The overlap trigger fires before the partial unique index gets a chance;
    # both exist, and either refusal is the right answer.
    with pytest.raises(Exception, match="already has a holder"):
        await store.db.execute(
            "insert into badge_assignment (assignment_id, badge_id, person_id, assigned_from,"
            " assigned_by) values (%s,'badge-9',%s,%s,'a human')",
            (_id(), second, NOW),
        )
    # close the first, then an overlapping historical window is still refused
    await store.db.execute(
        "update badge_assignment set assigned_to = %s where badge_id = 'badge-9'",
        (NOW - timedelta(days=1),),
    )
    with pytest.raises(Exception, match="already has a holder"):
        await store.db.execute(
            "insert into badge_assignment (assignment_id, badge_id, person_id, assigned_from,"
            " assigned_to, assigned_by) values (%s,'badge-9',%s,%s,%s,'a human')",
            (_id(), second, NOW - timedelta(days=5), NOW - timedelta(days=2)),
        )


# --- 0005: the gate's four-way outcome --------------------------------------


async def test_a_replayed_tap_is_never_recorded_as_no_face(store):
    cam = await _camera(store.db, "gate_door", "gate")
    tap_id = await _tap(store.db, source="zkt_replay", reader_seq=7)
    with pytest.raises(Exception, match="replay_not_attempted"):
        await store.db.execute(
            "insert into gate_event (gate_event_id, tap_id, camera_id, face_verified, source)"
            " values (%s,%s,%s,'no_face','zkt_replay')",
            (_id(), tap_id, cam),
        )
    await store.db.execute(
        "insert into gate_event (gate_event_id, tap_id, camera_id, face_verified, source)"
        " values (%s,%s,%s,'not_attempted','zkt_replay')",
        (_id(), tap_id, cam),
    )


async def test_a_verdict_carries_a_score_and_no_face_does_not(store):
    cam = await _camera(store.db, "gate_door", "gate")
    tap_id = await _tap(store.db, reader_seq=11)
    with pytest.raises(Exception, match="score_iff_compared"):
        await store.db.execute(
            "insert into gate_event (gate_event_id, tap_id, camera_id, face_verified, source)"
            " values (%s,%s,%s,'false','simulated')",
            (_id(), tap_id, cam),
        )
    with pytest.raises(Exception, match="score_iff_compared"):
        await store.db.execute(
            "insert into gate_event (gate_event_id, tap_id, camera_id, face_verified,"
            " match_score, source) values (%s,%s,%s,'no_face',0.4,'simulated')",
            (_id(), tap_id, cam),
        )


async def test_a_tap_is_recorded_once_per_reader_sequence(store):
    await _tap(store.db, reader_seq=42)
    with pytest.raises(Exception, match="reader_seq_once"):
        await _tap(store.db, reader_seq=42)


async def test_gate_review_happens_once_and_touches_only_review_columns(store):
    cam = await _camera(store.db, "gate_door", "gate")
    tap_id = await _tap(store.db, reader_seq=3)
    gate_event_id = _id()
    await store.db.execute(
        "insert into gate_event (gate_event_id, tap_id, camera_id, face_verified, match_score,"
        " source) values (%s,%s,%s,'false',0.2,'simulated')",
        (gate_event_id, tap_id, cam),
    )
    with pytest.raises(Exception, match="only the review columns"):
        await store.db.execute(
            "update gate_event set face_verified = 'true' where gate_event_id = %s",
            (gate_event_id,),
        )
    await store.db.execute(
        "update gate_event set review_state = 'dismissed', reviewed_by = 'a human',"
        " reviewed_at = now() where gate_event_id = %s",
        (gate_event_id,),
    )
    with pytest.raises(Exception, match="already reviewed"):
        await store.db.execute(
            "update gate_event set review_state = 'escalated', reviewed_by = 'a human'"
            " where gate_event_id = %s",
            (gate_event_id,),
        )


# --- 0006: the derived rows a wage would be read from ------------------------


async def test_a_flagged_day_charges_nothing(store):
    """ADR-0023, enforced where the number is stored."""
    person_id = await insert_person(store.db, "p-day")
    run_id = await _run(store.db)
    with pytest.raises(Exception, match="flagged_charges_nothing"):
        await store.db.execute(
            "insert into dwell_day (day_id, pairing_run_id, person_id, space_id, local_day,"
            " total_dwell_s, allowance_s, overage_s, day_state)"
            " values (%s,%s,%s,'canteen','2026-09-18',5400,3600,1800,'flagged')",
            (_id(), run_id, person_id),
        )
    await store.db.execute(
        "insert into dwell_day (day_id, pairing_run_id, person_id, space_id, local_day,"
        " total_dwell_s, allowance_s, overage_s, day_state)"
        " values (%s,%s,%s,'canteen','2026-09-18',5400,3600,0,'flagged')",
        (_id(), run_id, person_id),
    )


async def test_overage_cannot_exceed_the_measured_dwell(store):
    person_id = await insert_person(store.db, "p-bounds")
    run_id = await _run(store.db)
    with pytest.raises(Exception, match="overage_bounded"):
        await store.db.execute(
            "insert into dwell_day (day_id, pairing_run_id, person_id, space_id, local_day,"
            " total_dwell_s, allowance_s, overage_s, day_state)"
            " values (%s,%s,%s,'canteen','2026-09-18',600,3600,900,'clean')",
            (_id(), run_id, person_id),
        )


async def test_a_resolved_interval_has_both_ends(store):
    person_id = await insert_person(store.db, "p-interval")
    run_id = await _run(store.db)
    with pytest.raises(Exception, match="resolved_is_complete"):
        await store.db.execute(
            "insert into dwell_interval (interval_id, pairing_run_id, person_id, space_id,"
            " local_day, start_utc, state) values (%s,%s,%s,'canteen','2026-09-18',%s,'resolved')",
            (_id(), run_id, person_id, NOW),
        )


async def test_an_interval_cannot_end_before_it_starts(store):
    person_id = await insert_person(store.db, "p-order")
    run_id = await _run(store.db)
    with pytest.raises(Exception, match="dwell_interval_ordered"):
        await store.db.execute(
            "insert into dwell_interval (interval_id, pairing_run_id, person_id, space_id,"
            " local_day, start_utc, end_utc, state)"
            " values (%s,%s,%s,'canteen','2026-09-18',%s,%s,'ambiguous')",
            (_id(), run_id, person_id, NOW, NOW - timedelta(minutes=5)),
        )


async def test_a_run_cannot_claim_it_is_not_shadow(store):
    with pytest.raises(Exception, match="is_shadow"):
        await store.db.execute(
            "insert into pairing_run (pairing_run_id, computed_at, logic_version,"
            " policy_fingerprint, policy_json, policy_description, window_from_utc,"
            " window_to_utc, lead_in_s, space_ids, event_count, gap_count, shadow_mode)"
            " values (%s,%s,'pairing-1.0.0','abc','{}'::jsonb,'d',%s,%s,0,array['canteen'],"
            "0,0,false)",
            (_id(), NOW, NOW - timedelta(days=1), NOW),
        )


async def test_derived_rows_are_never_edited(store):
    person_id = await insert_person(store.db, "p-append")
    run_id = await _run(store.db)
    day_id = _id()
    await store.db.execute(
        "insert into dwell_day (day_id, pairing_run_id, person_id, space_id, local_day,"
        " total_dwell_s, allowance_s, overage_s, day_state)"
        " values (%s,%s,%s,'canteen','2026-09-18',3600,3600,0,'clean')",
        (day_id, run_id, person_id),
    )
    with pytest.raises(Exception, match="append-only"):
        await store.db.execute("update dwell_day set overage_s = 60 where day_id = %s", (day_id,))
    with pytest.raises(Exception, match="append-only"):
        await store.db.execute("delete from dwell_day where day_id = %s", (day_id,))


async def test_export_is_refused_by_the_database(store):
    """RISKS.md §11 and AGENTS.md §2.1: this is a sign-off, not a column write."""
    person_id = await insert_person(store.db, "p-export")
    run_id = await _run(store.db)
    line_id = _id()
    await store.db.execute(
        "insert into payroll_line (line_id, pairing_run_id, person_id, space_id, local_day,"
        " overage_s, day_state) values (%s,%s,%s,'canteen','2026-09-18',0,'flagged')",
        (line_id, run_id, person_id),
    )
    with pytest.raises(Exception, match="payroll export is off"):
        await store.db.execute(
            "insert into payroll_line (line_id, pairing_run_id, person_id, space_id, local_day,"
            " overage_s, day_state, exported_at)"
            " values (%s,%s,%s,'canteen','2026-09-19',0,'clean',now())",
            (_id(), run_id, person_id),
        )
    with pytest.raises(Exception, match=r"payroll export is off|append-only"):
        await store.db.execute(
            "update payroll_line set exported_at = now() where line_id = %s", (line_id,)
        )
    with pytest.raises(Exception, match="append-only"):
        await store.db.execute("delete from payroll_line where line_id = %s", (line_id,))


# --- 0007: the anonymous pipelines and the access log -----------------------


async def test_a_review_names_the_human_who_made_it(store):
    cam = await _camera(store.db, "floor_view", "floor")
    candidate_id = _id()
    await store.db.execute(
        "insert into violence_candidate (candidate_id, camera_id, at, trigger_score)"
        " values (%s,%s,%s,0.8)",
        (candidate_id, cam, NOW),
    )
    with pytest.raises(Exception, match="names the human"):
        await store.db.execute(
            "update violence_candidate set review_state = 'dismissed' where candidate_id = %s",
            (candidate_id,),
        )
    await store.db.execute(
        "update violence_candidate set review_state = 'dismissed', reviewed_by = 'a human',"
        " reviewed_at = now() where candidate_id = %s",
        (candidate_id,),
    )
    with pytest.raises(Exception, match="already reviewed"):
        await store.db.execute(
            "update violence_candidate set review_state = 'escalated', reviewed_by = 'a human'"
            " where candidate_id = %s",
            (candidate_id,),
        )


async def test_violence_candidate_has_no_classifier_score_column(store):
    """ADR-0012 is trigger-only; a classifier needs a reopened ADR, not a column."""
    rows = await store.db.fetch_all(
        "select column_name from information_schema.columns where table_name = 'violence_candidate'"
    )
    assert "classifier_score" not in {r[0] for r in rows}


async def test_occupancy_and_violence_carry_no_person_column(store):
    rows = await store.db.fetch_all(
        "select table_name, column_name from information_schema.columns"
        " where table_name in ('occupancy_sample','violence_candidate','violence_review_action')"
    )
    assert not [r for r in rows if r[1] in ("person_id", "employee_ref")]


async def test_clip_access_log_is_append_only_and_logs_denials(store):
    cam = await _camera(store.db)
    clip_id = await _clip(store.db, cam, "clips/watched.mp4")
    access_id = _id()
    await store.db.execute(
        "insert into clip_access_log (access_id, actor, role, clip_id, context, outcome)"
        " values (%s,'a human','reviewer',%s,'canteen_audit','served')",
        (access_id, clip_id),
    )
    await store.db.execute(
        "insert into clip_access_log (access_id, actor, role, clip_id, context, outcome)"
        " values (%s,'a human','viewer',%s,'canteen_audit','denied')",
        (_id(), clip_id),
    )
    with pytest.raises(Exception, match="append-only"):
        await store.db.execute(
            "update clip_access_log set outcome = 'served' where access_id = %s", (access_id,)
        )
