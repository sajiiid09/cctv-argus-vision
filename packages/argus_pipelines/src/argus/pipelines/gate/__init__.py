"""argus.pipelines.gate -- badge taps, and whether the face matches the badge.

Attendance, not pay: nothing here produces a deduction, and a mismatch produces
a review item with a clip -- never a block, never an alarm (PLAN.md M4).

The split is by purity. This package holds the types and the decisions;
`services/gate` holds the process that talks to a database. `taps.py` is
stdlib-only because it is the contract two implementations and every test
share, so a dependency in it is a dependency everywhere.
"""
