"""argus.payroll -- pairing, dwell and overage (ARCHITECTURE.md §7.3).

Pure functions over stored events: no vision imports, no database, no wall
clock, all enforced by tests/structural/test_structure.py. Keeping probabilistic
code out of wage arithmetic is what makes the wage arithmetic testable
(AGENTS.md §4).

There is deliberately no export here. ADR-0005 keeps the system in shadow mode
and ADR-0006 says a human keys in a signed export; until that exists, an export
function in this package would be a loaded gun. A structural test asserts its
absence.
"""

from __future__ import annotations

from argus.payroll.dwell import duration_s, measured_dwell_s
from argus.payroll.overage import compute_day, day_is_clean
from argus.payroll.pairing import interval_id, overlaps_gap, walk
from argus.payroll.policy import PairingPolicy, PolicyError
from argus.payroll.recompute import run_pairing
from argus.payroll.types import (
    DayResult,
    DoorEvent,
    Flag,
    Gap,
    Interval,
    MachineState,
    PairingResult,
    PairingState,
    Unattributable,
)
from argus.payroll.version import LOGIC_VERSION

__all__ = [
    "LOGIC_VERSION",
    "DayResult",
    "DoorEvent",
    "Flag",
    "Gap",
    "Interval",
    "MachineState",
    "PairingPolicy",
    "PairingResult",
    "PairingState",
    "PolicyError",
    "Unattributable",
    "compute_day",
    "day_is_clean",
    "duration_s",
    "interval_id",
    "measured_dwell_s",
    "overlaps_gap",
    "run_pairing",
    "walk",
]
