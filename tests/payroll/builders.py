"""Builders for pairing tests.

Times are written as Asia/Dhaka wall clock, because that is how the policy and
every case in ARCHITECTURE.md §7.3 are expressed, and converted to the UTC instants
the code actually works with. Writing UTC in the tests would mean doing the
+06:00 arithmetic in one's head on every line, which is how a boundary test ends
up asserting the wrong day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from argus.common.clock import FixedClock
from argus.payroll import DoorEvent, Gap, PairingPolicy

DHAKA = ZoneInfo("Asia/Dhaka")
DAY = date(2026, 9, 17)
NEXT_DAY = date(2026, 9, 18)

_IDS = uuid5(NAMESPACE_URL, "sparrow-vision/tests/payroll")


def eid(label: str) -> UUID:
    """A stable event id derived from a label, so failures name the event."""
    return uuid5(_IDS, label)


def at(clock_time: str, day: date = DAY) -> datetime:
    """A local Dhaka wall time, as the UTC instant it denotes."""
    parts = [int(p) for p in clock_time.split(":")]
    while len(parts) < 3:
        parts.append(0)
    local = datetime.combine(day, time(*parts), tzinfo=DHAKA)
    return local.astimezone(UTC)


def ev(
    label: str,
    clock_time: str,
    direction: str,
    *,
    day: date = DAY,
    person: str | None = "p_alice",
    space: str = "canteen",
    door: str = "c1",
    camera: str = "canteen_door_01",
    clip: str | None = "clips/x.mp4",
    confidence: float | None = None,
    reported: str | None = None,
    duplicate_of: UUID | None = None,
) -> DoorEvent:
    return DoorEvent(
        event_id=eid(label),
        ts_utc=at(clock_time, day),
        space_id=space,
        camera_id=camera,
        direction=direction,  # type: ignore[arg-type]
        door_id=door,
        person_id=person,
        match_confidence=confidence,
        clip_ref=clip,
        duplicate_of=duplicate_of,
        ts_camera_reported=at(reported, day) if reported else None,
    )


def gap(
    from_time: str,
    to_time: str | None,
    *,
    day: date = DAY,
    space: str | None = "canteen",
    camera: str = "canteen_door_01",
    cause: str = "offline",
) -> Gap:
    return Gap(
        camera_id=camera,
        from_utc=at(from_time, day),
        to_utc=at(to_time, day) if to_time else None,
        cause=cause,
        space_id=space,
    )


def policy(**overrides: object) -> PairingPolicy:
    """A policy with a short allowance, so overage is easy to read in a test."""
    base: dict[str, object] = {
        "allowance_s": 600,
        "min_dwell_s": 60,
        "max_dwell_s": 10800,
        "duplicate_window_s": 3.0,
        "identity_threshold": 0.0,
        "clock_skew_tolerance_s": 60.0,
    }
    base.update(overrides)
    return PairingPolicy(**base)  # type: ignore[arg-type]


def clock_after(clock_time: str = "23:59", day: date = NEXT_DAY) -> FixedClock:
    """A clock late enough that the day under test has certainly closed."""
    return FixedClock(at(clock_time, day))
