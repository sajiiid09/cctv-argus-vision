# ARCHITECTURE.md

System shape for **Sparrow Vision**. Absorbs the former `DATA_MODEL.md` and
`FOOTAGE.md`. Depth on individual choices lives in `DECISIONS.md`.

**Status 2026-09-17.** Ingest, the event store, the backend registry and the
pairing state machine are built and tested on Linux/CPU. The pipelines that
connect them are not. Anything marked **OPEN** or **PROVISIONAL** is a real
question, not a formality.

---

## 1. What the system does

Four independent pipelines that share infrastructure and almost nothing else.

| Pipeline | Input | Identity? | Output | Touches pay? |
|---|---|---|---|---|
| Gate attendance | Badge/RFID tap + gate camera | Yes — verify badge holder | Attendance record, mismatch flag | Indirectly |
| Canteen dwell | Canteen door cameras only | Yes — face is the only identifier | Dwell interval, overage minutes | **Yes** |
| Workstation occupancy | Floor cameras | **No** — seat coordinates only | Per-seat occupied/empty | **Never** |
| Violence detection | Floor cameras | No | Clip in a human review queue | No |

That table is the architecture in miniature. The pipelines are separate because
their failure consequences differ, and mixing them would let a failure in a
harmless pipeline become a payroll error.

## 2. Identity is resolved at doorways only (ADR-0002)

Faces are matched at the gate and at canteen doors. Nowhere else. No floor-wide
tracking, no cross-camera re-identification.

**Why.** Garments floors have assigned seats, so a seat *is* an identity lookup
via the roster with no computer vision involved — re-ID would buy information we
already have from a spreadsheet. Cross-camera re-ID is also the most
failure-prone thing in this domain: it degrades badly with similar clothing (a
factory where most people wear similar clothing), overhead angles and occlusion,
and its errors are silent and compounding. And it is the capability that turns
this from a measurement system into a surveillance system. Without it, "where was
this person between 10:00 and 11:00" is a question the system cannot answer. That
boundary is worth more than the capability.

**Consequences we accept.** We cannot attribute an empty seat to a reason — we
know seat 47 was empty for 18 minutes, not who was out of it or why. Canteen
dwell depends entirely on doorway capture quality, with no fallback if a face is
missed; that concentrates the accuracy risk at a small number of camera
positions, which is also an advantage, because it is a small number of positions
to get right. Someone leaving by a non-camera exit produces an unpaired enter and
fail-open handles it; finding all such exits is a site-survey task.

**What would change this.** Evidence that assigned seating is not enforced, or a
compliance requirement for zone-level headcount that doorway counting cannot
satisfy. Neither is established.

## 3. Component shape

```
   RTSP sources                    ingest                 analysis                state                surfaces
 ┌───────────────┐          ┌────────────────┐      ┌────────────────┐     ┌─────────────┐      ┌──────────────┐
 │ Imou (2 lens) │          │                │      │ gate verify    │     │             │      │ review queue │
 │ Dahua via DVR │──RTSP──▶ │ stream manager │─────▶│ canteen door   │────▶│  event      │─────▶│ dashboards   │
 │ virtual rig   │          │ (demux, fan)   │      │ occupancy      │     │  store +    │      │ payroll      │
 │ (mediamtx)    │          │                │      │ violence       │     │  clip store │      │   export     │
 └───────────────┘          └────────────────┘      └────────────────┘     └─────────────┘      └──────────────┘
                                                            │                    ▲
                                                            └── clip writer ─────┘
```

**Stream manager.** Owns RTSP connections, reconnection, and fan-out. One
connection per stream, many consumers — duplicating decode is the classic way to
run out of GPU on a system that should have had room, and the DVR's low
concurrent-client limit makes a second connection an actual hazard. It keeps a
per-stream ring of recent **encoded packets** so a clip can include the seconds
*before* a trigger; without a pre-trigger buffer, violence clips start after the
interesting part. Decoded frames are a separate, short, sampled tap for the
pipelines (ADR-0031).

