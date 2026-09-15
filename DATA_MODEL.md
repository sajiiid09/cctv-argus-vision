# DATA_MODEL.md

Entities, relationships, and the door-event pairing state machine.

**All schema specifics here are PROVISIONAL.** The database is not chosen
(`DECISIONS.md`, ADR-0014). Field names and types are illustrative; the
*semantics* — especially §4 — are the part that must survive the choice.

---

## 1. Principles

1. **Raw events are evidence; everything else is derived.** Doorway events are
   append-only and never edited. Dwell intervals, overage minutes and occupancy
   summaries are computed from them and may be recomputed at any time. If a
   pairing bug is found, we fix the code and recompute; we never hand-edit an
   event to make the output right.
2. **Every payroll-affecting row points at a clip.** No clip reference, not
   exportable. See `ARCHITECTURE.md` §7.
3. **Ambiguity is a first-class state, not an error.** `unknown`, `unpaired` and
   `implausible` are values the schema must represent comfortably, because they
   will be common.
4. **UTC in storage, `Asia/Dhaka` at the edges.** Pay periods and the canteen
   allowance are local-day concepts; conversion happens in one place.
5. **Identity is scarce.** Only gate and canteen events carry a `person_id`.
   Occupancy and violence carry none, by design.

---

## 2. Entities

### Person / roster

```
person
  person_id        stable internal id
  employee_ref     factory HR id (their number, not ours)
  line_id          assigned production line          (nullable)
  seat_id          assigned seat                     (nullable)
  active_from/to   roster validity window
```

Roster churn in garments is high. `active_from/to` rather than a delete, because
a deduction computed last month must still resolve against who that person was
last month.

**OPEN:** how the roster arrives (manual CSV import vs. export from their HR
system vs. typed in). Affects onboarding effort more than architecture.

### Biometric template

```
face_template
  template_id
  person_id
  embedding        fixed-length vector
  model_ref        which model + version produced it
  enrolled_at, enrolled_by
  quality_score
```

`model_ref` is not optional bookkeeping. Embeddings are not comparable across
models or even across versions of the same model — a model upgrade invalidates
every template and requires re-enrolment or bulk re-embedding from stored
enrolment images. Storing `model_ref` is what makes that detectable instead of
silently catastrophic.

Enrolment images: whether we retain them (re-embedding on upgrade) or discard
them (smaller biometric footprint, re-enrolment required) is a privacy/operations
trade-off — **OPEN**, `PRIVACY_AND_COMPLIANCE.md`.

### Camera / stream

```
camera
  camera_id
  role             gate | canteen_door | floor
  source_uri       RTSP (real or virtual rig)
  space_id         the canteen space this camera's door opens into (doorway cameras)
  door_id          for doorway cameras
  direction_hint   which way "in" is, geometrically
  is_virtual       true for rig streams
```

`space_id` exists because pairing is per **canteen space**, not per door (§4): a
canteen with two doors is one space, and a dwell interval may start at one door
and end at the other.

`is_virtual` exists so that a payroll export can refuse to include anything
derived from a virtual camera. Demo footage must never be able to reach a wage.

### Doorway event (append-only — this is the evidence)

```
doorway_event
  event_id
  camera_id, door_id
  ts_utc                 authoritative (server clock)
  ts_camera_reported     recorded for audit, never used in arithmetic
  direction              enter | exit | ambiguous
  person_id              nullable — null means unknown
  match_confidence       nullable
  detection_quality
  clip_ref               → clip store
  ingest_run_id          which code+model version produced this
```

`ingest_run_id` is what makes an old event interpretable after the pipeline
changes. Without it, a disputed deduction from two months ago cannot be
reproduced.

### Stream health interval

```
stream_gap
  camera_id
  from_utc, to_utc
  cause         offline | decode_error | crash | clock_anomaly
```

First-class, because "we saw nothing" and "nothing happened" are different facts
and only this table distinguishes them. Any dwell interval overlapping a gap is
flagged and contributes zero overage.

### Derived: dwell interval

