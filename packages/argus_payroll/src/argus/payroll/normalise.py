"""Turning raw evidence into the events the state machine may walk.

Two filters, in order, and both of them throw information away on purpose:
duplicates of one crossing collapse to one, and events that cannot be attributed
to a person leave the machine entirely. What they leave behind is recorded, so a
reviewer can see that the evidence existed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from uuid import UUID

from argus.payroll.policy import PairingPolicy
from argus.payroll.types import DoorEvent, Flag, Unattributable


class NormaliseError(Exception):
    pass


def ordered(events: Iterable[DoorEvent]) -> list[DoorEvent]:
    """Chronological, with ties broken by event id.

    The tie-break is not cosmetic: two events sharing a timestamp must order the
    same way on every run, or recomputation stops being deterministic
    (AGENTS.md §7 property 4).
    """
    out = list(events)
    for event in out:
        if event.ts_utc.tzinfo is None:
            raise NormaliseError(
                f"event {event.event_id} has a naive timestamp; storage is UTC and "
                "tz-aware (ARCHITECTURE.md §7.1)"
            )
    return sorted(out, key=lambda e: (e.ts_utc, str(e.event_id)))


def dedupe(
    events: Sequence[DoorEvent], policy: PairingPolicy
) -> tuple[list[DoorEvent], list[Unattributable]]:
    """Collapse repeat detections of a single crossing to the earliest.

    Two rules, and the second one exists because the first cannot be applied
    retrospectively: an event already marked ``duplicate_of`` by ingest is
    dropped, and any remaining event matching an earlier one on
    (person, door, direction) within the policy window is dropped too.

    The earliest survives because it is the better estimate of when the person
    actually crossed; later detections are the same person still in frame.
    """
    kept: list[DoorEvent] = []
    dropped: list[Unattributable] = []
    last_seen: dict[tuple[str | None, str | None, str], DoorEvent] = {}

    for event in ordered(events):
        if event.duplicate_of is not None:
            dropped.append(Unattributable(event.event_id, Flag.DEDUPLICATED))
            continue
        key = (event.person_id, event.door_id, event.direction)
        previous = last_seen.get(key)
        if (
            previous is not None
            and (event.ts_utc - previous.ts_utc).total_seconds() <= policy.duplicate_window_s
        ):
            dropped.append(Unattributable(event.event_id, Flag.DEDUPLICATED))
            continue
        last_seen[key] = event
        kept.append(event)
    return kept, dropped


def attributable(
    events: Sequence[DoorEvent], policy: PairingPolicy
) -> tuple[list[DoorEvent], list[Unattributable]]:
    """Split events into those chargeable to a person and those that are not.

    An unknown face, a match below the identity threshold, and a crossing whose
    direction could not be read all leave the machine. There is no person to
    charge, so there is nothing to compute -- and inventing one is the punitive
    default SOUL.md forbids.

    The consequence is worth stating: dropping an unknown *exit* turns the
    preceding known enter into UNPAIRED_ENTER, and the day contributes zero.
    That is fail-open working, not a bug to be smoothed over.
    """
    chargeable: list[DoorEvent] = []
    excluded: list[Unattributable] = []
    for event in events:
        if event.person_id is None:
            excluded.append(Unattributable(event.event_id, Flag.UNKNOWN_PERSON))
        elif (
            event.match_confidence is not None
            and event.match_confidence < policy.identity_threshold
        ):
            excluded.append(Unattributable(event.event_id, Flag.LOW_CONFIDENCE))
        elif event.direction == "ambiguous":
            excluded.append(Unattributable(event.event_id, Flag.CLOCK_ANOMALY))
        else:
            chargeable.append(event)
    return chargeable, excluded


def group_by_person_space(
    events: Sequence[DoorEvent],
) -> dict[tuple[str, str], list[DoorEvent]]:
    """One walk per (person, canteen space).

    Per space, never per door: entering by one door and leaving by another is
    ordinary behaviour, and a per-door machine would flag half the workforce
    every day (ARCHITECTURE.md §7.3).
    """
    groups: dict[tuple[str, str], list[DoorEvent]] = {}
    for event in events:
        assert event.person_id is not None  # guaranteed by attributable()
        groups.setdefault((event.person_id, event.space_id), []).append(event)
    return groups


def superseded_ids(events: Iterable[DoorEvent]) -> set[UUID]:
    """Ids that some other event points at as the survivor of a duplicate pair."""
    return {e.duplicate_of for e in events if e.duplicate_of is not None}
