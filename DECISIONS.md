# DECISIONS.md

Architecture Decision Record log for `PROJECT_NAME`.

## Format

Each ADR is short. If it needs more than a screen, the reasoning belongs in
`ARCHITECTURE.md` and the ADR links to it.

```
## ADR-NNNN — Title
Date: YYYY-MM-DD
Status: OPEN | ACCEPTED | SUPERSEDED by ADR-NNNN | REJECTED

Context.    What forces this decision, in two or three sentences.
Options.    2–3 real candidates with honest trade-offs.
Decision.   What we chose, or "not decided" with a provisional leaning.
Reasoning.  Why. Required — an ADR without reasoning cannot be challenged.
Changes it. What evidence would flip this.
Consequences. What we now have to live with.
```

Rules:

- Numbers are never reused. A reversed decision gets a new ADR that supersedes
  the old one; the old one stays, with its reasoning, so the argument is not
  re-litigated from scratch.
- `OPEN` entries carry a **provisional leaning** so work can proceed. A leaning
  is not a decision and must not be cited as one.
- Anything in `SOUL.md` requires an ADR *and* human sign-off to change. See
  `AGENTS.md`.

---

# Accepted

## ADR-0001 — Scope is four pipelines; theft and floor tracking are out
Date: 2026-09-13 · Status: ACCEPTED

**Context.** A camera system in a factory can be asked to do arbitrarily many
things. Unbounded scope here means none of it works.

**Decision.** In: gate attendance verification, canteen dwell, workstation
occupancy, violence detection. Out: theft detection (deferred), floor-wide person
tracking, cross-camera re-identification.

**Reasoning.** Each included pipeline has a clear consumer and a bounded failure
mode. Theft detection has neither yet. Re-ID is covered separately in ADR-0002.

**Changes it.** A buyer-audit requirement that cannot be met without a dropped
capability.

**Consequences.** Requests for "while you're at it" features get answered with
this ADR rather than a discussion.

## ADR-0002 — Identity is resolved at doorways only
Date: 2026-09-13 · Status: ACCEPTED

**Context.** We must attribute canteen dwell to individuals but do not want, and
do not need, floor-wide tracking.

**Options.** (a) Doorway-only identity. (b) Cross-camera re-ID for full-floor
attribution. (c) Wearables/BLE tags on the floor.

**Decision.** (a).

**Reasoning.** Assigned seating already maps seat → person via the roster, so
re-ID would re-derive information we have for free, using the most failure-prone
technique in the domain — one whose errors are silent and compounding, in a
building where many people wear similar clothing. It is also the capability that
converts measurement into surveillance: without it, "where was this person all
day" is structurally unanswerable. Full reasoning in `ARCHITECTURE.md` §2.

**Changes it.** Evidence that seating is not enforced in practice, or a
compliance requirement for zone headcount that doorway counting cannot meet.

**Consequences.** Empty seats cannot be attributed to a reason or reliably to a
person. All identification accuracy risk concentrates at a few doorways.

## ADR-0003 — Two separate numbers; occupancy never affects pay
Date: 2026-09-13 · Status: ACCEPTED

**Context.** Occupancy data is the obvious thing to convert into a productivity
deduction, and someone will propose it.

**Decision.** Canteen overage is the only payroll-affecting output. Occupancy is
a management indicator, computed by a separate pipeline, stored with no path to a
person, and structurally unable to reach a payroll table.

**Reasoning.** Occupancy measures presence at a coordinate, not work. An operator
away from a seat may be collecting bundles, waiting on a mechanic, at the toilet,
or covering another line. Paying on that number pays on something that does not
mean what it appears to mean. `SOUL.md`.

**Changes it.** Nothing short of a different product. A proposal to merge them is
a `SOUL.md` change and needs explicit human sign-off.

**Consequences.** Some management questions stay unanswered by design. A test
enforces the separation.

## ADR-0004 — Fail open: ambiguity yields zero deduction and a flag
Date: 2026-09-13 · Status: ACCEPTED

**Decision.** Every unpaired, implausible, gap-affected or low-confidence case
resolves to zero deduction plus a review flag. No estimated or default deduction,
ever.

**Reasoning.** Error costs are asymmetric. A missed deduction is a rounding error
across a large payroll. A false deduction takes money from one specific person
with limited power to contest it. When costs are that asymmetric, the bias must
be deliberate and in code.