**Analysis workers.** One process family per pipeline. Separate processes rather
than one loop, because latency requirements differ by orders of magnitude — a
door event is near-real-time, occupancy can be sampled every few seconds — and
because a crash in violence detection must not stop canteen pairing.

**Event store.** Append-only doorway events plus derived, recomputable
aggregates. Raw events are evidence and are never edited; dwell, overage and
occupancy summaries are derived and can be recomputed when a pairing bug is fixed.

**Clip store.** Video segments addressed by camera + time range, with retention
tiers. Every payroll-affecting record points into it. Clips are produced by
remuxing buffered packets, so they are bit-identical to what the camera sent.

**Payroll boundary — decided.** We never write to a payroll system. We produce a
reviewed, signed export and a human enters it. This keeps the irreversible action
in human hands, makes shadow mode a one-line difference, and avoids integrating
with software we have not seen.

## 4. The canteen path (the one that touches pay)

1. Canteen door camera → stream manager → person detection on the door region.
2. A short single-camera track across the door line gives `enter` or `exit` from
   the sign of the crossing. Not re-ID: the track lives for a second or two and
   its id is never stored.
3. Best face crop from that track → align → embed → match 1:N against enrolled
   templates. Below threshold, or a near-tie between two identities, produces an
   `unknown` event: evidence, but not identity. Never a best guess.
4. Append a doorway event with a clip reference.
5. The pairing state machine (§7) turns events into dwell intervals or review
   flags. Every ambiguous case resolves to **zero deduction plus a flag**.
6. Overage is dwell beyond the allowance, per local day, across all visits.
7. Shadow mode: computed and reported. Export only when the flag is on, and there
   is currently no export to enable.

Steps 5–7 are the code that can cost someone money. They are pure functions over
stored events — deterministic, replayable, testable without video. Keeping
payroll logic free of vision code is deliberate: vision is probabilistic and hard
to test; arithmetic about someone's wage should be neither.

---

## 5. Cross-platform strategy

Dev is macOS/arm64 with an Apple GPU; production is Ubuntu/x86_64 with NVIDIA;
Docker on macOS cannot reach the GPU at all. The same tree must run on both and
the differences must not leak into application code.

### 5.1 Backend abstraction

Narrow interfaces with pluggable backends: `Detector`, `PoseEstimator`,
`FaceDetector`, `FaceEmbedder`, `ClipClassifier`.

1. **Application code never imports a backend.** It asks a registry. If
   `import onnxruntime` or `import torch` appears outside a backend module, the
   abstraction is already broken — enforced by a structural test.
2. **Selection is by capability probe, never a platform check.**
   `if platform == "darwin"` hardcodes a false equivalence; an arm64 Linux box, a
   Mac with no GPU budget and a CI runner with neither all break it.
3. **Backend choice is loggable and overridable.** Every run records which
   backend and model hash it used, because "does this reproduce on CPU?" is the
   first debugging question and must not require a code change.
4. **Interfaces stay narrow.** No backend-specific options in signatures.

This discipline is load-bearing twice: it keeps the platforms interchangeable,
and since ADR-0030 it is also what keeps a licence-restricted model swappable.

### 5.2 Model format

**ONNX is the single interchange format**, with platform acceleration applied at
deploy time, and every artefact referenced by sha256 (ADR-0026). One artefact
serves dev and prod, and a hashed artefact is what makes parity meaningful —
comparing two *different* model files tells you nothing.

Honest caveat: ONNX export is where models with dynamic control flow go to die,
and the failure is at export time, loudly, which is the good kind. Some
commercial face SDKs expose no ONNX path at all, which would make parity testing
for embeddings an integration test against a fixed SDK version rather than a
numeric comparison.

**TensorRT engines are not portable** — they are tied to GPU architecture,
TensorRT version and driver. Leaning (ADR-0021) is to build on first run with a
disk cache keyed by (model hash, GPU, TRT version, driver): we do not own the
production GPU, so we cannot build engines for hardware we have not chosen, and a
cold start on a box that runs for months is cheap. Worth keeping in view: skip
TensorRT entirely and run the CUDA EP if measured throughput suffices. It is an
optimisation, not a requirement, and it costs build complexity and one more
source of divergence.

