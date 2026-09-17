# ARCHITECTURE.md

System shape for **Sparrow Vision**. Depth lives here and in `DECISIONS.md`.

Status: **no code exists**. This describes the shape we intend to build and the
reasoning behind the parts that are settled. Anything marked **OPEN** is a real
question, not a formality — see `DECISIONS.md` for the candidate list.

---

## 1. What the system does

Four independent pipelines that share infrastructure and almost nothing else.

| Pipeline | Input | Identity? | Output | Touches pay? |
|---|---|---|---|---|
| Gate attendance | Badge/RFID tap + gate camera | Yes — verify badge holder | Attendance record, mismatch flag | Indirectly (attendance already does) |
| Canteen dwell | Canteen door cameras only | Yes — face is the only identifier | Dwell interval, overage minutes | **Yes** |
| Workstation occupancy | Floor cameras | **No** — seat coordinates only | Per-seat occupied/empty over time | **Never** |
| Violence detection | Floor cameras | No | Clip in a human review queue | No |

The table is the architecture in miniature. The pipelines are separate because
their failure consequences are different, and mixing them would let a failure in
a harmless pipeline become a payroll error. See `SOUL.md`.

---

## 2. Decided: identity is resolved at doorways only

**Decision.** Faces are matched to identities at the gate and at canteen doors.
Nowhere else. No floor-wide person tracking, no cross-camera re-identification.

**Reasoning.**

- Garments floors have assigned seats. A seat *is* an identity lookup, via the
  roster, with no computer vision involved. Re-ID would buy us information we
  already have from a spreadsheet.
- Cross-camera re-ID is the single most failure-prone thing in this domain:
  it degrades badly with similar clothing (a factory where most people wear
  similar clothing), overhead angles, and occlusion. Its errors are silent and
  compounding.
- It is also the capability that turns this from a measurement system into a
  surveillance system. Without it, "where was this person between 10:00 and
  11:00" is a question the system cannot answer. That boundary is worth more to
  us than the capability.

**Consequences we accept.**

- We cannot attribute an empty seat to a reason. We know seat 47 was empty for 18
  minutes; we do not know who was out of it or why. Occupancy stays a
  line-level-ish indicator, and per-operator attribution is deliberately weak.
  Whether we report occupancy per-operator at all is **OPEN** (see `DECISIONS.md`).
- Canteen dwell depends entirely on doorway capture quality. There is no fallback
  identification if a face is missed at the door — the event is unpaired, and
  unpaired means zero deduction. This concentrates all our accuracy risk at a
  small number of camera positions, which is also an advantage: it is a small
  number of positions to get right.
- If someone leaves the canteen through a non-camera exit, we see an unpaired
  enter. Fail open handles it. Finding all such exits is a site-survey task, not
  a software task, and it is on the hardware-blocked list in `PLAN.md`.

**What would change this.** Evidence that assigned seating is not actually
enforced on this floor, or a compliance requirement for zone-level headcount that
doorway counting cannot satisfy. Neither is established.

---

## 3. Component shape

Nothing below is built. Boundaries, not implementations.

```
   RTSP sources                    ingest                 analysis                state                surfaces
 ┌───────────────┐          ┌────────────────┐      ┌────────────────┐     ┌─────────────┐      ┌──────────────┐
 │ Imou (2 lens) │          │                │      │ gate verify    │     │             │      │ review queue │
 │ Dahua via DVR │──RTSP──▶ │ stream manager │─────▶│ canteen door   │────▶│  event      │─────▶│ dashboards   │
 │ virtual rig   │          │ (decode, fan)  │      │ occupancy      │     │  store +    │      │ payroll      │
 │ (mediamtx)    │          │                │      │ violence       │     │  clip store │      │   export     │
 └───────────────┘          └────────────────┘      └────────────────┘     └─────────────┘      └──────────────┘
                                                            │                    ▲
                                                            └── clip writer ─────┘
```

**Stream manager.** Owns RTSP connections, decode, reconnect, and frame fan-out
to analysis consumers. One decode per stream, many consumers — decode is the
expensive part and duplicating it is the classic way to run out of GPU on a
system that should have had room. Also owns a ring buffer of recent frames per
stream so that a clip can include the seconds *before* a trigger. Without a
pre-trigger buffer, violence clips start after the interesting part.

