# PRIVACY_AND_COMPLIANCE.md

Biometric handling, retention, access, the shadow-mode gate, disputes, buyer
audits, and the legal questions we are **not** qualified to answer.

**Nothing here is legal advice.** Several questions in §8 need a Bangladeshi
labour lawyer, and they should be asked before the payroll flag is turned on, not
after someone contests a deduction.

**Division of responsibility.** Whether the system is deployed, what workers are
told, and how disputes are handled inside the factory are management decisions,
and they have been made without our input. This document therefore describes (a)
what we build, which we control, and (b) the questions we escalate rather than
answer. It does not promise anything on workers' behalf, because we cannot keep
such a promise. See `SOUL.md`.

---

## 1. What personal data the system holds

| Data | Personal? | Biometric? | Where |
|---|---|---|---|
| Roster (name, employee ref, line, seat) | Yes | No | On-prem DB |
| Face templates (embeddings) | Yes | **Yes** | On-prem DB |
| Enrolment images | Yes | **Yes** | On-prem, if retained (open) |
| Doorway events (person, time, door) | Yes | No | On-prem DB |
| Clips at doorways and the gate | Yes (faces) | Effectively | On-prem clip store |
| Dwell, overage, payroll lines | Yes | No | On-prem DB |
| Occupancy samples | No `person_id` — but see §3 | No | On-prem DB |
| Violence candidate clips | Yes (faces) | Effectively | On-prem clip store |

Face embeddings are biometric data under every regime we are likely to be
measured against. The fact that an embedding is "just numbers" and not a photo is
not a defence: it identifies a person, it is derived from their body, and it is
not revocable — a worker can be issued a new badge, not a new face.

---

## 2. Handling rules (these are architectural, not aspirational)

- **Templates and clips never leave the site.** No cloud face API, no vendor
  telemetry, no uploading a clip to a model provider for "help debugging". This
  constrains the face-recognition decision (ADR-0010) rather than following from
  it.
- **No face crops or clips in logs, error trackers or metrics.** A stack trace
  with an attached frame is a biometric leak with good intentions.
- **Templates encrypted at rest**, with the key held on the box and not in the
  repo. Mechanism undecided (**OPEN**).
- **Enrolment is a privileged, logged operation.** Adding a template is the most
  sensitive write in the system: it is what makes a person trackable at doorways.
- **Reads are audited, not just writes.** Who watched which clip, when, and
  under what justification. A system whose premise is auditability cannot have an
  unaudited viewing path — and clip viewing is the path most open to casual
  misuse by whoever has the login.
- **Cloud staging runs synthetic and public footage only.** If the Linux staging
  box is a rented GPU instance (`ARCHITECTURE.md` §5.7), site footage does not go
  on it. That is not a preference; it is the first rule above.

---

## 3. Occupancy is anonymous, but only just

Occupancy stores no `person_id` and has no join path to one. But seats are
assigned, so per-seat occupancy is functionally per-operator to anyone holding
the roster — which is everyone who matters.

Be honest about this rather than hiding behind the schema. Consequences:

- Per-seat views sit behind a higher access tier than aggregate views
  (ADR-0019, leaning (c)).
- Occupancy cannot touch pay (ADR-0003), and a test enforces it.
- Shorter retention and earlier aggregation than doorway events, because
  occupancy has no evidentiary role — nobody will ever need to prove a seat was
  empty on the 14th.

---

## 4. Retention

**PROVISIONAL** — retention periods are a legal question (§8), and the numbers
below are engineering placeholders, not advice.

| Data | Provisional | Reason |
|---|---|---|
| Continuous recording | Short, storage-bound (days) | Not evidence unless promoted to a clip |
| Clips referenced by a payroll line | Pay period + full dispute window + margin | A deduction must stay auditable while it can be challenged |
| Clips for flagged/unpaired events | Same | These are what people argue about |
| Violence candidate clips | Until reviewed + short tail | Then resolved and deleted |
| Doorway events | Long (small, and they are the evidence) | Reproducing an old dispute needs them |
| Face templates | Roster-active + short tail | Shortest defensible life for biometric data |
| Enrolment images | Retain or discard — **OPEN** | Retaining allows re-embedding on model upgrade without re-enrolling everyone; discarding shrinks the biometric footprint. Trade-off, not an oversight |
| Occupancy | Short, aggregated early | No evidentiary role |
| Access/audit logs | Longest | The log that proves the rest was handled properly |

