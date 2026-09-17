# PLAN.md

Phased milestones from today (documentation only, no code) to a management demo
in roughly 6–10 weeks, and onward to a pilot.

Sequencing principle: **front-load the things that can kill the project**, which
are not the things that look hardest. The violence classifier looks hardest and
is the least likely to sink us, because nothing depends on it. Doorway capture
quality, cross-platform parity, and pairing correctness are less glamorous and
are load-bearing.

Week numbers are indicative, not commitments. Each milestone lists what it
de-risks and what it deliberately does not do, because scope creep in this
project will come disguised as "while we're in there".

**HW-BLOCKED** marks work that cannot complete without physical cameras on site.

---

## M0 — Documentation and decisions (now)

**Goal.** Shared understanding; decisions framed, not made.

**Exit.** This doc set exists. `DECISIONS.md` has an ADR stub for every open
choice. Someone has read `SOUL.md` and disagreed with something in it.

**De-risks.** Building the wrong thing carefully.

**Not doing.** Choosing the stack. Writing code.

---

## M1 — Virtual camera rig + stream ingest (week 1–2)

**Status 2026-09-16: implemented.** Rig (deterministic synthetic footage,
manifests, fault injection), stream manager (single consumer per source, fan-out,
ring buffer, reconnect, application-level stall watchdog), clip store, event
store with append-only enforcement, and the full integration test suite are
verified on a GPU-less Linux box with containerised Postgres + mediamtx. **Not
yet done:** GPU decode on the RTX staging box — the last M1 exit criterion.

**Goal.** Frames flowing from RTSP into Python on both platforms, with the rig
serving recorded/synthetic video indistinguishably from a real camera.

**Entry.** Stack decisions for language/runtime and container layout settled
(ADR-0009, ADR-0014, ADR-0016, ADR-0017, ADR-0018 — all closed 2026-09-16).

**Exit.**
- mediamtx (or equivalent) serving ≥ 4 simultaneous looped streams.
- Stream manager: connect, decode, reconnect, fan out to multiple consumers with
  a single decode per stream; pre-trigger ring buffer working.
- Clip extraction by `(camera, time range)` producing a playable file.
- Fault injection working: stream drop, stall, resolution change, latency.
- **Runs on the Linux staging box, in containers, with GPU decode.** Not optional.
- Timestamp discipline: UTC storage, `Asia/Dhaka` conversion in one place, camera
  clock recorded separately.

**De-risks.** The single biggest schedule risk after doorway geometry: that
ingest is fiddly and platform-divergent. It always is. Also proves the staging
box exists and is usable, in week two rather than week eight.

**Not doing.** Any inference. Any UI. Real cameras.

---

## M2 — Backend abstraction + golden-frame parity (week 2–3)

**Status 2026-09-16: implemented.** `Detector`/`PoseEstimator`/`FaceEmbedder`/
`ClipClassifier` interfaces, capability-probed registry, CPU/CUDA/CoreML backend
modules, sha256-verified ONNX artefact mechanism (ADR-0026), committed golden
frames + reference outputs, tolerance suite, and the import-graph structural
check — all in CI (ADR-0025). **Verified legs:** CPU reference (Linux box + CI).
**Pending:** CUDA leg on staging, CoreML leg on the Mac (ADR-0022). ADR-0011
closed: permissively licensed models only; bring-up artefact is Apache-2.0
`ssd_mobilenet_v1`.

**Goal.** One detector running through `Detector` on CoreML (macOS) and
CUDA/TensorRT (Linux), with divergence measured rather than assumed.

**Entry.** M1 ingest works on both platforms. Detection model family chosen
(ADR-0011), with its licence understood.

**Exit.**
- `Detector` interface + ≥ 2 backends; capability-based selection; no backend
  import outside backend modules (enforced by a lint/import check, not a habit).
- ONNX artefacts referenced by hash; backend and model version logged per run.
- Golden-frame suite committed, running in Linux CI, with tolerances from
  `ARCHITECTURE.md` §5.3 — adjusted to observed divergence and documented.