### 5.3 Numerical parity

Different backends will not produce byte-identical output. Some divergence is
normal (fp16 vs fp32, fused kernels, different NMS); some means the pipeline is
broken. The golden-frame suite tells them apart automatically rather than by eye:
committed frames with reference outputs from a designated reference backend (ONNX
Runtime CPU fp32 — available everywhere including CI, and numerically the most
boring).

Tolerances, **PROVISIONAL**, and to be re-measured whenever the models change
rather than carried over:

| Quantity | Tolerance | Why this one |
|---|---|---|
| Detection box IoU vs reference | ≥ 0.95 per matched box | Sub-pixel drift is harmless; a box that moved enough to change a door-line crossing is not |
| Detection confidence | ±0.05 absolute | Larger drift can flip a threshold decision |
| Detection set match | no missing/extra box above threshold | A disappeared detection is a missed event — the failure that matters |
| Face embedding cosine distance | ≤ 0.02 | Identity thresholds live around 0.3–0.4; divergence an order of magnitude below that is safe, at the same magnitude is not |
| Face verification decision | identical accept/reject on every pair | The decision, not the number, is what reaches payroll |
| Pose keypoints | ≤ 2 px at 1080p per visible keypoint | Pose only gates a cheap trigger |
| Clip classifier scores | ±0.10, identical top-1 | Output goes to a human queue, so ranking matters more than calibration |

Select a non-default leg with `ARGUS_PARITY_BACKEND`. The CUDA leg and the macOS
CoreML leg (ADR-0022, **OPEN**) are both unrun.

### 5.4 What runs in containers

**macOS:** inference runs natively; Postgres, mediamtx and other stateless
infrastructure run in containers. **Linux:** everything can be containerised via
the NVIDIA Container Toolkit, and should be, because that is what we operate.

The resulting dev/prod drift is not small — CoreML vs CUDA, host vs container
networking, case-insensitive vs case-sensitive filesystem — and Apple has made
eliminating it impossible. So it is *bounded* instead: identical entrypoints in
both places, one configuration mechanism, path/timezone/filesystem behaviour
covered by tests that run on Linux CI, and the Linux box as the arbiter.

### 5.5 Architecture-specific landmines

- **ONNX Runtime variants.** `onnxruntime`, `onnxruntime-gpu` and CoreML support
  are different wheels that unpack into the same directory. Exactly one per
  environment; see `AGENTS.md` §4.
- **CUDA-only packages** do not install on macOS at all. Optional extras, never
  base requirements.
- **PyTorch** builds differ (MPS vs CUDA). Keeping runtime inference on ONNX
  Runtime keeps Torch out of the deployed path entirely.
- **ffmpeg** builds differ in hardware acceleration: VideoToolbox on macOS,
  NVDEC/VAAPI on Linux, and a pip-installed build generally has neither. If we
  depend on hardware decode we depend on a specific build, and that must be
  explicit rather than "whatever is on PATH". The packet ring removes hardware
  decode from the critical path, because nothing decodes the evidence stream
  continuously any more.
- **Face SDKs**, if commercial, frequently ship Linux x86_64 binaries only.

### 5.6 Environments

Three machines in practice. A **macOS dev** box proves the CoreML leg. A
**GPU-less Linux** box runs the same tree on the CPU reference backend — which is
exactly the reference leg of the parity suite, useful rather than a compromise —
but proves neither CoreML nor CUDA. The **NVIDIA box** proves the system: an
RTX-class GPU with ≥ 12 GB VRAM is enough to prove it; production sizing for the
real camera count remains **OPEN** (ADR-0020).

Code must be proven on the NVIDIA box **as soon as the first pipeline produces an
event end to end** — not after the demo. Every problem in §5.4 and §5.5 surfaces
only there, and every one is discovered late if you wait. Late means during demo
preparation, in a week with no slack. A box exercised early turns each into an
hour's annoyance; the same box exercised at the end turns them into the reason
the demo slips.

