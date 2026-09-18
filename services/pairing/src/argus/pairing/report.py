"""The monitoring numbers RISKS.md §8 asks for from day one.

    python -m argus.pairing.report --config config/dev.yaml --day 2026-09-18

These live beside the pairing runner because that is what produces most of
them, and they are **queries over stored rows**, not counters a pipeline keeps.
Two of the four M3 metrics are properties of pairing output rather than of any
frame -- the unpaired rate and the flagged-day percentage -- so a pipeline
counter for them would be permanently zero and quietly reassuring.

What each number is for, which is the part that gets lost:

* **unknown-face rate per door.** Rising means the door has stopped
  identifying people; the deductions do not rise with it, because unknown fails
  open. It is a capture problem, and it is the first thing to look at.
* **unpaired rate per door.** Rising means crossings are being seen once, not
  twice -- a camera, a geometry or a tailgating problem.
* **stream-gap minutes per camera.** How long we were not looking at all.
* **flagged-day percentage.** How often the system declines to charge. A
  *falling* number is not automatically good news: it can mean the flags stopped
  firing rather than the measurement improving.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from argus.common.config import load_config
from argus.store.db import Database

log = logging.getLogger("argus.pairing.report")


def _local_day_window(day: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


async def unknown_face_rate(db: Database, start: datetime, end: datetime) -> list[tuple]:
    return await db.fetch_all(
        "select door_id, date_trunc('hour', ts_utc) as hour,"
        " count(*) filter (where person_id is null) as unknown, count(*) as total"
        " from doorway_event where ts_utc >= %s and ts_utc < %s"
        " group by door_id, hour order by hour, door_id",
        (start, end),
    )


async def unpaired_rate(db: Database, day: date) -> list[tuple]:
    return await db.fetch_all(
        "select i.space_id,"
        " count(*) filter (where i.state in ('unpaired_enter', 'unpaired_exit')) as unpaired,"
        " count(*) as intervals"
        " from dwell_interval i"
        " join pairing_run r on r.pairing_run_id = i.pairing_run_id"
        # The latest run for the day: a rerun supersedes rather than adds.
        " where i.local_day = %s and r.computed_at = ("
        "   select max(r2.computed_at) from pairing_run r2"
        "   join dwell_interval i2 on i2.pairing_run_id = r2.pairing_run_id"
        "   where i2.local_day = %s)"
        " group by i.space_id order by i.space_id",
        (day, day),
    )


async def gap_minutes(db: Database, start: datetime, end: datetime) -> list[tuple]:
    return await db.fetch_all(
        "select camera_id, cause,"
        " round(sum(extract(epoch from (least(coalesce(to_utc, %s), %s)"
        "   - greatest(from_utc, %s))) / 60.0)::numeric, 1) as minutes"
        " from stream_gap"
        " where from_utc < %s and (to_utc is null or to_utc > %s)"
        " group by camera_id, cause order by camera_id, cause",
        (end, end, start, end, start),
    )


async def flagged_day_percent(db: Database, day: date) -> list[tuple]:
    return await db.fetch_all(
        "select count(*) filter (where day_state = 'flagged') as flagged, count(*) as days,"
        " coalesce(sum(overage_s), 0) as overage_s"
        " from dwell_day d"
        " join pairing_run r on r.pairing_run_id = d.pairing_run_id"
        " where d.local_day = %s and r.computed_at = ("
        "   select max(r2.computed_at) from pairing_run r2"
        "   join dwell_day d2 on d2.pairing_run_id = r2.pairing_run_id"
        "   where d2.local_day = %s)",
        (day, day),
    )


async def clip_access(db: Database, start: datetime, end: datetime) -> list[tuple]:
    return await db.fetch_all(
        "select actor, outcome, count(*) from clip_access_log"
        " where at >= %s and at < %s group by actor, outcome order by actor, outcome",
        (start, end),
    )


async def review_latency(db: Database, start: datetime, end: datetime) -> list[tuple]:
    """Time from a candidate appearing to a human deciding.

    On the page because near-zero latency means rubber-stamping, not diligence
    (RISKS.md §8).
    """
    return await db.fetch_all(
        "select review_state, count(*),"
        " round(avg(extract(epoch from (reviewed_at - created_at)))::numeric, 1) as mean_s"
        " from violence_candidate"
        " where created_at >= %s and created_at < %s and reviewed_at is not null"
        " group by review_state order by review_state",
        (start, end),
    )


async def report(config_path: str, day: date) -> list[str]:
    config = load_config(config_path)
    start, end = _local_day_window(day, config.timezone)
    db = await Database.connect(config.database.dsn)
    try:
        lines = [
            f"metrics for local day {day.isoformat()} ({config.timezone})",
            f"window {start.isoformat()} .. {end.isoformat()}",
            "",
            "unknown-face rate per door per hour (RISKS.md §8):",
        ]
        for door_id, hour, unknown, total in await unknown_face_rate(db, start, end):
            share = (unknown / total * 100.0) if total else 0.0
            lines.append(
                f"  {hour:%Y-%m-%d %H:%M} door={door_id or '-'} {unknown}/{total} ({share:.0f}%)"
            )
        lines += ["", "unpaired interval rate per space:"]
        for space_id, unpaired, intervals in await unpaired_rate(db, day):
            share = (unpaired / intervals * 100.0) if intervals else 0.0
            lines.append(f"  space={space_id} {unpaired}/{intervals} ({share:.0f}%)")
        lines += ["", "stream-gap minutes per camera:"]
        for camera_id, cause, minutes in await gap_minutes(db, start, end):
            lines.append(f"  {camera_id} {cause} {minutes} min")
        lines += ["", "days and charges:"]
        for flagged, days, overage_s in await flagged_day_percent(db, day):
            share = (flagged / days * 100.0) if days else 0.0
            lines.append(
                f"  {flagged}/{days} days flagged ({share:.0f}%), {overage_s}s overage total"
            )
        lines += ["", "clip access by actor:"]
        for actor, outcome, count in await clip_access(db, start, end):
            lines.append(f"  {actor} {outcome} {count}")
        lines += ["", "violence review latency:"]
        for state, count, mean_s in await review_latency(db, start, end):
            lines.append(f"  {state} n={count} mean={mean_s}s")
        lines += [
            "",
            "Nothing here is written to payroll: shadow mode (ADR-0005).",
        ]
        return lines
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="argus-report")
    parser.add_argument("--config", required=True)
    parser.add_argument("--day", required=True, help="local day, YYYY-MM-DD")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper())
    for line in asyncio.run(report(args.config, date.fromisoformat(args.day))):
        print(line)


if __name__ == "__main__":
    main()
