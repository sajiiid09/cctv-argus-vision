"""argus.pairing -- the thin orchestrator around argus.payroll.

`argus.payroll` is a pure function of (events, gaps, policy, logic version) and
imports no database, so somebody has to read the evidence and write the result.
That is all this service does: load, call `run_pairing`, persist in one
transaction, notify.

Nothing here decides anything. If a number in the output looks wrong, the
answer is in argus.payroll or in the policy -- not here.
"""