**Changes it.** Nothing. Cost of abuse is accepted explicitly.

**Consequences.** The system is gameable, and we will see systematic gaming
before we see systematic error. That trade is intentional.

## ADR-0005 — Shadow mode by default
Date: 2026-09-13 · Status: ACCEPTED

**Decision.** Deductions are computed and reported but not exported to payroll
until a config flag is set, which requires legal and buyer-compliance
(BSCI/SMETA/WRAP) review first. Flipping it is a logged, human-authorised action.

**Reasoning.** We will be wrong in ways we cannot anticipate from outside the
building. The cheapest place to discover that is a report nobody is paid from.

**Consequences.** Every demo and the entire pilot run in shadow mode. Nobody may
describe shadow output as a live deduction.

## ADR-0006 — We never write to payroll; a human keys in a signed export
Date: 2026-09-13 · Status: ACCEPTED

**Options.** (a) Reviewed export, human enters it. (b) Direct API/DB integration
with their HR/payroll software. (c) We become the timekeeping system of record.

**Decision.** (a).

**Reasoning.** Keeps the irreversible action with a human; makes shadow mode a
one-line difference (produce the export, do not hand it over); avoids integrating
with software we have not seen; and keeps scope off the cliff that is (c) — full
rosters, shifts, overtime, leave, holidays, period close.

**Changes it.** A factory requirement for automated payroll posting, which would
need its own ADR and a rollback design.

**Consequences.** A manual step each pay period, and a signature/hash so the
entered numbers can be proven to match what we computed.

## ADR-0007 — Single factory; no multi-tenancy
Date: 2026-09-13 · Status: ACCEPTED

**Decision.** Build for one site. Site-specific configuration stays a seam (one
config surface, no hardcoded site facts) but multi-tenancy, per-site licensing
and fleet management are not built.

**Reasoning.** We have one site, ~6–10 weeks, and no second customer. Building
tenancy now costs weeks and would be designed against imaginary requirements.

**Changes it.** A second factory with a signed commitment.

**Consequences.** A second site means real work, not a config file. Accepted.

## ADR-0008 — Auditability is an architectural requirement
Date: 2026-09-13 · Status: ACCEPTED

**Decision.** Every payroll-affecting record resolves to a clip and timestamp,
retrievable by a non-engineer in minutes. No clip, no deduction — enforced in
code, and retention must consult references rather than age alone.

**Reasoning.** An unauditable deduction is indistinguishable from an arbitrary
one, including to us. This constrains storage layout, retention and clock
discipline, all of which are far cheaper to accept now than to retrofit.

**Consequences.** Storage cost rises. Retention jobs get more complex.

---

# Open

Each carries a provisional leaning so work can start. A leaning is not a
decision.

## ADR-0009 — NVR/VMS substrate vs build from scratch
Date: 2026-09-13 · Status: **OPEN**

**Options.** (a) Build ingest/clipping/events ourselves. (b) Frigate-class NVR as
substrate, entering only at Linux staging. (c) NVR alongside as an operator
convenience while we read RTSP directly.

**Leaning:** (a), with (c) as a cheap later addition. The parts an NVR gives
cheaply — recording, motion, a camera UI — are not where our value is; the parts
we need (audited doorway events, pairing, payroll-grade traceability) no NVR
provides. And (b) makes a central component untestable on the primary dev
machine, which on this timeline is a serious tax. `ARCHITECTURE.md` §5.6.

**Changes it.** A measured GPU/decode budget showing we cannot afford our own
decode path, or a long list of operator-facing features we would otherwise build.

## ADR-0010 — Face recognition: self-hosted service vs embedded library vs commercial SDK
Date: 2026-09-13 · Status: **OPEN**

**Options.** (a) Self-hosted recognition service with its own API and store.
(b) Embedded library — we own detection, alignment, embedding, matching and the
template store. (c) Commercial SDK.

**Leaning:** (b). We already need our own template store with `model_ref`
versioning (`DATA_MODEL.md`), matching thresholds must be ours to tune per
door and per task (1:1 gate verification and 1:N canteen matching are different
problems with different thresholds), and an embedded library keeps everything
behind `FaceEmbedder` with an ONNX artefact the parity suite can test. (a) adds
an operational component for little gain at this scale. (c) is the accuracy
favourite but risks a Linux-x86-only binary, which would make part of the
pipeline undevelopable on macOS, and may have no ONNX path — breaking numerical
parity testing entirely.

