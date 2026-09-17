# GLOSSARY.md

Shared vocabulary. Domain terms first, then ours. The point is that humans and
agents use the same words for the same things — especially the words that decide
whether code touches someone's pay.

---

## Garments domain

**Line** — a production line: a sequence of workstations that together assemble a
garment. The unit management thinks in.

**Operator** — a worker at a sewing or finishing station. The person this system
measures. Preferred over "employee" or "worker" in code and UI, because it is the
term the factory uses.

**Seat / workstation** — a fixed position on a line, assigned to an operator.
Because assignment is fixed, a seat identifies a person via the roster — which is
why we need no floor tracking (ADR-0002), and why per-seat occupancy is less
anonymous than its schema suggests (`PRIVACY_AND_COMPLIANCE.md` §3).

**Bundle** — a tied batch of cut pieces moving along the line. Operators
sometimes leave their seat to collect or pass bundles, which is one reason an
empty seat does not mean idleness.

**Takt / takt time** — the pace a line must sustain to hit its target; the target
seconds per piece per station. We do not measure it. Listed because it will come
up and because someone will eventually propose inferring it from occupancy, which
would be a bad inference.

**Roster** — the list of who works, on which line, in which seat, for a given
period. Our source of identity outside the vision system. Churns often, so it is
time-bounded rather than mutable.

**Buddy-punching** — one worker clocking in for an absent colleague. The specific
fraud the gate face-verification pipeline exists to detect. Detect, not prevent:
the outcome is a flagged review item, never a blocked entry.

**Shift** — the working period. Shift boundaries matter because dwell intervals
and pay periods are local-day concepts in `Asia/Dhaka`.

**Overage** — canteen dwell beyond the allowance (currently one hour per local
day, aggregated across visits). The only quantity in this system that can reduce
pay.

**Allowance** — the permitted canteen time. Policy, not a constant in code.

**BSCI / SMETA / WRAP** — buyer-driven social-compliance audit schemes. They will
examine any camera-driven wage deduction. See `PRIVACY_AND_COMPLIANCE.md` §7.

---

## System terms

**Doorway event** — one append-only record: a person (or `unknown`) crossing a
door line in a direction, at a server-authoritative time, with a clip reference.
The evidence from which everything payroll-affecting derives.

**Direction** — `enter` | `exit` | `ambiguous`, from a short single-camera track
across the door line. Not tracking, not re-ID: it lasts a second or two and dies
at the door.

**Pairing** — turning doorway events into dwell intervals via the state machine
in `DATA_MODEL.md` §4. Where the care lives.

**Dwell / dwell interval** — time between a paired enter and exit, per canteen
*space* (not per door: a canteen with two doors is one space).

**Unpaired** — an enter with no exit, or an exit with no enter. Always resolves to
zero deduction plus a flag. Never inferred, never estimated.

**Implausible** — a dwell outside sane bounds (provisionally < 60 s or > 3 h).
Zero deduction, flagged. Usually a missed event, not a real three-hour lunch.

**Fail open** — the project's governing rule: any ambiguity produces zero
deduction and a review flag, never a punitive default. `SOUL.md`, ADR-0004.

**Shadow mode** — deductions computed and reported but not exported to payroll.
The default, until legal and buyer-compliance review plus explicit human sign-off
(ADR-0005).

**Review queue** — the human work list: unpaired and implausible events, gate
mismatches, violence candidates. Items are questions for a person, never verdicts.

**Gate verification** — 1:1 face check against the badge holder's template. A
different problem from canteen 1:N identification, with a different threshold.
Conflating them is a known and expensive mistake.

**`no_face` vs `mismatch`** — not seeing a face (a camera problem) versus seeing
the wrong face (a buddy-punching signal). Never collapse these into a boolean.

**Occupancy** — anonymous per-seat occupied/empty on the floor. A management
indicator. Never touches pay (ADR-0003).

**Two separate numbers** — shorthand for the rule that canteen overage (payroll)
and occupancy (management) are computed, stored and displayed separately and are
never merged.

**Virtual camera rig / the rig** — recorded and synthetic video served over RTSP
so the pipeline cannot tell it from a camera. Infrastructure, not a fixture: it
is the CI video source and the demo-day fallback. `ARCHITECTURE.md` §6.

**`is_virtual`** — the camera flag that prevents rig-derived data reaching a
payroll export.

**Golden frame** — a committed frame or clip with reference model outputs, used
to measure backend divergence across platforms. `TESTING.md` §3.

**Parity / divergence** — how far a backend's output sits from the reference. The
goal is bounded and visible, not zero.

**Backend** — a concrete implementation of `Detector`, `PoseEstimator`,
`FaceEmbedder` or `ClipClassifier`. Selected at runtime by capability probing.
Application code never imports one.

**Stream gap** — a recorded interval during which a camera produced nothing.
First-class, because "we saw nothing" and "nothing happened" are different facts.

**Clip reference** — the pointer from a record to retrievable video. No clip, no
deduction (ADR-0008).

**Template** — a face embedding tied to a person and to the model version that
produced it. Embeddings from different models are not comparable, which is why
`model_ref` is stored.

**Enrolment** — registering a person's face template. The most privileged write
in the system.

**Pre-roll / ring buffer** — retained recent frames so a clip can start *before*
its trigger. Without it, violence clips begin after the interesting part.

**Sparrow Vision** — the product name (ADR-0024). The code namespace is
`argus.`; that is an import path, not a product name, and the two coexist
deliberately.
