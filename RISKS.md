# RISKS.md

How this gets gamed, who it can harm, and what we hold. Merges the former
`THREAT_MODEL.md` and `PRIVACY_AND_COMPLIANCE.md`, which argued the same point
from two directions.

Read it **before** building a pipeline, not after the first incident.

Two categories live here and must not be conflated. **Attacks on the
measurement** — someone avoiding a deduction — are common, mostly cheap, and
mostly end in no deduction. **Attacks on the people measured** — misuse of the
footage or the biometrics — are rarer, far more serious, committed by insiders,
and least likely to be reported because nobody involved is motivated to report
them.

**Nothing here is legal advice.** §13 needs a Bangladeshi labour lawyer, and the
questions should be asked before the payroll flag is turned on, not after someone
contests a deduction. Whether the system is deployed, what workers are told, and
how disputes are handled are management decisions made without our input. This
document covers what we build, which we control, and what we escalate rather than
answer.

---

## 1. What we hold

| Data | Personal? | Biometric? |
|---|---|---|
| Roster (name, employee ref, line, seat) | Yes | No |
| Face templates (embeddings) | Yes | **Yes** |
| Enrolment images | Yes | **Yes** |
| Doorway and gate events | Yes | No |
| Clips at doorways and the gate | Yes (faces) | Effectively |
| Dwell, overage, payroll lines | Yes | No |
| Occupancy samples | No `person_id` — but see §3 | No |
| Violence candidate clips | Yes (faces) | Effectively |

All of it on-prem. Face embeddings are biometric data under every regime we are
likely to be measured against; "just numbers, not a photo" is not a defence. It
identifies a person, it is derived from their body, and it is not revocable — a
worker can be issued a new badge, not a new face.

## 2. Handling rules (architectural, not aspirational)

- **Templates and clips never leave the site.** No cloud face API, no vendor
  telemetry, no uploading a clip to a model provider to help debug. This
  constrains ADR-0010 rather than following from it.
- **No face crops or clips in logs, error trackers or metrics.** A stack trace
  with a frame attached is a biometric leak with good intentions.
- **Templates encrypted at rest** — mechanism **OPEN**, and unbuilt as of
  2026-09-17.
- **Enrolment is privileged and logged.** It is what makes a person trackable at
  doorways, and it is the most sensitive write in the system.
- **Reads are audited, not just writes.** Who watched which clip, when, and why.
  A system whose premise is auditability cannot have an unaudited viewing path.
- **A rented or cloud GPU box runs synthetic and public footage only.** Site
  footage does not go on it; that follows from the first rule.

## 3. Occupancy is anonymous, but only just

It stores no `person_id` and has no join path to one. But seats are assigned, so
per-seat occupancy is functionally per-operator to anyone holding the roster —
which is everyone who matters. Be honest about that rather than hiding behind the
schema. Consequences: per-seat views sit behind a higher access tier than
aggregates (ADR-0019); occupancy cannot touch pay (ADR-0003) and a test enforces
it; and it gets shorter retention than doorway events, because nobody will ever
need to prove a seat was empty on the 14th.

---

## 4. Attacks on the measurement

| Attack | Plausibility | Effect | Response |
|---|---|---|---|
| Photo or phone at the gate camera | Moderate — easy and works against naive systems | Buddy-punching succeeds | Liveness is a requirement of the face decision (ADR-0010), not an extra. **Unbuilt as of 2026-09-17: a printed photo passes.** Gate cameras are supervised in practice |
| Video replay on a screen | Lower — more effort | Same | Same |
| Face covering at the canteen door | **High, and mostly not malicious** — ordinary clothing | `unknown` → unpaired → zero | Fail open handles it. The risk is systematic silent loss; monitor `unknown` rates per door |
| Tailgating, two crossing as one | **High** — cheapest exploit, spreads by word of mouth | Missed event → zero | Accepted (ADR-0004). A pairing rate falling over weeks is the signature |
| Blocking the camera when leaving | Moderate | Unpaired exit → zero | Monitor per-door rates and obstruction |
| Twins / close relatives | Real in a large workforce | Wrong attribution | 1:1 gate verification is robust; 1:N canteen matching is not. A near-tie resolves to `unknown`, never to the better score |
| Bag on a chair to fake occupancy | **Moderate to high once people learn what it measures** | Wrong occupancy | Precisely why occupancy never touches pay. If it did, this would be worth money and universal within a month |
| Model drift (new uniforms, camera drift, seasonal light) | **High, and slow enough to be invisible** | Quiet accuracy loss | Monitor `unknown` and unpaired rates. A drifting model looks exactly like workers gaming the system; confusing the two is how an accusation lands on the wrong party |
| Adversarial clothing patterns | Very low — needs knowledge and intent absent here | — | None planned. Named so the question stops being asked |

