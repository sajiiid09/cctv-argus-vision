# THREAT_MODEL.md

**Why this document exists.** Almost everything this system measures, someone has
a motive to distort: workers to avoid a deduction, supervisors to protect their
line's numbers, and anyone with access to misuse the footage. Split across
`ARCHITECTURE.md` and `PRIVACY_AND_COMPLIANCE.md`, that reasoning gets lost. It
belongs in one place that is read *before* each pipeline is built, not after the
first incident.

Scope: adversarial behaviour and accidental-but-systematic distortion. It is not
a general infosec review of the network.

A framing note. Two distinct categories live here and must not be conflated:
**attacks on the measurement** (someone avoiding a deduction) and **attacks on
the people measured** (someone misusing the footage or the biometrics). The
second is the more serious, is committed by insiders rather than workers, and is
the one most likely to go unnoticed because nobody is motivated to report it.

---

## 1. Attacks on identity at doorways

| Attack | Plausibility | Effect | Response |
|---|---|---|---|
| Photo or phone held to the gate camera | Moderate — easy, obvious, and it works against naive systems | Buddy-punching succeeds | Liveness/anti-spoofing is a requirement of the face decision (ADR-0010), not an optional extra. Gate cameras are supervised in practice, which helps. |
| Video replay on a screen | Lower — more effort | Same | Same |
| Face covering (mask, scarf, cap) at the canteen door | **High, and mostly not malicious** — this is ordinary clothing | `unknown` event → unpaired → zero deduction | Fail open handles it correctly. The real risk is a systematic, silent loss of measurement. Monitor `unknown` rates per door; a rising rate is an operational finding. |
| Tailgating: two people crossing as one | **High** — the cheapest exploit, and it spreads by word of mouth | Missed event → unpaired → zero deduction | Accepted (ADR-0004). Monitor: a dwell-pairing rate that falls over weeks is the signature. |
| Deliberately blocking the camera's view when leaving | Moderate | Unpaired exit → zero | Same. Monitor per-door rates and camera obstruction. |
| Twins / close relatives | Real in a large workforce | Wrong attribution | 1:1 gate verification is robust; 1:N canteen matching is not. Thresholds must be set with this in mind, and a near-tie should resolve to `unknown`, never to the better score. |

The uncomfortable pattern: **most attacks on measurement succeed and result in no
deduction.** That is the design (ADR-0004), and the mitigation is not to close
each hole but to watch the aggregate rates. A system where 40% of days are
flagged is not measuring anything, and the response to that is better camera
placement (M8), not a punitive default.

---

## 2. Attacks by staff with authority

The threats with the worst consequences, and the least likely to be reported.

| Attack | Effect | Response |
|---|---|---|
| Supervisor unplugs, reaims or covers a camera | Silent loss of measurement for a line or door | `stream_gap` recorded and surfaced; an unexplained gap is an operational event, not a shrug. Aim changes are harder — a reference-frame check that alerts on large scene changes is cheap and worth building. |
| Supervisor disables the canteen queue / pipeline "temporarily" | Measurement gap | Same. This is the failure mode that motivated the no-badge-at-canteen decision in the first place. |
| Someone enrols a false template for a person | Attribution of dwell to the wrong person | Enrolment is the most privileged write in the system: restricted, logged, with actor and image retained. |
| Reviewer dismisses violence candidates without watching | Incidents lost | Log review actions with timing. A pattern of sub-second dismissals is visible. |
| Someone edits events to alter a deduction | Wrong pay, defensibly-looking | Events are append-only; derived values are recomputed, never hand-edited. Any edit path is an architectural violation (`AGENTS.md`). |
| Casual browsing of clips (voyeurism, targeting an individual) | **Serious privacy harm, no measurement signal at all** | Reads are audited, not just writes. Clip access requires a reason. This is the misuse most likely to happen and least likely to be reported, and the audit log is the only control that touches it. |
| Exporting biometric templates | Irreversible harm — a worker cannot be issued a new face | Templates encrypted at rest; export is not a supported operation; admin group kept minimal. |

---

## 3. Attacks on the evidence