A rented cloud GPU is an acceptable substitute for everything except final
performance numbers — but site footage does not go on it (`RISKS.md` §2).

### 5.7 Mundane footguns

- **Case-insensitive APFS.** `models/Face.onnx` and `models/face.onnx` are one
  file on a Mac and two on the server. All repo paths lowercase, enforced on CI.
- **Timezone.** `Asia/Dhaka`, UTC+06:00, **no daylight saving**. Store UTC,
  convert at the edges, and take the policy timezone from config — a server left
  on UTC would silently shift every pay-period boundary by six hours.
- **Clocks.** Camera, DVR and server clocks drift. Payroll timestamps come from
  one NTP-disciplined authority, with camera-reported time recorded for audit and
  never used in arithmetic. Two cameras disagreeing is how an exit precedes its
  own enter.
- **Line endings and locale.** LF via `.gitattributes`; never format numbers or
  parse dates with the ambient locale.
- **File watchers.** FSEvents and inotify differ, and inotify limits bite in
  containers. Do not depend on file watching in the runtime path.
- **`/dev/shm`.** Docker defaults to 64 MB; anything passing frames through
  shared memory fails confusingly. Sized explicitly in the compose files.
- **GPU memory.** Four pipelines plus decode on one GPU is a budget, not an
  assumption, and must be measured before a camera count is promised.

---

## 6. The virtual camera rig

**Not a test fixture. Infrastructure.** It exists because no cameras existed, and
it survives as the CI video source and the demo-day fallback.

Recorded and synthetic files, looped and served over RTSP by mediamtx, so that at
the pipeline boundary a virtual camera is indistinguishable from a real one: same
protocol, same auth, same reconnect behaviour, same decode path. **Nothing
downstream of the stream manager may know whether a stream is virtual.**

Requirements: deterministic playback, so a golden-frame test is reproducible;
wall-clock mapping, so a test can say "this clip starts at 13:58 local" and
exercise pay-period boundaries; **fault injection** — drop, stall, corrupt,
change resolution mid-stream, add latency — which is the rig's most valuable
feature and the one most likely to be skipped under time pressure; and multiple
simultaneous streams for load testing.

One operational detail learned the hard way: rig footage is encoded with a
one-second GOP, because libx264's default of 250 frames left a five-second
capture containing **zero keyframes**, and clip extraction is keyframe-aligned.
Real cameras get the same setting.

**Target real sources.** 2× Imou dual-lens WiFi PTZ, each exposing a fixed
~2304×1296 lens and a pan-tilt ~2880×1620 lens. RTSP auth uses the **safety code
printed on the device**, not the app password — a detail that costs an afternoon
if undocumented. WiFi means instability is normal, not exceptional, and a pan-tilt
lens on a doorway needs a fixed preset plus a drift check (ADR-0029). Plus 3–4
Dahua analog cameras behind a DVR with a **low concurrent-client limit**; that
limit is an architectural constraint, and a second process opening its own
connection can starve the first, which is also a denial-of-evidence risk.

---

## 7. Data model

### 7.1 Principles

1. **Raw events are evidence; everything else is derived.** Doorway events are
   append-only and never edited. If a pairing bug is found we fix the code and
   recompute; we never hand-edit an event to make the output right.
2. **Every payroll-affecting row points at a clip.** No clip, not exportable.
3. **Ambiguity is a first-class state, not an error.** `unknown`, `unpaired` and
   `implausible` are values the schema represents comfortably, because they will
   be common.
4. **UTC in storage, `Asia/Dhaka` at the edges**, converted in one place.
5. **Identity is scarce.** Only gate and canteen events carry a `person_id`.
   Occupancy and violence carry none, by design.

### 7.2 Entities

**`person`** — `person_id`, `employee_ref` (their HR number, not ours), `line_id`,
`seat_id`, `active_from`/`active_to`. Roster churn is high, so validity windows
rather than deletes: a deduction computed last month must still resolve against
who that person was last month. **OPEN:** how the roster arrives.