```
dwell_interval
  interval_id
  person_id
  space_id                        the canteen space (enter and exit may be different doors in it)
  enter_event_id, exit_event_id    either may be null
  start_utc, end_utc
  duration_s
  state                  see §4
  overage_s              0 unless state = resolved
  flags[]                review reasons
  computed_by            pairing-logic version
```

### Derived: payroll line (shadow by default)

```
payroll_line
  payroll_line_id
  person_id
  pay_period
  total_overage_s
  contributing_intervals[]
  reviewed_by, reviewed_at     nullable
  exported_at                  null while in shadow mode
  export_signature             hash over contents
```

Named `payroll_line_id`, not `line_id`: in this domain "line" already means a
production line (`person.line_id`), and a payroll schema with two meanings of
`line_id` is a confusion waiting for a tired reviewer.

`export_signature` exists so that the CSV a human keys into payroll can be proven
to match what the system computed, if it is ever questioned.

### Occupancy sample (anonymous, never payroll)

```
occupancy_sample
  seat_id | line_id
  ts_utc
  occupied          bool
  confidence
```

Deliberately no `person_id` and no foreign key that could acquire one. Whether we
even store per-seat rather than per-line aggregates is **OPEN** (ADR-0019): the
per-seat version is more useful to management and more re-identifiable, since
seats are assigned. If we keep per-seat, the access rules must treat it closer to
identified data than its schema suggests.

### Violence candidate

```
violence_candidate
  candidate_id
  camera_id, ts_utc
  trigger_score, classifier_score
  clip_ref
  review_state      pending | dismissed | escalated
  reviewed_by, reviewed_at, note
```

No `person_id`, ever. A clip goes to a human; the human decides who is in it.

### Gate attendance

```
gate_event
  badge_id → person_id
  ts_utc
  face_verified       true | false | no_face
  verify_confidence
  clip_ref
```

`no_face` is distinct from `false`. Not seeing a face is a camera problem; seeing
a different face is a buddy-punching signal. Collapsing them into a boolean
destroys the distinction that the whole gate pipeline exists to make.

---

## 3. Relationships

```
person 1─* face_template
person 1─* gate_event
person 1─* doorway_event        (nullable: unknown events have no person)
doorway_event *─1 camera *─1 space (doorway cameras, via space_id)
dwell_interval *─1 person, 0..1 enter_event, 0..1 exit_event
payroll_line 1─* dwell_interval
stream_gap *─1 camera           (overlaps intervals by time, not by key)
occupancy_sample *─1 seat       (no path to person)
violence_candidate *─1 camera   (no path to person)
```

The two "no path to person" lines are a schema-level expression of a `SOUL.md`
commitment. A future migration adding such a path is an ADR-level change, not a
refactor.

---

## 4. Door event pairing state machine

The payroll-affecting core. Every case has a defined resolution, and every
ambiguous case resolves to **zero overage plus a review flag**.

Scope of one evaluation: one `person_id`, one canteen **space** (`space_id` —
not one door; see the multiple-doors row below), one local day (`Asia/Dhaka`),
processed in `ts_utc` order.

### States

```
IDLE ──enter──▶ INSIDE ──exit──▶ RESOLVED (dwell computed)
  │                │
  │                └──enter (again)──▶ AMBIGUOUS
  └──exit (no prior enter)──▶ AMBIGUOUS
```

Terminal per-day: `RESOLVED`, `UNPAIRED_ENTER`, `UNPAIRED_EXIT`, `AMBIGUOUS`,
`IMPLAUSIBLE`, `GAP_AFFECTED`.

### Case table

