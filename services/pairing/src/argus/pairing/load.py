"""Reading the evidence a run is computed from.

Three of the decisions here are payroll-affecting, and each of them is a way a
careless reader could change who gets charged:

* **Space comes from the camera row, never from the door.** Pairing is per
  canteen space (ARCHITECTURE.md §7.3): entering by one door and leaving by
  another is normal, and a per-door reading would flag half the workforce.
* **Gaps load per space, for any camera in it.** The unseen exit might have been
  at the other door, so a gap on either one affects intervals in the space.
* **Open gaps load with `to_utc=None`.** Dropping a gap because it has no end is
  the most plausible way to charge somebody straight through an outage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from argus.payroll import DoorEvent, Gap
from argus.store.db import Database

log = logging.getLogger(__name__)


class LoadError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Window:
    from_utc: datetime
    to_utc: datetime
    lead_in_s: int
    space_ids: tuple[str, ...]

    @property
    def read_from_utc(self) -> datetime:
        return self.from_utc - timedelta(seconds=self.lead_in_s)


@dataclass(frozen=True, slots=True)
class Evidence:
    events: tuple[DoorEvent, ...]
    gaps: tuple[Gap, ...]
    window: Window


async def canteen_spaces(db: Database) -> tuple[str, ...]:
    rows = await db.fetch_all(
        "select distinct space_id from camera where role = 'canteen_door'"
        " and space_id is not null order by space_id"
    )
    return tuple(row[0] for row in rows)


async def load_window(db: Database, window: Window) -> Evidence:
    events = await _load_events(db, window)
    gaps = await _load_gaps(db, window)
    log.info(
        "loaded %d event(s) and %d gap(s) for %s from %s to %s (lead-in %ds)",
        len(events),
        len(gaps),
        ",".join(window.space_ids) or "every space",
        window.from_utc.isoformat(),
        window.to_utc.isoformat(),
        window.lead_in_s,
    )
    return Evidence(events=tuple(events), gaps=tuple(gaps), window=window)


async def _load_events(db: Database, window: Window) -> list[DoorEvent]:
    params: list[object] = [window.read_from_utc, window.to_utc]
    space_filter = ""
    if window.space_ids:
        space_filter = " and c.space_id = any(%s)"
        params.append(list(window.space_ids))
    rows = await db.fetch_all(
        "select e.event_id, e.ts_utc, c.space_id, e.camera_id, e.direction, e.door_id,"
        " e.ts_camera_reported, e.person_id, e.match_confidence, e.clip_ref, e.duplicate_of,"
        " c.role"
        " from doorway_event e join camera c on c.camera_id = e.camera_id"
        f" where e.ts_utc >= %s and e.ts_utc < %s{space_filter}"
        " order by e.ts_utc, e.event_id",
        tuple(params),
    )
    events: list[DoorEvent] = []
    for row in rows:
        space_id = row[2]
        if space_id is None:
            # CameraConfig refuses a canteen door with no space, so a null here
            # means somebody wrote a camera row by hand. Refuse rather than
            # invent a space: a wrong space pairs the wrong crossings together.
            raise LoadError(
                f"camera {row[3]} (role {row[11]}) has no space_id but produced doorway "
                f"event {row[0]}; pairing is per space and there is nothing to guess"
            )
        events.append(
            DoorEvent(
                event_id=row[0],
                ts_utc=row[1],
                space_id=space_id,
                camera_id=row[3],
                direction=row[4],
                door_id=row[5],
                ts_camera_reported=row[6],
                person_id=row[7],
                match_confidence=row[8],
                clip_ref=row[9],
                duplicate_of=row[10],
            )
        )
    # Duplicates are NOT filtered here. argus.payroll.normalise.dedupe owns the
    # window that decides what a duplicate is, and it must own it alone.
    return events


async def _load_gaps(db: Database, window: Window) -> list[Gap]:
    params: list[object] = [window.read_from_utc, window.to_utc]
    space_filter = ""
    if window.space_ids:
        space_filter = " and c.space_id = any(%s)"
        params.append(list(window.space_ids))
    rows = await db.fetch_all(
        "select g.camera_id, g.from_utc, g.to_utc, g.cause, c.space_id"
        " from stream_gap g join camera c on c.camera_id = g.camera_id"
        # An open gap (to_utc null) overlaps everything after it starts, and a
        # gap that started before the window may still cover part of it.
        f" where (g.to_utc is null or g.to_utc >= %s) and g.from_utc < %s{space_filter}"
        " order by g.from_utc",
        tuple(params),
    )
    return [
        Gap(camera_id=row[0], from_utc=row[1], to_utc=row[2], cause=row[3], space_id=row[4])
        for row in rows
    ]