**`face_template`** — `template_id`, `person_id`, `embedding`, **`model_ref`**,
`enrolled_at`, `enrolled_by`, `quality_score`, `consent_id`. `model_ref` is not
bookkeeping: embeddings are not comparable across models or even versions, so a
model upgrade invalidates every template. Storing it makes that an error at
startup instead of silently catastrophic.

**`camera`** — `camera_id`, `role` (gate | canteen_door | floor), `source_uri`,
`space_id`, `door_id`, `direction_hint`, `is_virtual`. `space_id` exists because
pairing is per canteen **space**, not per door: a canteen with two doors is one
space and an interval may start at one and end at the other. `is_virtual` exists
so an export can refuse anything derived from a virtual camera — demo footage
must never reach a wage.

**`doorway_event`** (append-only — this is the evidence) — `event_id`,
`camera_id`, `door_id`, `ts_utc` (authoritative, server clock),
`ts_camera_reported` (audit only, never arithmetic), `direction`
(enter | exit | ambiguous), `person_id` (null means unknown), `match_confidence`,
`detection_quality`, `clip_ref`, `ingest_run_id`, `duplicate_of`.
`ingest_run_id` is what makes an old event interpretable after the pipeline
changes; without it a disputed deduction from two months ago cannot be reproduced.

**`stream_gap`** — `camera_id`, `from_utc`, `to_utc`, `cause`
(offline | decode_error | crash | clock_anomaly | stall | refused | aim_changed).
First-class, because "we saw nothing" and "nothing happened" are different facts
and only this table distinguishes them.

**`gate_tap`** and **`gate_event`** — the tap records `reader_id`, `badge_id`,
`ts_utc` (server clock at receipt), `ts_reader_reported` (audit only) and
`source` (zkt_live | zkt_replay | simulated). The event references its tap and
records `face_verified` ∈ `true | false | no_face | not_attempted`. The tap is
written *before* verification is attempted, so a crash loses a verification
result and never the attendance evidence.

`no_face` is distinct from `false`: not seeing a face is a camera problem, seeing
a different face is a buddy-punching signal, and collapsing them destroys the
distinction the gate pipeline exists to make. `not_attempted` is distinct from
both — a tap recovered from the reader's log after an outage was never compared
against anything, and recording it as `no_face` would claim we looked.

**Derived:** `dwell_interval`, `dwell_day`, `payroll_line`, each carrying the
logic version and policy that produced it, recomputed rather than edited.
**`occupancy_sample`** carries no `person_id` and no foreign key that could
acquire one. **`violence_candidate`** carries none either, ever: a clip goes to a
human and the human decides who is in it.

### 7.3 The pairing state machine

The payroll-affecting core. Scope of one evaluation: one `person_id`, one canteen
**space**, processed in `ts_utc` order.

```
IDLE ──enter──▶ INSIDE ──exit──▶ RESOLVED (dwell computed)
  │                │
  │                └──enter (again)──▶ AMBIGUOUS
  └──exit (no prior enter)──▶ UNPAIRED_EXIT
```

Terminal states: `RESOLVED`, `UNPAIRED_ENTER`, `UNPAIRED_EXIT`, `AMBIGUOUS`,
`IMPLAUSIBLE`, `GAP_AFFECTED`.

**Case table.** Every row has a branch in `argus.payroll.pairing` and a test named
after it in `tests/payroll/test_case_table.py`. Adding a row here means adding a
test in the same commit.

