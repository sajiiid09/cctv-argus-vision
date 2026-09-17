"""Clock discipline (ARCHITECTURE.md §5.8, AGENTS.md §7).

Server time is the only authority for payroll-relevant timestamps; camera time
is recorded, never used for arithmetic. ``now()`` does not exist in this module
except behind the ``SystemClock`` implementation, so payroll code can be tested
with fixed clocks and never reads the wall clock by accident.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

DHAKA = "Asia/Dhaka"


class Clock(Protocol):
    def now_utc(self) -> datetime: ...
    def monotonic(self) -> float: ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        import time

        return time.monotonic()


class FixedClock:
    """Deterministic clock for tests. Time is an input (AGENTS.md §7)."""

    def __init__(self, start: datetime, monotonic_start: float = 0.0) -> None:
        if start.tzinfo is None:
            raise ValueError("FixedClock requires a tz-aware datetime")
        self._now = start
        self._mono = monotonic_start

    def now_utc(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now += timedelta(seconds=seconds)
        self._mono += seconds


def policy_timezone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def to_local(ts: datetime, tz: ZoneInfo) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("timestamp must be tz-aware (UTC in storage)")
    return ts.astimezone(tz)


def local_day(ts: datetime, tz: ZoneInfo) -> date:
    """The local calendar day a UTC timestamp belongs to.

    Pay periods, the canteen allowance and day attribution are local-day
    concepts; this is the single place the conversion happens (ARCHITECTURE.md §7.1).
    """
    return to_local(ts, tz).date()