The uncomfortable pattern: **most attacks on measurement succeed and produce no
deduction.** That is the design (ADR-0004). The mitigation is not to close each
hole but to watch the aggregate rates. A system where 40% of days are flagged is
not measuring anything, and the answer is better camera placement, never a
punitive default.

## 5. Attacks by staff with authority

Worst consequences, least likely to be reported.

| Attack | Effect | Response |
|---|---|---|
| Supervisor unplugs, reaims or covers a camera | Silent loss of measurement | `stream_gap` recorded and surfaced; an unexplained gap is an operational event. Aim drift is caught by a reference-frame check (ADR-0029) |
| Supervisor disables a pipeline "temporarily" | Measurement gap | Same. This is the failure mode that motivated no-badge-at-canteen |
| False template enrolled for a person | Dwell attributed to the wrong person | Enrolment restricted and logged with an actor |
| Reviewer dismisses violence candidates without watching | Incidents lost | Review actions logged with timing; sub-second dismissals are visible |
| Events edited to alter a deduction | Wrong pay, and it looks defensible | Events are append-only; derived values are recomputed, never hand-edited. Any edit path is an architectural violation |
| Casual clip browsing (voyeurism, targeting someone) | **Serious harm, no measurement signal at all** | Reads audited, access needs a reason. The misuse most likely to happen and least likely to be reported; the audit log is the only control that touches it |
| Exporting biometric templates | Irreversible — no new face | Export is not a supported operation; admin group minimal |

## 6. Attacks on the evidence

Auditability is the central promise, so denying evidence is an attack, and a
quieter one than falsifying it.

| Attack | Effect | Response |
|---|---|---|
| DVR client limit exhausted | Our stream refused, events lost | One consumer per channel, fanned out internally. Refusals recorded as gaps, never dropped |
| Clips deleted before review | A deduction becomes unauditable | Retention consults references, not just age |
| Storage filled so retention deletes early | Same, without touching anything | Monitor free space; refuse to deduct without a clip (ADR-0008) — the invariant holds even here |
| Clock tampering | Intervals shift; an exit can precede its enter | Server clock authoritative and NTP-disciplined; camera time recorded, never used in arithmetic; detected skew flags the window and forces zero |
| Record-to-clip mapping lost | Silent unauditability | "No clip, no deduction" enforced in pairing, so it fails loudly |

## 7. Accidental threats — the most likely of all

Not adversarial, same consequences, far more common:

- **A canteen exit nobody mentioned.** Systematic unpaired enters for everyone
  who uses it. Found by a site survey, not by code.
- **Backlit doorway.** `unknown` rate rises at the same hour every day. Found by
  looking at rates *by hour*.
- **Camera knocked out of alignment by cleaning.** The door line is no longer
  where config says. Caught by the reference-frame check.
- **Model upgrade invalidating templates.** Embeddings are not comparable across
  models; without `model_ref` this is silent and catastrophic, with it an error
  at startup.
- **Server timezone left at UTC.** Every pay-period boundary shifts six hours.
  Policy timezone comes from config, never from the system.
- **Demo footage reaching a payroll export.** Prevented by `is_virtual` and a
  test, because under demo pressure this is genuinely plausible.

## 8. Monitoring that doubles as threat detection

The same handful of metrics catch gaming, degradation and misconfiguration.
They exist from the first pipeline, not after the first incident.

- `unknown` face rate, **per door, per hour** — obstruction, lighting, drift.
- Unpaired event rate, per door, per day — tailgating or a missed exit route.
- Stream gap minutes, per camera, per day — tampering or hardware.
- Flagged-day percentage — high means we are not measuring; fix capture, never
  the fail-open rule.
- Clip access counts, per user — misuse.
- Review dismissal latency — near zero means rubber-stamping.

None of these distinguishes an attack from a fault on its own. They are prompts
to go and look at a clip, which is the point of building a system where looking
at a clip is always possible.

---

## 9. Retention

**PROVISIONAL** — these are engineering placeholders, not advice.

| Data | Provisional | Reason |
|---|---|---|
| Continuous recording | Days, storage-bound | Not evidence unless promoted to a clip |
| Clips referenced by a payroll line | Pay period + dispute window + margin | Must stay auditable while it can be challenged |
| Clips for flagged/unpaired events | Same | These are what people argue about |
| Violence candidate clips | Until reviewed + short tail | Then resolved and deleted |
| Doorway and gate events | Long — small, and they are the evidence | Reproducing an old dispute needs them |
| Face templates | Roster-active + short tail | Shortest defensible life for biometric data |
| Enrolment images | Retained in the clear for the demo roster (ADR-0027) | Scoped to consenting team members; does not survive contact with real workers |
| Occupancy | Short, aggregated early | No evidentiary role |
| Access/audit logs | Longest | The log that proves the rest was handled properly |