| Case | Detection | Resolution | Overage | Flag |
|---|---|---|---|---|
| Clean enter→exit | Matched pair, plausible duration | `RESOLVED` | Dwell − allowance, floored at 0 | none |
| Enter→enter | Second enter while `INSIDE` | Keep the first enter as the start candidate; `AMBIGUOUS`. Do **not** pick "the more likely" exit | **0** | `double_enter` |
| Exit→exit | Exit while `IDLE`, preceded by an exit | `AMBIGUOUS` | **0** | `double_exit` |
| Unpaired enter | Enter, no exit before the plausibility ceiling | `UNPAIRED_ENTER` | **0** | `missing_exit` |
| Unpaired exit | Exit, no prior enter | `UNPAIRED_EXIT` | **0** | `missing_enter` |
| Implausible short | Dwell < floor (**PROVISIONAL** 60 s) | `IMPLAUSIBLE` — a doorway loiter or a double detection, not a meal | **0** | `too_short` |
| Implausible long | Dwell > ceiling (**PROVISIONAL** 3 h) | `IMPLAUSIBLE` — almost certainly a missed exit | **0** | `too_long` |
| Overlaps stream gap | Any gap in that space intersects the interval | `GAP_AFFECTED` | **0** | `stream_gap` |
| Clock anomaly | Exit before enter, or camera-reported time disagreeing beyond tolerance | `AMBIGUOUS` | **0** | `clock_anomaly` |
| Unknown face | `person_id` null | Not attributable; retained as evidence only | **0** | `unknown_person` |
| Low-confidence match | Above detection, below the identity threshold | Treated as unknown. Never a best guess | **0** | `low_confidence` |
| Multiple doors | Enter at door A, exit at door B | `RESOLVED` — pairing is per space | normal | none |
| Crossing midnight | Interval spans the local-day boundary | Attributed to the local day of the **enter** | computed | `spans_boundary` |
| Duplicate event | Same person, door and direction within (**PROVISIONAL**) 3 s | Deduplicate to the earliest; the later row carries `duplicate_of` → the earlier | normal | `deduplicated` |
| Missing clip | `RESOLVED` interval whose enter or exit has no `clip_ref` | Interval stays `RESOLVED`; the **day** is flagged | **0** for the day | `missing_clip` |

Four things that are easy to get wrong:

- **Pairing is per canteen space, not per door.** Entering by one door and
  leaving by another is normal; a per-door machine would flag half the workforce
  every day.
- **Never infer a missing event from the other one.** "They entered at 13:00,
  everyone leaves by 14:00, assume an exit" is exactly the punitive default
  `SOUL.md` forbids. `UNPAIRED_ENTER` stays unpaired.
- **Deduplication points backwards, and the earliest wins.** The append-only
  trigger rejects every `UPDATE`, so the earlier row cannot be marked after the
  fact; the later row carries `duplicate_of` and pairing ignores it. The first
  detection is kept because it is the better estimate of when the crossing
  happened. The field is not called `supersedes`, because the row it points at is
  the one that survives.
- **No clip, no deduction** (ADR-0008). The interval still resolved — the
  measurement happened — but a day containing an unauditable interval contributes
  zero. Enforcing this only at export would mean enforcing it nowhere, because in
  shadow mode there is no export.

**Multiple intervals in a day.** The allowance is one hour of canteen time per
day, not per visit, so overage is computed on the day's total. If **any** interval
in the day is non-`RESOLVED`, the whole day is flagged and contributes zero
(ADR-0023). That is the strictest reading of fail-open: one bad event forgives a
whole day, deliberately, because partial charging on a day with a known
measurement failure is exactly the plausible-looking wrong number this project
exists to avoid.

**Recomputation.** Pairing is a pure function of (events, gaps, policy, logic
version). Rerunning it is safe and produces a new derived row with a new run id,
never an in-place edit — so a disputed line from the past can be reproduced
exactly.

---

## 8. Failure modes and boundaries

The governing rule: **degrade toward no deduction and a visible flag.** Never
toward a guess.

| Failure | Behaviour | Payroll effect |
|---|---|---|
| Camera offline | Outage window recorded | Events in the window unpaired → zero, flagged |
| Face not detected at the door | `unknown` event, clip retained | Unpaired → zero, flagged |
| Face below threshold, or a near-tie | `unknown`, never a best guess | Zero, flagged |
| Camera aim drifted | Pipeline stops emitting; gap opened with `aim_changed` | Zero, flagged |
| GPU OOM / inference crash | Pipeline restarts; gap recorded explicitly | Gap window fails open |
| Clock skew detected | Affected window flagged | Zero, flagged |
| Event store unavailable | Ingest buffers or drops with a recorded gap, never silently continues | Gap window fails open |
| Clip missing for a record | Day flagged; record not exportable | Blocked, flagged |
| Violence model unavailable | No queue items; no alternative path | None |

