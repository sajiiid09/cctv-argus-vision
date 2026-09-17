"""Per-local-day rollup: total dwell, allowance, overage.

The allowance is one hour of canteen time per day, not one hour per visit, so
overage is computed on the day's total rather than interval by interval.

ADR-0023 option (a): if any interval in the day is not RESOLVED -- or is
RESOLVED but missing a clip on either end -- the whole day is flagged and
contributes exactly zero. One bad event forgives a whole day. That is the
strictest reading of fail-open, it is deliberate, and it is not configurable:
a policy switch here would put "which cases resolve to a deduction" behind a
YAML key, which AGENTS.md §2.2 puts behind human sign-off instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from argus.payroll.dwell import measured_dwell_s
from argus.payroll.policy import PairingPolicy
from argus.payroll.types import DayResult, Flag, Interval, PairingState


def day_is_clean(intervals: Sequence[Interval]) -> bool:
    """Every interval resolved, and every resolved interval auditable.

    The clip condition is ADR-0008 ("no clip, no deduction") enforced where it
    can actually bite. Enforcing it only at export would mean enforcing it
    nowhere, because in shadow mode there is no export.
    """
    return all(
        interval.state is PairingState.RESOLVED and Flag.MISSING_CLIP not in interval.flags
        for interval in intervals
    )


def compute_day(
    person_id: str,
    space_id: str,
    local_day: date,
    intervals: Sequence[Interval],
    policy: PairingPolicy,
) -> DayResult:
    total = measured_dwell_s(intervals)
    flags = tuple(sorted({flag for interval in intervals for flag in interval.flags}))
    clean = day_is_clean(intervals)
    overage = max(0, total - policy.allowance_s) if clean else 0
    return DayResult(
        person_id=person_id,
        space_id=space_id,
        local_day=local_day,
        total_dwell_s=total,
        allowance_s=policy.allowance_s,
        overage_s=overage,
        day_state="clean" if clean else "flagged",
        flags=flags,
        interval_ids=tuple(i.interval_id for i in intervals),
    )
