"""Writing a run: one transaction, and nothing ever edited.

`Database.connect` is autocommit, so the transaction here is explicit. That
matters: reusing the ordinary execute path would commit each statement on its
own, and a crash mid-run would leave a half-written run that looks complete.

The NOTIFY goes last and **inside** the commit. Postgres queues notifications
until commit, so a subscriber can never see a run id that does not exist yet,
and a rollback discards the notification with the rows. "NOTIFY after commit"
and "NOTIFY last inside the commit" look identical and only one of them is
atomic.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID, uuid4

from argus.pairing.load import Evidence
from argus.payroll import PairingPolicy, PairingResult
from argus.store.db import Database
from argus.store.store import EVENTS_CHANNEL

log = logging.getLogger(__name__)


async def write_run(
    db: Database,
    result: PairingResult,
    evidence: Evidence,
    policy: PairingPolicy,
    *,
    code_version: str,
    config_hash: str | None = None,
    actor: str | None = None,
    reason: str | None = None,
) -> UUID:
    run_id = uuid4()
    window = evidence.window
    async with db.conn.transaction(), db.conn.cursor() as cur:
        await cur.execute(
            "insert into pairing_run (pairing_run_id, computed_at, logic_version,"
            " policy_fingerprint, policy_json, policy_description, window_from_utc,"
            " window_to_utc, lead_in_s, space_ids, event_count, gap_count, code_version,"
            " config_hash, actor, reason)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                run_id,
                result.computed_at,
                result.logic_version,
                result.policy_fingerprint,
                json.dumps(policy.as_json()),
                # The human sentence, stored rather than recomputed: the
                # console may not import argus.payroll (ADR-0015), and a page
                # must describe the run that produced its numbers even if the
                # config changed afterwards.
                policy.describe(),
                window.from_utc,
                window.to_utc,
                window.lead_in_s,
                list(window.space_ids),
                len(evidence.events),
                len(evidence.gaps),
                code_version,
                config_hash,
                actor,
                reason,
            ),
        )
        for interval in result.intervals:
            await cur.execute(
                "insert into dwell_interval (interval_id, pairing_run_id, person_id,"
                " space_id, local_day, enter_event_id, exit_event_id, start_utc, end_utc,"
                " duration_s, state, flags)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                # The interval id is a uuid5 of (logic version, policy, person,
                # space, day, both event ids), so re-running after a network
                # blip is a no-op rather than a duplicate row.
                " on conflict (interval_id) do nothing",
                (
                    interval.interval_id,
                    run_id,
                    interval.person_id,
                    interval.space_id,
                    interval.local_day,
                    interval.enter_event_id,
                    interval.exit_event_id,
                    interval.start_utc,
                    interval.end_utc,
                    interval.duration_s,
                    interval.state.value,
                    [flag.value for flag in interval.flags],
                ),
            )
        for day in result.days:
            await cur.execute(
                "insert into dwell_day (day_id, pairing_run_id, person_id, space_id,"
                " local_day, total_dwell_s, allowance_s, overage_s, day_state, flags,"
                " interval_ids)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (pairing_run_id, person_id, space_id, local_day) do nothing",
                (
                    uuid4(),
                    run_id,
                    day.person_id,
                    day.space_id,
                    day.local_day,
                    day.total_dwell_s,
                    day.allowance_s,
                    day.overage_s,
                    day.day_state,
                    [flag.value for flag in day.flags],
                    list(day.interval_ids),
                ),
            )
            # A line for EVERY (person, day) in the run, flagged days
            # included, with zero overage. A flagged day that produces no row
            # is indistinguishable from a person who did not eat, and
            # fail-open is only visible if it is on the page.
            await cur.execute(
                "insert into payroll_line (line_id, pairing_run_id, person_id, space_id,"
                " local_day, overage_s, day_state)"
                " values (%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (pairing_run_id, person_id, space_id, local_day) do nothing",
                (
                    uuid4(),
                    run_id,
                    day.person_id,
                    day.space_id,
                    day.local_day,
                    day.overage_s,
                    day.day_state,
                ),
            )
        for unattributable in result.unattributable:
            await cur.execute(
                "insert into pairing_unattributable (pairing_run_id, event_id, flag)"
                " values (%s,%s,%s) on conflict do nothing",
                (run_id, unattributable.event_id, unattributable.flag.value),
            )
        for event_id in result.open_enters:
            await cur.execute(
                "insert into pairing_open_enter (pairing_run_id, event_id)"
                " values (%s,%s) on conflict do nothing",
                (run_id, event_id),
            )
        await cur.execute(
            "select pg_notify(%s, %s)",
            (
                EVENTS_CHANNEL,
                json.dumps(
                    {
                        "type": "pairing_run",
                        "pairing_run_id": str(run_id),
                        "logic_version": result.logic_version,
                        "policy_fingerprint": result.policy_fingerprint,
                        "intervals": len(result.intervals),
                        "days": len(result.days),
                    }
                ),
            ),
        )
    log.info(
        "run %s: %d interval(s), %d day(s), %d unattributable, %d still inside",
        run_id,
        len(result.intervals),
        len(result.days),
        len(result.unattributable),
        len(result.open_enters),
    )
    return run_id


def summarise(result: PairingResult) -> list[str]:
    """What --dry-run prints, and what the run log says afterwards."""
    by_state: dict[str, int] = {}
    for interval in result.intervals:
        by_state[interval.state.value] = by_state.get(interval.state.value, 0) + 1
    clean = sum(1 for day in result.days if day.day_state == "clean")
    flagged = len(result.days) - clean
    charged = sum(day.overage_s for day in result.days)
    lines = [
        f"logic {result.logic_version} policy {result.policy_fingerprint}",
        "intervals: " + (", ".join(f"{k}={v}" for k, v in sorted(by_state.items())) or "none"),
        f"days: clean={clean} flagged={flagged}",
        f"overage total: {charged}s across {len(result.days)} day(s)",
        f"unattributable events: {len(result.unattributable)}",
        f"still inside at computation time: {len(result.open_enters)}",
        "shadow mode: nothing is written to payroll and no export exists",
    ]
    return lines