**Analysis workers.** One process family per pipeline, subscribing to the streams
they need. Separate processes rather than one big loop, because their latency
requirements differ by orders of magnitude (a door event must be handled in near
real time; occupancy can be sampled every few seconds) and because a crash in
violence detection must not stop canteen pairing.

**Event store.** Append-only doorway events plus derived, recomputable
aggregates. The distinction matters: raw events are evidence and are never
edited; dwell intervals, overage and occupancy summaries are *derived* and can be
recomputed when a pairing bug is fixed. See `DATA_MODEL.md`.

**Clip store.** Video segments addressed by camera + time range, with retention
tiers. Every payroll-affecting record points into it.

**Surfaces.** A review queue (unpaired events, implausible dwell, gate mismatch,
violence candidates), management dashboards, and a per-period payroll export.

**Payroll boundary — decided.** We never write to a payroll system. We produce a
reviewed, signed export; a human enters it. This keeps the irreversible action in
human hands, keeps shadow mode a one-line difference (produce the export, do not
hand it over), and avoids an integration with software we have not seen.

---

## 4. Data flow, canteen (the payroll-affecting path)

1. Camera at canteen door → stream manager → face detection on the door region.
2. Detected face → embedding → match against enrolled templates above a
   threshold. Below threshold: an `unknown` doorway event, which is evidence but
   not identity.
3. Direction of travel (enter or exit) from the track across the door line. This
   is a short, single-camera track over a second or two — not re-ID.
4. Append doorway event: `(camera, timestamp, direction, person_id|unknown,
   confidence, clip_ref)`.
5. Pairing state machine turns events into dwell intervals, or into review flags.
   Every ambiguous case resolves to **zero deduction + flag**. Full case table in
   `DATA_MODEL.md`.
6. Overage = dwell beyond the 1-hour allowance, per policy window.
7. Shadow mode: computed and reported. Export only when the flag is on.

Steps 5–7 are the code that can cost someone money. They are pure functions over
stored events wherever possible — deterministic, replayable, unit-testable
without video. Keeping payroll logic free of vision code is deliberate: vision is
probabilistic and hard to test, arithmetic about someone's wage should be neither.

---

## 5. Cross-platform strategy

This is the hard part. Dev is macOS/arm64 with Apple GPU; prod is Ubuntu/x86_64
with an NVIDIA GPU; Docker on macOS cannot reach the GPU at all. The same source
tree must run on both, and the differences must not leak into application code.

### 5.1 Backend abstraction

Four narrow interfaces, each with pluggable backends:

- `Detector` — frames in, boxes + classes + scores out.
- `PoseEstimator` — frames (or crops) in, keypoints out.
- `FaceEmbedder` — aligned face crops in, fixed-length normalised vectors out.
- `ClipClassifier` — a short clip in, class scores out.

Rules, and they are not stylistic:

1. **Application code never imports a backend.** It asks a registry for a
   `Detector`. If `import onnxruntime` or `import torch` appears outside a
   backend module, the abstraction is already broken.
2. **Selection is by capability detection at runtime, not by platform check.**
   Probe for available execution providers / devices and pick by a declared
   preference order; fall back down the list. `if platform == "darwin"` hardcodes
   a false equivalence — an arm64 Linux box, a Mac with no GPU budget, and a CI
   runner with neither all break it. Capability probing handles all three
   without a new branch.
3. **Backend choice is loggable and overridable.** Every run records which backend
   and model file it used. A config override forces a specific backend, because
   "does this reproduce on CPU?" is the first debugging question and it must not
   require a code change.
4. **Interfaces stay narrow.** No backend-specific options leaking through the
   signature. If TensorRT needs a workspace size, it reads it from its own config
   section, not from a parameter every caller must pass.

The cost of this discipline is real — roughly a day of design and a persistent
temptation to shortcut it under demo pressure. The cost of *not* doing it is
discovering on the Linux box, a week before pilot, that half the pipeline assumes
CoreML semantics.

### 5.2 Model format

**Leaning: ONNX as the single interchange format**, with platform acceleration
applied at deploy time. Training/export artefacts (PyTorch checkpoints, whatever
a vendor ships) are converted once to ONNX and that ONNX file is the thing the
repo, CI and both platforms refer to by hash.

Reasoning: ONNX Runtime runs on both platforms with the accelerators we care
about (CoreML EP on macOS, CUDA/TensorRT EP on Linux), so one artefact serves dev
and prod; and a single hashed artefact is what makes the parity suite meaningful
— comparing two *different* model files tells you nothing.