Auditability is the system's central promise, so denying evidence is an attack in
its own right — and a quieter one than falsifying it.

| Attack | Effect | Response |
|---|---|---|
| DVR concurrent-client limit exhausted (deliberately or by a second tool) | Our stream is refused; events lost | One consumer per channel, fanned out internally (`ARCHITECTURE.md` §6). Refusals are recorded as gaps, never dropped silently. |
| Deleting or overwriting clips before review | A deduction becomes unauditable | Retention consults references: a clip tied to a payroll line or an open dispute cannot be deleted by a retention job. |
| Filling storage so retention deletes early | Same, without touching anything directly | Monitor free space; refuse to deduct where the clip is missing (ADR-0008) — the invariant holds even under this attack. |
| Clock tampering on a camera or the server | Intervals shift; an exit can precede its enter | Server clock is authoritative and NTP-disciplined; camera-reported time recorded but never used in arithmetic; detected skew flags the window and forces zero. |
| Losing the mapping from record to clip | Silent unauditability | "No clip, no deduction" enforced at export, so the failure is loud rather than silent. |

---

## 4. Attacks on the models

| Attack | Plausibility | Response |
|---|---|---|
| Adversarial patterns on clothing to defeat detection | Very low here — requires knowledge and intent nobody in this setting has | None planned. Named so the question does not get asked repeatedly. |
| Gaming the violence trigger (horseplay for entertainment) | Moderate | Output is a human queue; a person dismisses it. Low cost. |
| Gaming seat occupancy (a bag on a chair, a jacket over a backrest) | **Moderate to high once people learn what it measures** | This is precisely why occupancy never touches pay. If it did, this attack would be worth money and would be universal within a month. |
| Model degradation over time (new uniforms, camera drift, seasonal light) | **High, and slow enough to be invisible** | Monitor `unknown` and unpaired rates as a health metric. A drifting model looks exactly like workers gaming the system — distinguishing them requires trend data plus spot-checking clips, and confusing the two is how an accusation gets made against the wrong party. |

---

## 5. Accidental threats (most likely of all)

Not adversarial, but with the same consequences, and in practice far more common:

- **A canteen exit nobody told us about.** Systematic unpaired enters for
  everyone who uses it. Found by a site survey (M8), not by code.
- **Backlit doorway.** Faces silhouetted at certain hours; `unknown` rate rises
  at the same time every day. Found by looking at rates *by hour*.
- **Camera knocked out of alignment by cleaning.** Door line no longer where the
  config says it is. A reference-frame check catches this.
- **Model upgrade invalidating templates.** Embeddings are not comparable across
  models; without `model_ref` this is silent and catastrophic. With it, it is an
  error at startup.
- **Server timezone left at UTC.** Every pay-period boundary shifts six hours.
  Policy timezone comes from config, never from the system.
- **Demo footage reaching a payroll export.** Prevented by `is_virtual` and a
  test, because under demo pressure this is a genuinely plausible mistake.

---

## 6. What we are not defending against

Named so the boundary is explicit rather than assumed:

- Nation-state or sophisticated targeted attack on the box.
- Physical seizure of the server.
- A determined insider with admin access and intent — audit logs make this
  *detectable*, not impossible. That is the honest limit of the control.
- Network attacks beyond basic segmentation (cameras on their own VLAN,
  reachable only from the analysis box).

---

## 7. Monitoring that doubles as threat detection

The same handful of metrics catch gaming, degradation and misconfiguration. They
should exist from M3, not be added after the first incident:

- `unknown` face rate, **per door, per hour**. Rising = obstruction, lighting,
  drift, or covered faces.
- Unpaired event rate, per door, per day. Rising = tailgating or a missed exit
  route.
- Stream gap minutes, per camera, per day. Unexplained = tampering or hardware.
- Flagged-day percentage. High = the system is not measuring; fix capture, never
  the fail-open rule.
- Clip access counts, per user. Unusual = misuse.
- Review dismissal latency. Near-zero = rubber-stamping.

None of these can distinguish an attack from a fault on their own. They are
prompts to go and look at a clip, which is the point of building a system where
looking at a clip is always possible.