**Hard constraints regardless:** templates never leave the site (rules out cloud
APIs), and the licence must permit commercial deployment.

**Changes it.** Measured accuracy on real doorway footage falling short of what
an SDK demonstrably achieves — a question only M8/M9 footage can settle.

## ADR-0011 — Detection and pose model family, and licensing
Date: 2026-09-13 · Status: **OPEN**

**Options.** (a) YOLO-family via Ultralytics (**AGPL-3.0** — a live constraint
for a deployed commercial system, not a footnote). (b) Permissively licensed
alternatives (RT-DETR variants, MMDetection-derived exports, licence per model).
(c) A commercially licensed model.

**Leaning:** start with (a) for prototyping speed, and treat the licence as a
**blocking question before pilot**, not before demo. Decide early enough that a
swap is cheap — which is exactly what the `Detector`/`PoseEstimator` abstraction
buys us.

**Changes it.** Legal advice on AGPL exposure for an on-prem deployment at a
client site; measured accuracy differences on doorway footage.

## ADR-0012 — Violence classifier approach
Date: 2026-09-13 · Status: **OPEN**

**Context.** Public violence datasets are access-restricted and often poorly
matched to overhead factory CCTV. No site footage exists. This is the weakest
evidence base in the project.

**Options.** (a) Pose-derived features (velocity, proximity, limb dynamics) into
a small classifier — interpretable, cheap, trainable on little data, weak on
subtlety. (b) A pretrained video-action model fine-tuned on whatever violence
data we can legitimately obtain — stronger in principle, needs data we may not
get, harder to explain. (c) Trigger only, no classifier: pose heuristics send
candidates straight to human review — no ML claims, higher reviewer load.

**Leaning:** (c) for the demo, (a) as the first real classifier. Since the output
is always a human queue (ADR-0001), a crude trigger with a decent recall rate is
*useful* immediately, while a weak classifier is worse than none because it
invites unearned trust.

**Changes it.** Access to a suitable dataset; site footage after M9; measured
reviewer load making (c) impractical.

## ADR-0013 — Event bus / inter-process transport
Date: 2026-09-13 · Status: **OPEN**

**Options.** (a) Postgres as the queue (`LISTEN/NOTIFY`, or polling a table).
(b) Redis streams. (c) A real broker (NATS, RabbitMQ).

**Leaning:** (a) or (b), leaning (a). Single box, modest event rate — doorway
events are a handful per second at peak, not thousands — and one fewer component
to operate. Frames do **not** go through the bus in any case; only events do.

**Changes it.** Measured event rates or a need for durable replay semantics that
Postgres makes awkward.

## ADR-0014 — Database
Date: 2026-09-13 · Status: **OPEN**

**Options.** (a) Postgres. (b) Postgres + TimescaleDB for occupancy samples.
(c) SQLite.

**Leaning:** (a), revisit (b) only if occupancy sample volume proves it. (c) is
tempting for a single box but poor under concurrent writers from several
pipelines.

## ADR-0015 — Dashboard / review UI framework
Date: 2026-09-13 · Status: **OPEN**

**Options.** (a) Server-rendered templates + minimal JS. (b) React/Next SPA
against an API. (c) A low-code dashboard tool.

**Leaning:** (a) for the review queue, which is the UI that matters and is mostly
"a list, a video player, two buttons". (c) fails on the video-with-clip-seek
requirement. (b) is justified only if the management dashboards grow.

## ADR-0016 — Deployment orchestration
Date: 2026-09-13 · Status: **OPEN**

**Options.** (a) docker compose + systemd on one box. (b) Kubernetes (k3s).
(c) Bare systemd units, no containers.

**Leaning:** (a). One box, intermittent internet, no ops team. (b) is
unjustifiable at this scale. Remember that on macOS inference runs natively
regardless (`ARCHITECTURE.md` §5.4), so compose describes the Linux deployment,
not the dev loop.

## ADR-0017 — Monorepo vs polyrepo, language boundaries, Python version
Date: 2026-09-13 · Status: **OPEN**

**Leaning:** monorepo; Python for pipelines and services; JS only if ADR-0015
lands on a SPA; no second backend language unless a measured bottleneck demands
it. Python version: pin one, choose it by what the chosen inference runtime and
CUDA stack support on Ubuntu 22.04/24.04 — a constraint from below, not a
preference.