Honest caveats: ONNX export is where models with dynamic control flow or exotic
ops go to die, and the failure is usually at export time, loudly, which is the
good kind. Some commercial face SDKs do not expose an ONNX path at all — if we
pick one of those (**OPEN**, see `DECISIONS.md`), the `FaceEmbedder` backend for
that vendor becomes a platform-specific implementation and parity testing for
face embeddings becomes an integration test against a fixed SDK version instead
of a numeric comparison. That is a real cost of the commercial option and should
be weighed when that decision is made.

**AOT TensorRT engines vs first-run build.** TensorRT engines are not portable:
they are tied to GPU architecture, TensorRT version and driver. Two options:

- *Build ahead of time in CI*, ship the engine. Fast startup, reproducible, but
  requires CI hardware matching the production GPU exactly, and every driver or
  GPU change invalidates the artefact.
- *Build on first run*, cache to disk keyed by (model hash, GPU, TRT version,
  driver). First start is slow — minutes, sometimes many — subsequent starts are
  fast, and the cache self-heals across hardware changes.

**Leaning: build on first run with a keyed disk cache, for now.** We do not own
the production GPU yet (**OPEN**), so we cannot build engines for hardware we
have not chosen, and a cold-start penalty on a box that runs for months is cheap.
Revisit if startup time becomes operationally painful — e.g. if the box turns out
to be power-cycled nightly, which for a Bangladeshi factory floor with unstable
mains is a genuine possibility worth checking during the site survey. A third
option worth keeping in view: skip TensorRT entirely and run the CUDA EP, if
measured throughput on the real camera count is sufficient. TensorRT is an
optimisation, not a requirement, and it costs build complexity and one more
source of numerical divergence.

### 5.3 Numerical parity

Different backends will not produce byte-identical output. Some divergence is
normal (fp16 vs fp32, fused kernels, different NMS implementations); some means
the pipeline is broken. We need to tell them apart automatically, not by eye.

**Golden-frame regression suite.** A small, committed set of frames and short
clips — real doorway frames once we have them, synthetic before that — with
reference outputs produced by a designated reference backend (leaning: ONNX
Runtime CPU, fp32, since it is available everywhere including CI without a GPU
and is the most boring numerically).

Proposed tolerances. These are **PROVISIONAL** — they are starting points to be
tightened or loosened once we have measurements, and the first real job of the
suite is to tell us what the natural divergence actually is:

| Quantity | Tolerance | Why this one |
|---|---|---|
| Detection box IoU vs reference | ≥ 0.95 per matched box | Sub-pixel box drift is harmless; a box that moved enough to change a door-line crossing is not. |
| Detection confidence | ±0.05 absolute | Larger drift can flip a threshold decision. |
| Detection set match | no missing/extra box above operating threshold | A disappeared detection is a missed event, the failure that matters. |
| Face embedding cosine distance to reference embedding | ≤ 0.02 | Identity thresholds live around 0.3–0.4 depending on model; divergence an order of magnitude below that is safe, at the same magnitude is not. |
| Face verification decision | identical accept/reject on every golden pair | The decision, not the number, is what reaches payroll. |
| Pose keypoints | ≤ 2 px at 1080p, per visible keypoint | Pose only gates a cheap trigger; it can be loose. |
| Clip classifier scores | ±0.10, and identical top-1 | Output goes to a human queue, so ranking matters more than calibration. |

Run on macOS and Linux in CI. Linux CI is straightforward; **macOS GPU CI is
unsolved (OPEN)** — hosted macOS runners generally do not expose the GPU usefully,
so the realistic options are a self-hosted Mac runner, or accepting that the
CoreML leg of the parity suite runs on a developer machine on demand and is
recorded rather than enforced. Do not pretend this is solved in the plan.

The suite's job is not to prove the backends agree. It is to make disagreement
*visible and bounded* so that when a number changes, we know whether to care.

### 5.4 What runs in Docker, and what does not

**On macOS:** inference runs **natively** (no GPU passthrough exists for Docker
Desktop; CPU-only containerised inference would be slow enough to make the dev
loop useless). Postgres, Redis/broker, mediamtx and any other stateless
infrastructure run in containers. So the dev machine runs a hybrid: containerised
infrastructure, native Python processes for analysis.