**Hard rule:** a retention job may not delete a clip referenced by a payroll line
or an open dispute. Retention consults references, not just age. Getting this
wrong destroys exactly the evidence that protects the worker.

**Leaver rule:** when a worker leaves, templates go on the short tail; events and
clips tied to lines they could still dispute stay until that window closes.
Deleting a leaver's evidence early protects nobody.

## 10. Access control

| Tier | Sees | Notes |
|---|---|---|
| Viewer | Aggregate dashboards | No clips, no per-seat |
| Reviewer | Review queue + clips | Every view logged |
| Payroll | Overage lines + export | Cannot browse clips freely |
| Admin | Enrolment, config, retention | Smallest possible group |

Two operations sit above the tiers and need named human authorisation:
**enrolment** and **the shadow-mode flag**.

Tiers are **unbuilt as of 2026-09-18**; ADR-0028 settles their shape (per-role
passphrase, typed actor, localhost-bound; `payroll` reaches a clip only through
the line that references it). Clip-access logging is built
regardless, because it is the control that touches the voyeurism threat and it
does not depend on the authentication being good.

## 11. Shadow mode

The deduction is computed and reported, never exported, until a flag is set.
Preconditions before that flag is even considered:

1. Legal review (§13) complete.
2. Buyer-compliance review complete (§12).
3. A pilot with measured error rates — unpaired events, unknown faces, stream
   gaps, per door and per hour.
4. A dispute path that exists in practice, not on a slide: a named person who
   receives a challenge, access to the clip, and a way to reverse a line.
5. Human sign-off, logged (`AGENTS.md` §2).

Flipping it is a logged action with a named actor. It is the single most
consequential setting in the system and must not be an ordinary config value in
an ordinary file. Today there is no export to enable, and a database trigger
rejects any attempt to mark a payroll line exported.

## 12. Buyer audits (BSCI / SMETA / WRAP)

A camera-driven deduction system is squarely in scope. Auditors ask three things:

1. **Is the deduction lawful and correctly calculated?** This is where
   auditability earns its cost — a clip and a timestamp per line, in minutes.
2. **Was it applied consistently?** Fail-open helps: errors bias toward not
   deducting, and every exception is flagged rather than silently absorbed.
3. **Can a worker contest it?** A *factory process* question, not a software one.
   We supply the evidence trail and the ability to reverse a line; we cannot
   supply the process. If none exists, that is a finding against the factory, and
   we should say so internally before an auditor says it externally.

An auditor who finds a deduction system nobody can explain or contest is a
materially worse outcome for the factory than not having one. The compliance case
and the engineering case point the same way.

## 13. Open legal questions — need a Bangladeshi labour lawyer

None are answered. Several block the payroll flag.

1. **Are wage deductions for canteen overage lawful** under the Bangladesh
   Labour Act 2006 and its rules — under which permitted category, with what
   notice, subject to what cap? Everything else depends on this, and it is not a
   technology question.
2. **Does the applicable framework require notice or consent for biometric
   processing**, and in what form? Bangladesh has no comprehensive
   GDPR-type statute as of this writing; draft legislation has circulated. Get
   current advice rather than reasoning from the last thing anyone read.
3. **Is a legally sufficient dispute process required**, and what must it look
   like?
4. **Retention limits** for biometric data and for video evidence of a wage
   decision — minimums for audit and maximums for privacy may both exist and may
   conflict.
5. **Participation-committee or union obligations** before introducing a
   measurement system that affects pay.
6. **Buyer code obligations** binding the factory contractually, possibly
   stricter than local law.
7. **CCTV notice requirements** for the workplace generally, separate from the
   biometric question.
8. **Whether a failed gate verification may affect attendance.** Our design says
   it produces a review item, never an automatic absence. Confirm that is also
   the legally safe position, because it is the one we have built.

Until 1, 2 and 3 have answers, the payroll flag stays off. That is not caution
theatre: writing a deduction we cannot defend is the one failure in this project
that lands on someone who had no say in any of it.

## 14. What we are not defending against

Named so the boundary is explicit rather than assumed: nation-state or
sophisticated targeted attack; physical seizure of the server; a determined
insider with admin access and intent — audit logs make that *detectable*, not
impossible, and that is the honest limit of the control; network attacks beyond
basic segmentation (cameras on their own VLAN, reachable only from the analysis
box).
