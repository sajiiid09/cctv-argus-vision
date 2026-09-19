"""Every SQL statement the console runs. The only module with any.

Keeping them here is what lets the route tests fake the data layer and stay in
the fast test tier, and it is also where "what does the console read?" has one
answer -- which matters, because the answer must stay "stored rows".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from argus.store.db import Database


@dataclass(frozen=True, slots=True)
class RunHeader:
    """What the canteen page says about itself, from the run that made it."""

    pairing_run_id: UUID
    computed_at: datetime
    logic_version: str
    policy_fingerprint: str
    policy_description: str
    shadow_mode: bool
    window_from_utc: datetime
    window_to_utc: datetime


async def latest_run(db: Database, day: date | None = None) -> RunHeader | None:
    if day is None:
        row = await db.fetch_one(
            "select pairing_run_id, computed_at, logic_version, policy_fingerprint,"
            " policy_description, shadow_mode, window_from_utc, window_to_utc"
            " from pairing_run order by computed_at desc limit 1"
        )
    else:
        row = await db.fetch_one(
            "select r.pairing_run_id, r.computed_at, r.logic_version, r.policy_fingerprint,"
            " r.policy_description, r.shadow_mode, r.window_from_utc, r.window_to_utc"
            " from pairing_run r join dwell_day d on d.pairing_run_id = r.pairing_run_id"
            " where d.local_day = %s order by r.computed_at desc limit 1",
            (day,),
        )
    if row is None:
        return None
    return RunHeader(
        pairing_run_id=row[0],
        computed_at=row[1],
        logic_version=row[2],
        policy_fingerprint=row[3],
        policy_description=row[4],
        shadow_mode=row[5],
        window_from_utc=row[6],
        window_to_utc=row[7],
    )


async def days_for_run(db: Database, run_id: UUID) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "select person_id, space_id, local_day, total_dwell_s, allowance_s, overage_s,"
        " day_state, flags from dwell_day where pairing_run_id = %s"
        " order by local_day desc, person_id",
        (run_id,),
    )
    return [
        {
            "person_id": row[0],
            "space_id": row[1],
            "local_day": row[2],
            "total_dwell_s": row[3],
            "allowance_s": row[4],
            "overage_s": row[5],
            "day_state": row[6],
            "flags": row[7],
        }
        for row in rows
    ]


async def intervals_for_person_day(
    db: Database, run_id: UUID, person_id: str, day: date
) -> list[dict[str, Any]]:
    """The audit view's rows: an interval, its flags, and the clips at both ends.

    The clips come from a join on the events, not from a column on the interval:
    a doorway event is append-only, so its clip_ref is the truth and copying it
    onto the derived row would be a second copy that could disagree.
    """
    rows = await db.fetch_all(
        "select i.interval_id, i.state, i.start_utc, i.end_utc, i.duration_s, i.flags,"
        " enter.clip_ref, exit_.clip_ref, enter_clip.clip_id, exit_clip.clip_id,"
        " enter.ts_utc, exit_.ts_utc, enter_clip.keyframe_utc, exit_clip.keyframe_utc"
        " from dwell_interval i"
        " left join doorway_event enter on enter.event_id = i.enter_event_id"
        " left join doorway_event exit_ on exit_.event_id = i.exit_event_id"
        " left join clip enter_clip on enter_clip.rel_path = enter.clip_ref"
        " left join clip exit_clip on exit_clip.rel_path = exit_.clip_ref"
        " where i.pairing_run_id = %s and i.person_id = %s and i.local_day = %s"
        " order by i.start_utc nulls last",
        (run_id, person_id, day),
    )
    return [
        {
            "interval_id": row[0],
            "state": row[1],
            "start_utc": row[2],
            "end_utc": row[3],
            "duration_s": row[4],
            "flags": row[5],
            "enter_clip_ref": row[6],
            "exit_clip_ref": row[7],
            "enter_clip_id": row[8],
            "exit_clip_id": row[9],
            "enter_ts": row[10],
            "exit_ts": row[11],
            "enter_keyframe": row[12],
            "exit_keyframe": row[13],
        }
        for row in rows
    ]


async def gate_events(db: Database, limit: int = 100) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "select e.gate_event_id, t.badge_id, e.person_id, e.face_verified, e.match_score,"
        " e.source, e.decided_at, e.review_state, e.reviewed_by, t.ts_utc"
        " from gate_event e join gate_tap t on t.tap_id = e.tap_id"
        " order by e.decided_at desc limit %s",
        (limit,),
    )
    return [
        {
            "gate_event_id": row[0],
            "badge_id": row[1],
            "person_id": row[2],
            "face_verified": row[3],
            "match_score": row[4],
            "source": row[5],
            "decided_at": row[6],
            "review_state": row[7],
            "reviewed_by": row[8],
            "tapped_at": row[9],
        }
        for row in rows
    ]


async def gate_outcome_counts(db: Database) -> dict[str, int]:
    rows = await db.fetch_all(
        "select face_verified, count(*) from gate_event group by face_verified"
    )
    counts = {row[0]: row[1] for row in rows}
    # Every outcome is shown, including the zeroes: an outcome missing from a
    # page reads as "this does not happen" rather than "this has not happened".
    for outcome in ("true", "false", "no_face", "not_attempted"):
        counts.setdefault(outcome, 0)
    return counts


async def reader_gaps(db: Database, limit: int = 20) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "select reader_id, from_utc, to_utc, cause from reader_gap order by from_utc desc limit %s",
        (limit,),
    )
    return [
        {"reader_id": row[0], "from_utc": row[1], "to_utc": row[2], "cause": row[3]} for row in rows
    ]


async def violence_queue(db: Database, state: str = "pending") -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "select candidate_id, camera_id, at, trigger_score, clip_id, review_state,"
        " reviewed_by, reviewed_at, review_reason from violence_candidate"
        " where review_state = %s order by at desc",
        (state,),
    )
    return [
        {
            "candidate_id": row[0],
            "camera_id": row[1],
            "at": row[2],
            "trigger_score": row[3],
            "clip_id": row[4],
            "review_state": row[5],
            "reviewed_by": row[6],
            "reviewed_at": row[7],
            "review_reason": row[8],
        }
        for row in rows
    ]


async def review_violence(
    db: Database, candidate_id: UUID, *, state: str, actor: str, reason: str | None
) -> bool:
    """Idempotent: only a pending candidate moves. Returns whether it did.

    Zero rows means somebody else reviewed it first, which is not an error --
    the reviewer did nothing wrong -- and must not overwrite an audit fact.
    """
    rows = await db.fetch_all(
        "update violence_candidate set review_state = %s, reviewed_by = %s,"
        " reviewed_at = now(), review_reason = %s"
        " where candidate_id = %s and review_state = 'pending'"
        " returning candidate_id",
        (state, actor, reason, candidate_id),
    )
    return bool(rows)


async def review_gate_event(
    db: Database, gate_event_id: UUID, *, state: str, actor: str, reason: str | None
) -> bool:
    rows = await db.fetch_all(
        "update gate_event set review_state = %s, reviewed_by = %s, reviewed_at = now(),"
        " review_reason = %s where gate_event_id = %s and review_state = 'pending'"
        " returning gate_event_id",
        (state, actor, reason, gate_event_id),
    )
    return bool(rows)


async def clip_row(db: Database, clip_id: UUID) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "select clip_id, camera_id, rel_path, start_utc, end_utc, keyframe_utc, is_virtual,"
        " deleted_at from clip where clip_id = %s",
        (clip_id,),
    )
    if row is None:
        return None
    return {
        "clip_id": row[0],
        "camera_id": row[1],
        "rel_path": row[2],
        "start_utc": row[3],
        "end_utc": row[4],
        "keyframe_utc": row[5],
        "is_virtual": row[6],
        "deleted_at": row[7],
    }


async def clip_is_payroll_evidence(db: Database, clip_id: UUID) -> bool:
    """Is this clip referenced by an interval in a pairing run?

    The scope of the payroll tier's clip access (ADR-0028): a payroll actor may
    open a clip *through the line that references it*, and no other. The check
    is on the clip's relationship to the interval, not on the role alone, which
    is what lets RISKS.md §10 and SOUL.md's under-a-minute audit path both hold.
    """
    row = await db.fetch_one(
        "select 1 from clip c"
        " join doorway_event e on e.clip_ref = c.rel_path"
        " join dwell_interval i on i.enter_event_id = e.event_id"
        "   or i.exit_event_id = e.event_id"
        " where c.clip_id = %s limit 1",
        (clip_id,),
    )
    return row is not None


async def violence_counts(db: Database) -> dict[str, int]:
    rows = await db.fetch_all(
        "select review_state, count(*) from violence_candidate group by review_state"
    )
    counts = {row[0]: row[1] for row in rows}
    for state in ("pending", "dismissed", "escalated"):
        counts.setdefault(state, 0)
    return counts


async def monitoring(db: Database, day: date) -> dict[str, Any]:
    """RISKS.md §8, rendered from stored rows.

    Including review latency, which is on the page because near-zero latency
    means rubber-stamping rather than diligence.
    """
    unknown = await db.fetch_all(
        "select door_id, count(*) filter (where person_id is null), count(*)"
        " from doorway_event group by door_id order by door_id"
    )
    gaps = await db.fetch_all(
        "select camera_id, cause, round(sum(extract(epoch from"
        " (coalesce(to_utc, now()) - from_utc)) / 60.0)::numeric, 1)"
        " from stream_gap group by camera_id, cause order by camera_id"
    )
    flagged = await db.fetch_one(
        "select count(*) filter (where day_state = 'flagged'), count(*),"
        " coalesce(sum(overage_s), 0) from dwell_day where local_day = %s",
        (day,),
    )
    access = await db.fetch_all(
        "select actor, outcome, count(*) from clip_access_log"
        " group by actor, outcome order by actor"
    )
    latency = await db.fetch_all(
        "select review_state, count(*),"
        " round(avg(extract(epoch from (reviewed_at - created_at)))::numeric, 1)"
        " from violence_candidate where reviewed_at is not null"
        " group by review_state order by review_state"
    )
    return {
        "unknown_by_door": [
            {"door_id": row[0], "unknown": row[1], "total": row[2]} for row in unknown
        ],
        "gap_minutes": [{"camera_id": row[0], "cause": row[1], "minutes": row[2]} for row in gaps],
        "flagged": {
            "flagged": (flagged or (0, 0, 0))[0],
            "days": (flagged or (0, 0, 0))[1],
            "overage_s": (flagged or (0, 0, 0))[2],
        },
        "clip_access": [{"actor": row[0], "outcome": row[1], "count": row[2]} for row in access],
        "review_latency": [{"state": row[0], "count": row[1], "mean_s": row[2]} for row in latency],
    }