**On Linux:** everything can be containerised via the NVIDIA Container Toolkit,
and should be, because that is what we operate.

**The consequence is dev/prod drift, and it is not small.** On macOS you are
testing your code against CoreML, native Python, host networking and a
case-insensitive filesystem. On Linux you are testing it against CUDA, a
container, container networking, and a case-sensitive filesystem. Bugs live in
every one of those gaps. We are not going to eliminate the drift — Apple has made
that impossible — so the strategy is to *bound* it:

- The same process entrypoints are used in both places. Containers on Linux run
  the identical command the developer runs natively on macOS; the container adds
  environment, not behaviour.
- All configuration comes from the same file/env mechanism in both, so
  "works locally" does not mean "works with the developer's ad-hoc arguments".
- Anything that touches paths, timezones, or the filesystem is covered by tests
  that run on Linux CI, where case sensitivity is real.
- The Linux staging box (§5.7) is the arbiter. macOS proves logic; Linux proves
  the system.

### 5.5 Architecture-specific dependencies

Known landmines, to be resolved when the stack is chosen:

- **ONNX Runtime variants.** `onnxruntime` (CPU), `onnxruntime-gpu` (CUDA/TensorRT,
  Linux x86_64), and CoreML support on macOS arm64 are different wheels with
  different names and availability. A single unconditional dependency list cannot
  express this; we need platform markers or per-platform lock files, and the
  choice of dependency tool must support that. **OPEN** (see `DECISIONS.md`).
- **CUDA-only packages** (TensorRT bindings, `pycuda`, DeepStream, anything
  `nvidia-*`) do not install on macOS at all. They must be optional extras, not
  base requirements.
- **PyTorch** builds differ (MPS on macOS arm64, CUDA on Linux). If Torch is in
  the runtime path rather than only in export/training tooling, this becomes a
  recurring version-matrix problem — an argument for keeping runtime inference on
  ONNX Runtime and Torch out of the deployed path entirely.
- **ffmpeg** builds differ in hardware acceleration: VideoToolbox on macOS,
  NVDEC/VAAPI on Linux. A pip-installed ffmpeg binary generally has neither. If
  we depend on hardware decode, we depend on a specific build, and that must be
  explicit rather than "whatever is on PATH".
- **Face SDKs**, if commercial, frequently ship Linux x86_64 binaries only. This
  could make part of the pipeline undevelopable on macOS, which would be a strong
  argument against that option. Check before choosing, not after.

### 5.6 Can a third-party NVR (Frigate et al.) be in the dev loop?

Honestly: **probably not on macOS, and that materially affects the plan.**

Frigate's practical assumptions are Linux, a GPU or Coral TPU, and container GPU
passthrough. On an Apple Silicon Mac it runs only CPU-only in a container — which
technically starts, and gives you a detection rate low enough that anything you
learn from it about pipeline behaviour is misleading. It also brings its own
opinions about detection, tracking, recording and event semantics, which we would
then have to either adopt or fight.

That leaves three positions, and this is **OPEN**:

1. **No NVR substrate.** Build ingest, clipping and events ourselves. Most work,
   full control, identical on both platforms, and the doorway/pairing logic — the
   part that matters — is ours regardless. Currently the leaning, mostly because
   the parts an NVR gives us cheaply (recording, motion, a UI) are not the parts
   our value is in, and the parts we need (audited doorway events, pairing,
   payroll-grade traceability) no NVR gives us.
2. **NVR as substrate, entering only at Linux staging.** Take recording and clip
   management from Frigate, subscribe to its events. Consequence: a component
   central to the system is untestable on the primary dev machine, so every
   change touching it is "works on staging or nowhere". That is a real tax on a
   ~6–10 week timeline.
3. **NVR alongside, not underneath.** Run it on the Linux box as an operator
   convenience (browsing recordings, camera health) while our pipeline reads RTSP
   directly and owns its own clips. Gets us a camera-ops UI for little
   architectural cost, at the price of duplicate decoding of the same streams —
   which on a single GPU may or may not be affordable. Depends on the hardware
   decision.

What would settle it: a measured GPU/decode budget on the real production box for
the real camera count, and an honest look at how many operator-facing features we
would otherwise have to build.

### 5.7 The third environment: Linux staging