| Case | Detection | Resolution | Overage | Flag | Audit |
|---|---|---|---|---|---|
| Clean enter→exit | Matched pair, plausible duration | `RESOLVED`, compute dwell | Dwell − allowance, floored at 0 | none | both clips |
| Enter→enter | Second enter while `INSIDE` | Keep first enter as the interval start candidate; mark `AMBIGUOUS`. Do **not** pick "the more likely" exit. | **0** | `double_enter` | both enter clips |
| Exit→exit | Exit while `IDLE` | `AMBIGUOUS` | **0** | `double_exit` | both exit clips |
| Unpaired enter | Enter, no exit before end of day/shift | `UNPAIRED_ENTER` | **0** | `missing_exit` | enter clip + door outage window |
| Unpaired exit | Exit, no prior enter | `UNPAIRED_EXIT` | **0** | `missing_enter` | exit clip |
| Implausible short | Dwell < floor (**PROVISIONAL**: 60 s) | `IMPLAUSIBLE` — likely a doorway loiter or a double detection, not a meal | **0** | `too_short` | both clips |
| Implausible long | Dwell > ceiling (**PROVISIONAL**: 3 h) | `IMPLAUSIBLE` — almost certainly a missed exit, not a three-hour lunch | **0** | `too_long` | both clips |
| Overlaps stream gap | Any `stream_gap` on that door intersects `[start,end]` | `GAP_AFFECTED` | **0** | `stream_gap` | gap record + clips |
| Clock anomaly | Exit before enter, or skew detected | `AMBIGUOUS` | **0** | `clock_anomaly` | both clips + skew measurement |
| Unknown face | `person_id` null | Not attributable to anyone; retained as evidence only | **0** (no person to charge) | `unknown_person` | clip |
| Low-confidence match | Above detection, below identity threshold | Treated as unknown. Never a best guess. | **0** | `low_confidence` | clip |
| Multiple doors | Enter at door A, exit at door B | `RESOLVED` if the canteen is one space with multiple doors — pairing is per *space*, not per door | normal | none | both clips |
| Crossing midnight / shift | Interval spans the local-day boundary | Attribute to the local day of the **enter** event; flag for review | computed | `spans_boundary` | both clips |
| Duplicate event | Same person, same door, same direction, within (**PROVISIONAL**) 3 s | Deduplicate to one event; keep both rows, mark one `superseded` | normal | `deduplicated` | both clips |

Two design notes that are easy to get wrong:

- **Pairing is per canteen space, not per door.** If a canteen has two doors,
  entering by one and leaving by the other is normal behaviour, and a per-door
  state machine would flag half the factory every day. Doors carry a `space_id`.
- **Never infer a missing event from the other one.** The tempting move — "they
  entered at 13:00, everyone leaves by 14:00, assume an exit at 14:00" — is
  precisely the punitive default `SOUL.md` forbids. `UNPAIRED_ENTER` stays
  unpaired.

### Multiple intervals in a day

A person may enter the canteen more than once. Overage is computed on **total
dwell across the local day**, not per visit — the allowance is one hour of
canteen time, not one hour per visit. If **any** interval in the day is in a
non-`RESOLVED` state, the whole day is flagged and **contributes zero** rather
than being partially charged. Partial charging on a day with a known measurement
failure is exactly the kind of plausible-looking wrong number this project
exists to avoid.

**PROVISIONAL:** this is the strictest reading of fail-open and it means one bad
event forgives a whole day. The alternative — charge the clean intervals, flag
the rest — is defensible and cheaper for the factory. Decide before shadow mode
ends (ADR-0023).

### Recomputation

Pairing runs over stored events and is a pure function of
`(events, gaps, policy, code version)`. Rerunning it must be safe and must
produce a new derived row with a new `computed_by`, never an in-place edit. Every
payroll line therefore names the logic version that produced it, and a disputed
line from the past can be reproduced exactly.

---

## 5. Retention (summary)

Full policy, and the open legal questions, in `PRIVACY_AND_COMPLIANCE.md`.

| Data | Provisional retention | Why |
|---|---|---|
| Continuous recording | Days | Storage-bound; not evidence unless promoted |
| Clips referenced by a payroll line | At least one full dispute window past the pay period | A deduction must remain auditable while it can still be challenged |
| Clips for flagged/unpaired events | Same as above | These are the ones people argue about |
| Violence candidate clips | Until reviewed + a short window | Human-reviewed, then resolved |
| Doorway events | Long | Small, and they are the evidence |
| Face templates | While roster-active, plus a short tail | Biometric data: shortest defensible life |
| Occupancy samples | Short, aggregated sooner | Management indicator with no evidentiary role |

The retention rule that matters most: **a clip referenced by a payroll line
cannot be deleted by a retention job.** Retention must consult references, not
just age.