- macOS leg of the suite runs somewhere, even if manually. Decide and record how
  (ADR-0022).

**De-risks.** The "it works on my Mac" class of failure, permanently. Everything
after this milestone inherits the abstraction instead of retrofitting it.

**Not doing.** Face recognition. Optimising throughput. Pretty numbers.

---

## M3 — Canteen doorway pipeline, synthetic (week 3–5)

**Goal.** The payroll-affecting path, end to end, on recorded video: face →
identity → direction → doorway event → pairing → dwell → overage → shadow report.

**Entry.** M2 parity suite green. Face recognition approach chosen (ADR-0010).

**Exit.**
- Enrolment flow for a handful of test identities (our own faces, or consented
  volunteers — see `ARCHITECTURE.md` §9).
- Face detection + embedding + matching at a door region, with an identity
  threshold chosen from measured data, not a vendor default.
- Direction from a short single-camera track over the door line. No re-ID.
- Doorway events persisted append-only with clip references.
- **Pairing state machine implemented to `ARCHITECTURE.md` §7.3, with every case in
  the table covered by a test.** Property tests: no input sequence produces a
  non-zero overage in a non-`RESOLVED` state.
- Overage computed per local day; shadow report rendered; nothing exported.
- Every payroll line resolves to a clip in under a minute, by hand, by someone
  who did not write the code.
- Monitoring metrics from `RISKS.md` §8 exist from day one: `unknown`
  face rate per door per hour, unpaired event rate per door per day, stream gap
  minutes per camera per day, flagged-day percentage.

**De-risks.** That the pairing semantics are wrong, which is the failure that
would actually cost someone money. Doing it on synthetic video means we can
construct the nasty cases (double enter, midnight crossing, stream gap) on
demand instead of waiting for them.

**Not doing.** Real-world accuracy claims — synthetic footage cannot support
them. Payroll export. Occupancy. Violence.

---

## M4 — Gate attendance verification (week 5–6)

**Goal.** Badge tap + face verify (1:1, not 1:N), producing attendance records
with `verified / mismatch / no_face` distinguished.

**Entry.** M3 face stack working. A badge/RFID input path — **simulated** if no
reader is available, which is likely at this stage.

**Exit.**
- 1:1 verification against the badge holder's template, with a threshold chosen
  and justified separately from the canteen 1:N threshold. They are different
  problems and must not share a constant.
- Three-way outcome persisted; `no_face` never collapses into `mismatch`.
- Mismatch produces a review item with a clip. Never a block, never an alarm.

**De-risks.** That verification and identification get conflated — an easy and
expensive mistake, since 1:1 is much easier and much less error-prone than 1:N,
and treating them as one problem would hide that.

**Not doing.** Turnstile or door-lock integration. Enforcement of any kind.

---

## M5 — Workstation occupancy (week 6–7)

**Goal.** Anonymous per-seat occupied/empty on a floor view, as a management
report.

**Entry.** M2 detection working; a floor view (synthetic or recorded) with
defined seat regions.

**Exit.**
- Seat regions configurable per camera, without code changes.
- Occupancy sampled on a slow cadence (seconds, not frames), smoothed to avoid
  flapping on a person leaning out of frame.
- Storage carries no `person_id` and no join path to one.
- Dashboard shows line-level and (if ADR-0019 lands that way) per-seat rates.
- A test asserts that no occupancy code path can reach a payroll table.

**De-risks.** Comparatively little — this milestone is here because it is a
visible demo win, not because it is risky. Its real risk is conceptual creep
toward pay, which the test above exists to stop.

**Not doing.** Idle-time analysis. Productivity scoring. Anything per-operator
that could become a disciplinary artefact.

---

## M6 — Violence detection, trigger + queue (week 7–9)

**Goal.** Cheap always-on pose trigger → clip → human review queue. Trigger-only
is a valid exit; a classifier stage, if any, sits before the queue.

**Entry.** M1 clipping solid. A classifier approach chosen (ADR-0012), knowing
that public datasets are access-restricted and site footage does not exist.

**Exit.**
- Pose-based trigger tuned for recall over precision, cheap enough to run
  continuously alongside everything else on one GPU.
