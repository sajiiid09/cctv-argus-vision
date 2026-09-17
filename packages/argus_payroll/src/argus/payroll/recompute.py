"""The single public entry point.

Pairing is a pure function of (events, gaps, policy, logic version). Running it
again over the same inputs must be safe and must produce the same answer, so
that a disputed line from months ago can be reproduced exactly rather than
argued about. Derived rows are written anew with a new run id; nothing is ever
edited in place.

The clock is read exactly twice -- once to stamp the result, once to ask whether
a day has closed -- which is few enough that the fixed-clock discipline can be
checked by reading the file.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from argus.common.clock import Clock
from argus.payroll.normalise import attributable, dedupe, group_by_person_space
from argus.payroll.overage import compute_day
from argus.payroll.pairing import walk
from argus.payroll.policy import PairingPolicy
from argus.payroll.types import (
    DayResult,
    DoorEvent,
    Gap,
    Interval,
    PairingResult,
    Unattributable,
)
from argus.payroll.version import LOGIC_VERSION


def run_pairing(
    events: Sequence[DoorEvent],
    gaps: Sequence[Gap],
    policy: PairingPolicy,
    clock: Clock,
) -> PairingResult:
    computed_at = clock.now_utc()
    fingerprint = policy.fingerprint()

    kept, duplicates = dedupe(events, policy)
    chargeable, excluded = attributable(kept, policy)

    intervals: list[Interval] = []
    open_enters: list[UUID] = []
    for (person_id, space_id), person_events in sorted(group_by_person_space(chargeable).items()):
        person_intervals, person_open = walk(
            person_events,
            gaps,
            policy,
            person_id=person_id,
            space_id=space_id,
            now_utc=computed_at,
            policy_fingerprint=fingerprint,
        )
        intervals.extend(person_intervals)
        open_enters.extend(person_open)

    by_day: dict[tuple[str, str, object], list[Interval]] = {}
    for interval in intervals:
        by_day.setdefault((interval.person_id, interval.space_id, interval.local_day), []).append(
            interval
        )

    days: list[DayResult] = [
        compute_day(person_id, space_id, day, group, policy)  # type: ignore[arg-type]
        for (person_id, space_id, day), group in sorted(
            by_day.items(), key=lambda kv: (kv[0][0], kv[0][1], str(kv[0][2]))
        )
    ]

    unattributable: list[Unattributable] = [*duplicates, *excluded]
    unattributable.sort(key=lambda u: (str(u.event_id), u.flag))

    return PairingResult(
        logic_version=LOGIC_VERSION,
        policy_fingerprint=fingerprint,
        computed_at=computed_at,
        intervals=tuple(sorted(intervals, key=_interval_sort_key)),
        days=tuple(days),
        open_enters=tuple(sorted(open_enters, key=str)),
        unattributable=tuple(unattributable),
    )


def _interval_sort_key(interval: Interval) -> tuple[str, str, str, str]:
    """Total order, independent of input order (TESTING.md §2 property 7)."""
    anchor = interval.start_utc or interval.end_utc
    assert anchor is not None
    return (
        interval.person_id,
        interval.space_id,
        anchor.isoformat(),
        str(interval.interval_id),
    )


__all__ = ["run_pairing"]