**Changes it.** A dependency that forces a split, or measured throughput that
needs a native component.

## ADR-0018 — Dependency management across platforms
Date: 2026-09-13 · Status: **OPEN**

**Context.** `onnxruntime` / `onnxruntime-gpu` / CoreML availability differ by
platform; CUDA-only packages do not install on macOS at all
(`ARCHITECTURE.md` §5.5).

**Options.** (a) uv with platform markers and extras. (b) Poetry with the same.
(c) Per-platform lock files.

**Leaning:** (a) with optional extras (`[cuda]`, `[macos]`) and a lock that
resolves on both platforms. Whatever we choose must express "this wheel only on
Linux x86_64" without a runtime hack.

## ADR-0019 — Occupancy granularity: per-operator or line-level only
Date: 2026-09-13 · Status: **OPEN**

**Context.** Seats are assigned, so per-seat occupancy is effectively
per-operator even though we store no `person_id`. That is a privacy fact the
schema does not express.

**Options.** (a) Line-level aggregate only. (b) Per-seat stored, per-seat shown.
(c) Per-seat stored, aggregate shown, per-seat behind a higher access tier.

**Leaning:** (c). It preserves the diagnostic value (which station on which line
is idle) while keeping the re-identifiable view behind an access control, and it
matches the honest position that per-seat data is closer to identified data than
its schema suggests. `SOUL.md` and ADR-0003 mean none of these can touch pay.

**Changes it.** A management requirement for per-operator reporting — which would
need an explicit argument about what it is for, since the answer is usually
discipline.

## ADR-0020 — Production hardware
Date: 2026-09-13 · Status: **OPEN**

**Context.** No box purchased. Docs assume one on-prem Ubuntu machine with a
single NVIDIA GPU, intermittent internet, cameras on the factory LAN. **This is
an unvalidated assumption.**

**Blocked on.** M8 site survey: real camera count, resolutions, decode budget,
GPU memory across four concurrent pipelines, and mains stability (which decides
ADR-0021).

**Changes it.** Measured throughput on staging; a camera count materially larger
than assumed.

## ADR-0021 — TensorRT engines: ahead-of-time vs first-run build
Date: 2026-09-13 · Status: **OPEN**

**Options.** (a) Build in CI, ship engines. (b) Build on first run, cache keyed
by (model hash, GPU, TRT version, driver). (c) Skip TensorRT; run the CUDA EP.

**Leaning:** (b) now, because we do not own the production GPU yet and cannot
build engines for unchosen hardware; a cold start is cheap on a long-running box.
Keep (c) in view — TensorRT is an optimisation, and it costs build complexity and
one more source of numerical divergence. `ARCHITECTURE.md` §5.2.

**Changes it.** Evidence the box is power-cycled often (likely enough in a
factory with unstable mains to be worth measuring), or throughput that CUDA EP
cannot meet.

## ADR-0022 — macOS leg of the parity suite in CI
Date: 2026-09-13 · Status: **OPEN**

**Context.** Hosted macOS runners generally do not expose the GPU usefully, so
the CoreML side of the golden-frame suite has no obvious CI home.

**Options.** (a) Self-hosted Mac runner. (b) Run on a developer machine on
demand, record results, do not gate merges. (c) Do not test the CoreML path
numerically at all.

**Leaning:** (b) initially, (a) if divergence bites. (c) is not acceptable —
it removes the only evidence that dev and prod agree.

## ADR-0023 — Partial-day charging when one interval is flagged
Date: 2026-09-13 · Status: **OPEN**

**Context.** `DATA_MODEL.md` §4 currently says any flagged interval in a day
zeroes the whole day's overage.

**Options.** (a) Zero the whole day (current, strictest fail-open). (b) Charge
clean intervals, flag the rest.

**Leaning:** (a) until shadow-mode data shows how often it triggers. If it fires
on most days, the system is not actually measuring anything and (b) is not the
fix — better capture is.

**Changes it.** Measured flag rates during M9.

## ADR-0024 — Product name
Date: 2026-09-13 · Status: **OPEN**

`PROJECT_NAME` is a placeholder throughout. The working directory is
`argus-vision`, which is a directory name, not a decision. Worth a moment's
thought that "Argus" — the hundred-eyed watchman — names the surveillance reading
of this system rather than the measurement-and-audit reading we argue for in
`SOUL.md`. Naming is not neutral when a buyer's auditor reads the slide.
