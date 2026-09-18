"""argus.ui -- the operator console.

Two rules shape everything here.

**It renders stored rows and never recomputes.** A structural test asserts this
package imports no `argus.payroll`. The canteen page's header comes from
`pairing_run.policy_description`, written when the run was computed, so the page
describes the run that produced its numbers even if the config changed
afterwards (ADR-0015).

**Every clip view is logged, including the refused ones.** RISKS.md §5 names
casual clip browsing as the misuse most likely to happen and least likely to be
reported, and §10 says the log is built regardless of how good the
authentication turns out to be.
"""
