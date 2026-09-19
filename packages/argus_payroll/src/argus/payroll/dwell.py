"""Duration arithmetic.

Integer seconds throughout. No float ever reaches a number someone is paid on:
``timedelta.total_seconds()`` returns a float, and letting one through here is
how a wage acquires a rounding error nobody can explain.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from argus.payroll.types import Interval


def duration_s(start_utc: datetime, end_utc: datetime) -> int:
    """Whole seconds between two instants, truncated, never negative.

    Truncation rather than rounding, because the interval is measured evidence
    and the half-second at the end was not observed.
    """
    seconds = (end_utc - start_utc).total_seconds()
    return int(seconds) if seconds > 0 else 0


def measured_dwell_s(intervals: Iterable[Interval]) -> int:
    """Total measured dwell across intervals, whatever their state.

    Every computable duration counts, not only the resolved ones. That makes the
    bound in AGENTS.md §7 property 2 -- overage never exceeds the day's measured
    dwell -- a real constraint rather than a tautology over the same subset.
    """
    return sum(i.duration_s for i in intervals if i.duration_s is not None)