A Linux box with an NVIDIA GPU that mirrors production: same OS, same container
runtime, same GPU family if possible. **Known as of 2026-09-16:** the staging
box is a Linux/x86_64 machine with an RTX 3060/4060-class GPU (≥ 12 GB VRAM).
That is enough to *prove the system*; production sizing for the real camera
count remains open (ADR-0020).

There is also a third machine in practice: a GPU-less Linux box used for
writing code. It runs the same source tree with the CPU reference backend
(onnxruntime CPU), which is exactly the reference leg of the parity suite —
useful, not a compromise — but it proves neither CoreML nor CUDA. macOS proves
the CoreML leg; the staging box proves the system.

**Earliest point code must be proven there: as soon as the first analysis
pipeline produces an event end-to-end** — not after the demo, not "before pilot".
Concretely, that is the end of the first pipeline milestone in `PLAN.md`, and it
must be a milestone exit criterion rather than an aspiration.

Why deferring is dangerous, specifically: every problem in §5.4 and §5.5 —
CUDA wheels, case-sensitive paths, container networking, hardware decode,
TensorRT build time, GPU memory budget across four pipelines — surfaces only on
Linux, and every one of them is discovered *late* if you wait. Late means during
demo preparation, in a week when there is no slack. A staging box exercised from
week two turns each of these into a one-hour annoyance. The same box exercised in
week eight turns them into the reason the demo slips.

The staging box does not need to be the production box, and does not need to
match its GPU exactly to be useful. It needs to exist and to be used
continuously. If no physical box is available, a rented cloud GPU instance is an
acceptable substitute for everything except final performance numbers — but note
that sending real site footage to it has privacy consequences (see
`PRIVACY_AND_COMPLIANCE.md`), so it runs synthetic and public footage only.

### 5.8 Mundane footguns

- **Case-insensitive APFS.** `models/Face.onnx` and `models/face.onnx` are the
  same file on the dev Mac and different files on the Linux box. This breaks at
  deploy time, and the fix is a CI check on Linux plus a convention: all repo
  paths lowercase with underscores, no exceptions.
- **Timezone.** The factory is `Asia/Dhaka`, UTC+06:00, **no daylight saving**.
  Store every timestamp in UTC; convert at display and at pay-period boundaries
  only. Pay periods, shift boundaries and the canteen allowance window are
  local-day concepts, so the conversion must be explicit and centralised, and the
  policy timezone must be config, not the server's locale. A server accidentally
  set to UTC would silently shift every pay-period boundary by six hours.
- **Clocks.** Camera clocks, DVR clocks and server clocks drift. Payroll-relevant
  timestamps must come from one authority (the ingest server, NTP-disciplined),
  with camera-reported time recorded alongside for audit but never used for
  arithmetic. Two cameras disagreeing about the time is how an exit precedes its
  own enter.
- **Line endings / locale.** Enforce LF in `.gitattributes`. Never format numbers
  or parse dates using the ambient locale — the payroll export especially.
- **File watchers.** macOS FSEvents and Linux inotify differ in semantics and
  limits (inotify watch limits bite in containers). Avoid depending on file
  watching in the runtime path; poll or use explicit signalling.
- **`/dev/shm`.** Docker defaults to 64 MB. Anything using shared memory for
  frame passing (multiprocessing queues, PyTorch DataLoader workers) will fail in
  confusing ways. Size it explicitly in the Linux compose/run config.
- **GPU memory.** Four pipelines plus decode on one GPU is a budget, not an
  assumption. It must be measured on staging before the camera count is promised.

---

## 6. The virtual camera rig

**Not a test fixture. Infrastructure.** It exists because no cameras exist yet,
and it survives into production as the demo-day fallback and the CI video source.

**Shape.** Recorded and synthetic video files, looped and served over RTSP by
mediamtx (or equivalent), so that at the pipeline boundary a virtual camera is
indistinguishable from a real one: same protocol, same auth, same reconnect
behaviour, same H.264/H.265 decode path. Nothing downstream of the stream manager
may know whether a stream is virtual.

**Requirements.**

- Deterministic playback for tests: same file, same start offset, same frames,
  so a golden-frame test is reproducible.
- Wall-clock mapping: a test needs to say "this clip starts at 13:58 local" so
  that pairing logic and pay-period boundaries can be exercised.
