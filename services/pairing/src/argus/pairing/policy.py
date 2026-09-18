"""The one place config becomes a PairingPolicy.

One place, because two mappings are two chances to disagree about what an
allowance is. It is a pure function, so it is testable with no database and no
clock.
"""

from __future__ import annotations

from argus.common.config import AppConfig
from argus.payroll import PairingPolicy


def policy_from_config(config: AppConfig) -> PairingPolicy:
    """Build the policy a run will be recorded against.

    The timezone comes from ``AppConfig.timezone`` and nowhere else: pay periods
    and the canteen allowance are local-day concepts, and a second source for
    the timezone would eventually shift every day boundary by six hours in one
    place and not the other.
    """
    payroll = config.payroll
    if not payroll.shadow_mode:  # pragma: no cover - the config refuses this first
        raise ValueError("shadow_mode is not optional (ADR-0005)")
    return PairingPolicy(
        timezone=config.timezone,
        allowance_s=payroll.allowance_s,
        min_dwell_s=payroll.min_dwell_s,
        max_dwell_s=payroll.max_dwell_s,
        duplicate_window_s=payroll.duplicate_window_s,
        identity_threshold=payroll.identity_threshold,
        clock_skew_tolerance_s=payroll.clock_skew_tolerance_s,
    )


def lead_in_seconds(config: AppConfig, policy: PairingPolicy) -> int:
    """How far back to read beyond the window's start.

    An interval that began before the window must not be truncated into an
    `UNPAIRED_ENTER`: that would turn a clean pair into a flag, or -- worse, in
    the other direction -- let a window boundary decide whether somebody is
    charged. So the reader overlaps by the longest an interval can legitimately
    be, and the run records the lead-in it used.
    """
    configured = config.payroll.lead_in_s
    return int(configured if configured is not None else policy.max_dwell_s)