**Hard rule:** a retention job may not delete a clip referenced by a payroll line
or an open dispute. Retention consults references, not just age. Getting this
wrong destroys exactly the evidence that protects the worker.

**Leaver rule:** when a worker leaves, templates go on the short tail; doorway
events and clips tied to payroll lines they could still dispute stay until that
window closes. Deleting a leaver's evidence early protects nobody.

---

## 5. Access control

| Tier | Sees | Notes |
|---|---|---|
| Viewer | Aggregate dashboards | No clips, no per-seat |
| Reviewer | Review queue + clips | Every view logged |
| Payroll | Overage lines + export | Cannot browse clips freely |
| Admin | Enrolment, config, retention | Smallest possible group |

Two operations sit above the tiers and require named human authorisation:
**enrolment** and **the shadow-mode flag**.

---

## 6. Shadow mode

The deduction is computed and reported but never exported until a config flag is
set. Preconditions before that flag is considered:

1. Legal review (§8) complete.
2. Buyer-compliance review complete (§7).
3. A pilot period with measured error rates — unpaired events, unknown faces,
   stream gaps, per door and per hour (`PLAN.md` M9).
4. A dispute path that exists in practice, not on a slide: a named person who
   receives a challenge, access to the clip, and a way to reverse a line.
5. Human sign-off, logged (`AGENTS.md` §2).

Flipping the flag is a logged action with a named actor. It is the single most
consequential setting in the system and must not be an ordinary config value in
an ordinary file.

---

## 7. Buyer audits (BSCI / SMETA / WRAP)

Garments buyers audit working conditions, wage practice and record-keeping. A
camera-driven deduction system is squarely in scope, and auditors will ask three
things:

1. **Is the deduction lawful and correctly calculated?** This is where
   auditability earns its cost — a clip and a timestamp per line, retrievable in
   minutes.
2. **Was it applied consistently?** Fail-open helps: errors bias toward not
   deducting, and every exception is flagged rather than silently absorbed.
3. **Can a worker contest it?** This is a *factory process* question, not a
   software question. We can supply the evidence trail and the ability to reverse
   a line; we cannot supply the process. If no process exists, that is a finding
   against the factory, and we should say so internally before an auditor says it
   externally.

Also expect questions about biometric consent and data handling. We hold the
technical answers (§2, §4, §5). The legal and consent answers are §8 and are not
ours.

A note worth putting in writing: an auditor who discovers a deduction system
nobody could explain or contest is a materially worse outcome for the factory
than not having one. The compliance case and the engineering case point the same
direction here.

---

## 8. Open legal questions — need a Bangladeshi labour lawyer

None of these are answered. Several block the payroll flag.

1. **Are wage deductions for canteen overage lawful** under the Bangladesh Labour
   Act 2006 and its rules — and if so, under which permitted category, with what
   notice, and subject to what cap? This is the question everything else depends
   on, and it is not a technology question.
2. **Does the applicable legal framework require worker notice or consent for
   biometric processing**, and what form must it take? Bangladesh has no
   comprehensive data-protection statute of the GDPR type as of this writing;
   draft legislation has been circulating. Get current advice rather than
   reasoning from the last thing anyone read.
3. **Is a legally sufficient dispute process required**, and what does it have to
   look like?
4. **Retention limits** for biometric data and for video evidence of a wage
   decision — minimums (for audit) and maximums (for privacy) may both exist and
   may conflict.
5. **Participation-committee or union obligations** before introducing a
   measurement system that affects pay.
6. **Buyer code obligations** that bind the factory contractually and may be
   stricter than local law.
7. **CCTV notice requirements** for the workplace generally, separate from the
   biometric question.
8. **Whether a failed face verification at the gate may affect attendance** —
   our design says it produces a review item, never an automatic absence. Confirm
   that is also the legally safe position, because it is the one we have built.

Until 1, 2 and 3 have answers, the payroll flag stays off. That is not caution
theatre: writing a deduction we cannot defend is the one failure in this project
that lands on someone who had no say in any of it.