- Fault injection: drop the stream, stall it, corrupt frames, change resolution
  mid-stream, introduce latency. WiFi cameras will do all of this, and the system
  must degrade rather than crash. This is the rig's most valuable feature and the
  one most likely to be skipped under time pressure.
- Multiple simultaneous streams, to approximate the real camera count for load
  testing.

**What footage we need** and which behaviours cannot be validated without real
cameras is in `FOOTAGE.md`. The short version: door crossings, canteen crowd
flow, seated work, and violence-like motion — and we cannot fake motion blur
under uncontrolled auto-exposure, real walk-through capture geometry, night
lighting, or WiFi stream instability at the level that matters.

**Target real sources** (documented so the rig can mimic them, not designed
around exclusively):

- 2× Imou dual-lens WiFi PTZ. Each exposes two independent lenses: a fixed
  ~2304×1296 and a pan-tilt ~2880×1620. RTSP auth uses the **safety code printed
  on the device**, not the app password — a detail that costs an afternoon if
  undocumented. WiFi means stream instability is normal, not exceptional.
- 3–4 Dahua analog cameras behind a DVR, pulled per channel from the DVR, with a
  **low concurrent-client limit**. That limit is an architectural constraint: one
  consumer per channel, fanned out internally. A second process opening its own
  RTSP connection can starve the first, which is also a denial-of-evidence risk
  (see `THREAT_MODEL.md`).

---

## 7. Failure modes and degradation

The governing rule: **degrade toward no deduction and a visible flag.** Never
toward a guess.

| Failure | Behaviour | Payroll effect |
|---|---|---|
| Camera offline | Flag stream down, record the outage window | All canteen events in the window unpaired → zero deduction, flagged |
| Face not detected at door | `unknown` doorway event, clip retained | Unpaired → zero deduction, flagged |
| Face below match threshold | `unknown`, never a best guess | Zero deduction, flagged |
| GPU OOM / inference crash | Pipeline restarts; gap recorded explicitly | Gap window fails open |
| Clock skew detected | Flag the affected window; refuse to compute overage across it | Zero deduction, flagged |
| Event store unavailable | Ingest buffers or drops with a recorded gap; never silently continues | Gap window fails open |
| Clip missing for a record | Record cannot be exported to payroll at all | Blocked, flagged |
| Violence model unavailable | No queue items; no alternative path | None |

Two invariants fall out of that table and belong in code as assertions, not
comments:

1. **No clip, no deduction.** A payroll line without a retrievable clip reference
   is not exportable.
2. **No gap, no deduction.** If a stream outage or clock anomaly overlaps a dwell
   interval, that interval is flagged and contributes zero.

---

## 8. Security and data boundaries

Detail in `PRIVACY_AND_COMPLIANCE.md` and `THREAT_MODEL.md`; the architectural
commitments:

- **Biometric templates never leave the site.** Enrolment images and embeddings
  live on the on-prem box. This rules out cloud face APIs for the identification
  path and is a constraint on the face-recognition decision (**OPEN**), not an
  outcome of it.
- **Clips never leave the site** except by deliberate, logged human export for a
  dispute or audit.
- **Network segmentation.** Cameras on their own VLAN, reachable from the
  analysis box only. WiFi cameras with device-code auth are not devices to expose.
- **Access tiers.** At minimum: viewer (dashboards, no clips), reviewer (review
  queue + clips), payroll (export), admin (enrolment, config). Enrolment and the
  shadow-mode flag are the two highest-privilege operations in the system.
- **Audit log for reads, not just writes.** Who watched which clip, when, and why
  — because a system whose whole premise is auditability cannot have an
  unaudited viewing path.
- **The shadow-mode flag is not ordinary config.** Flipping it is a logged,
  human-authorised action, and it is the one setting that requires the sign-off
  described in `AGENTS.md`.

---

## 9. Open architectural questions

Each of these is an ADR stub in `DECISIONS.md`:

- NVR substrate vs from scratch (§5.6)
- Face recognition: self-hosted service vs embedded library vs commercial SDK
- Detection/pose model family and its licence (AGPL implications for a deployed
  commercial system are a real constraint, not a footnote)
- Violence classifier approach, given restricted datasets and no site footage
- Event bus and database
- Dashboard framework
- Deployment orchestration
- Monorepo vs polyrepo, language boundaries, Python version
- Occupancy granularity: per-operator or line-level aggregate only
- Production hardware sizing
- macOS GPU CI (§5.3)
- The product name