- Trigger → clip (with pre-roll) → classifier, *if any* → queue item. With
  ADR-0012's trigger-only leaning the classifier stage is absent and the trigger
  feeds the queue directly — an acceptable exit, not a slip.
- Review UI: watch, dismiss, escalate, with the action logged.
- **No automated verdict anywhere in the path.** No notification, no name, no
  incident record without a human.
- Honest statement of expected false-positive rate on the footage we have, with
  the caveat that it means little without site footage.

**De-risks.** Least, and last, deliberately. Nothing depends on it; it is the
easiest thing to cut if weeks 7–9 evaporate. If the schedule slips, this is what
slips.

**Not doing.** Claiming accuracy. Real-time alerting. Anything automated.

---

## M7 — Demo (week 9–10)

**Goal.** A defensible demonstration to factory management on the Linux box, with
real cameras where available and the virtual rig as fallback.

**Entry.** M3 mandatory; M4–M6 as available.

**Exit.**
- Runs on the Linux staging/production box, not a laptop.
- Imou and Dahua/DVR streams ingested live if the cameras are present
  (**HW-BLOCKED**); rig fallback ready and rehearsed if not.
- Shadow-mode report shown, with the fact that it is shadow mode stated out loud
  rather than buried.
- The audit path demonstrated live: pick a line, watch the clip, in under a
  minute.
- A named list of what is not validated without site cameras (`ARCHITECTURE.md` §9.2),
  presented rather than hidden.

**Not doing.** Turning the payroll flag on. Promising accuracy numbers from
synthetic footage.

---

## M8 — Site survey and camera installation (HW-BLOCKED, parallel from week 4)

**Goal.** Know the real site before depending on assumptions about it.

Not a software milestone, and the earliest it can start is whenever site access
is granted — which is why it runs in parallel rather than in sequence.

**Exit.**
- Every canteen entrance and exit identified, including the one nobody mentions.
- Camera positions chosen for walk-through face capture: height, angle, lighting,
  backlight at doorways (a doorway is the worst lighting case in any building).
- Power and network reality: PoE or not, WiFi coverage, VLAN feasibility.
- Mains stability and whether the box gets power-cycled — this decides the
  TensorRT AOT-vs-first-run question (`ARCHITECTURE.md` §5.2).
- Headcount through the canteen doors per minute at peak, measured, not guessed.
- Production hardware sized against measured camera count and resolution.

**De-risks.** Everything in `ARCHITECTURE.md` §9.2. Most of the residual risk in
this
project is here, and none of it can be retired from a desk.

---

## M9 — Pilot with shadow mode (post-demo)

**Goal.** Run against real site footage, in shadow mode, long enough for the
error modes to show themselves.

**Exit.**
- Continuous operation on site for a defined period (**PROVISIONAL**: one full
  pay period, minimum).
- Measured rates: unpaired events, unknown faces, stream gaps, per door and per
  hour. These numbers *are* the deliverable.
- Manual audit of a sample of computed overages against clips, by a human who is
  not us.
- Dispute path exercised at least once, end to end.
- Legal review and buyer-compliance review complete (`RISKS.md`).

**Not doing.** Flipping the payroll flag. That is a separate, explicit decision
after this milestone produces evidence, and it needs the sign-off described in
`AGENTS.md`.

---

## Hardware-blocked summary

| Milestone | Blocked? |
|---|---|
| M0 docs | No |
| M1 rig + ingest | No |
| M2 abstraction + parity | No — but needs the Linux staging box |
| M3 canteen pipeline | No (real-accuracy claims are blocked) |
| M4 gate verify | Partly — badge reader can be simulated |
| M5 occupancy | No |
| M6 violence | No (validation is blocked) |
| M7 demo | Degrades gracefully to the rig |
| M8 site survey | **Fully blocked** |
| M9 pilot | **Fully blocked** |

The useful observation: almost everything is buildable without cameras, and
almost nothing is *believable* without them. Plan the build accordingly, and do
not let a green demo on synthetic footage be mistaken for a working system.