Two invariants fall out and belong in code as assertions, not comments:

1. **No clip, no deduction.**
2. **No gap, no deduction.** A stream outage or clock anomaly overlapping an
   interval flags it and contributes zero.

**Data boundaries** (detail in `RISKS.md`): biometric templates and clips never
leave the site; cameras sit on their own VLAN reachable only from the analysis
box; reads are audited, not just writes; and the shadow-mode flag is not ordinary
config — flipping it is a logged, human-authorised action.

---

## 9. Footage

### 9.1 What each pipeline needs

| Pipeline | Needed | Minimum viable |
|---|---|---|
| Ingest / rig | Several streams, long enough to loop without visible seams | Any stock or self-shot footage, 4+ files, ≥ 10 min each |
| Canteen doorway | People crossing a threshold toward and away from a fixed camera, faces visible, including two-at-once and partially occluded crossings | A doorway, a phone on a tripod, a handful of consenting people |
| Face identity | Enrolment images plus crossing footage for the same small set of people | Ourselves and consenting volunteers, written consent, deleted after |
| Gate verification | A person presenting at close range, plus mismatched pairs (A's badge, B's face) | Same volunteers, staged mismatches |
| Occupancy | A static wide view of seated people, with people leaving and returning | Any office or workshop wide shot |
| Violence | Pushing, grabbing, sudden aggressive motion from an overhead-ish angle — **plus far more non-violent fast motion** (bundle handling, hurrying, horseplay) | Staged, plus a large negative set |
| Load testing | The real camera count at real resolution | Duplicated streams at 2304×1296 and 2880×1620 |

Public violence datasets are access-restricted, research-only, and mostly
hand-held or movie footage rather than overhead CCTV — a classifier trained on
them reports confident nonsense on our angle. That is why ADR-0012 is trigger-only.

Footage of real people needs written consent naming purpose, retention and
viewers; it is never committed, never put on a cloud box, and deleted when the
milestone closes — **along with the templates derived from it**, because removing
the video and leaving the embeddings behind is the easy mistake.

### 9.2 What cannot be validated without real cameras

State this list unprompted, especially at a demo. It is the difference between a
working demo and a working system.

1. **Motion blur under uncontrolled auto-exposure.** Doorways swing between
   bright daylight and dim interior; shutters slow and walking faces smear.
   Footage shot in good light will not show this, and it is the most likely cause
   of a lower-than-expected capture rate.
2. **Walk-through capture geometry.** Whether a camera at a given height and
   angle captures a usable face from someone walking briskly through. A physical
   question, answered by mounting a camera, not by code.
3. **Backlight at doorways.** Usually the brightest thing in frame with a person
   silhouetted against it. The worst case in the building, hard to stage.
4. **Night and low-light behaviour.** IR mode changes appearance enough to break
   face matching.
5. **WiFi stream instability at scale.** Reconnect behaviour under *real*
   instability differs from injected faults, which are always cleaner.
6. **DVR concurrent-client behaviour under sustained load.** Documented limits
   and actual limits differ, and the failure is a refused connection at the worst
   moment.
7. **Real crowd density.** 500+ people through canteen doors in minutes.
   Occlusion, grouping and tailgating at that density cannot be staged with six
   volunteers.
8. **Appearance variation across the actual workforce.** A six-person volunteer
   set says nothing about this, and it is the fairness question that matters most.
9. **Long-run drift.** Dust, knocks from cleaning, seasonal light.
10. **Violence false-positive rate on real floor activity.** Almost certainly the
    dominant error source, and unmeasurable before a site pilot.

**Every accuracy number produced before a site pilot describes the rig.** Saying
so out loud costs a sentence and prevents a much worse conversation later.
