# DECISIONS.md

Architecture Decision Record log for **Sparrow Vision**.

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
Date: 2026-09-13 · Accepted 2026-09-16 · Status: **ACCEPTED** (option a)

**Options.** (a) Build ingest/clipping/events ourselves. (b) Frigate-class NVR as
substrate, entering only at Linux staging. (c) NVR alongside as an operator
convenience while we read RTSP directly.

**Decision.** (a), with (c) as a cheap later addition. The parts an NVR gives
cheaply — recording, motion, a camera UI — are not where our value is; the parts
we need (audited doorway events, pairing, payroll-grade traceability) no NVR
provides. And (b) makes a central component untestable on the primary dev
machine, which on this timeline is a serious tax. `ARCHITECTURE.md` §5.6.
Accepted per the leaning below; no new evidence had arrived.

**Changes it.** A measured GPU/decode budget showing we cannot afford our own
decode path, or a long list of operator-facing features we would otherwise build.

## ADR-0010 — Face recognition: self-hosted service vs embedded library vs commercial SDK
Date: 2026-09-13 · Accepted 2026-09-17 · Status: **ACCEPTED** (option b)

**Options.** (a) Self-hosted recognition service with its own API and store.
(b) Embedded library — we own detection, alignment, embedding, matching and the
template store. (c) Commercial SDK.

**Decision.** (b), embedded library. The stack is InsightFace `antelopev2`
(SCRFD-10g face detector + `glintr100` ArcFace embedder), consumed as ONNX
artefacts through the backend registry and pinned by sha256 in
`models/registry.yaml` (ADR-0026). Thresholds are separate config fields per
task — `face.gate_verify_threshold` for 1:1 and `face.canteen_match_threshold`
for 1:N — and must never share a constant.

**Reasoning.** (b). We already need our own template store with `model_ref`
versioning (`ARCHITECTURE.md` §7), matching thresholds must be ours to tune per
door and per task (1:1 gate verification and 1:N canteen matching are different
problems with different thresholds), and an embedded library keeps everything
behind `FaceEmbedder` with an ONNX artefact the parity suite can test. (a) adds
an operational component for little gain at this scale. (c) is the accuracy
favourite but risks a Linux-x86-only binary, which would make part of the
pipeline undevelopable on macOS, and may have no ONNX path — breaking numerical
parity testing entirely.

**Hard constraints regardless:** templates never leave the site (rules out cloud
APIs), and the stack must include a liveness/anti-spoofing story for the gate
(`RISKS.md` §4) — photo and replay attacks are easy, obvious, and work
against naive systems.

**Two constraints this decision does NOT satisfy, stated rather than buried:**

1. **No liveness/anti-spoofing is implemented.** A printed photo or a phone
   screen held to the gate camera will verify successfully. `RISKS.md` §4
   lists this as a requirement of the face decision, not an optional extra. The
   only mitigation in place is the one that document already names — gate
   cameras are supervised in practice. This must be said out loud at any
   demonstration and must be closed before a pilot.
2. **The `antelopev2` weights are published for non-commercial research use.**
   The insightface *code* is MIT; the pretrained weights are not. Permitted here
   under ADR-0030 (non-commercial test environment) and blocking for any
   commercial deployment.

**Changes it.** Measured accuracy on real doorway footage falling short of what
an SDK demonstrably achieves — a question only M8/M9 footage can settle. A
commercial deployment, which forces the weights question regardless of accuracy.

**Consequences.** Every `face_template` row records `model_ref`; a model change
invalidates every template and the process refuses to start rather than
comparing embeddings across models. Enrolment becomes the most privileged write
in the system (`RISKS.md` §10).

## ADR-0011 — Detection and pose model family, and licensing
Date: 2026-09-13 · Accepted 2026-09-16 · Superseded 2026-09-17
Status: **SUPERSEDED by ADR-0030**

> Superseded for the non-commercial test environment only. The reasoning below
> stands and is the reasoning to return to the moment this project moves toward
> commercial deployment. ADR-0030 records what changed and why.

**Options.** (a) YOLO-family via Ultralytics (**AGPL-3.0** — a live constraint
for a deployed commercial system, not a footnote). (b) Permissively licensed
alternatives (RT-DETR variants, MMDetection-derived exports, licence per model).
(c) A commercially licensed model.

**Decision.** (b). Permissively licensed models only (Apache-2.0/MIT-class), so
the deployed path never carries an AGPL question and the sign-off gate in
`AGENTS.md` §2.9 is not needed. Bring-up artefact: `ssd_mobilenet_v1_10.onnx`
(Apache-2.0, ONNX model zoo) — chosen for parity bring-up, not for final
accuracy; the family is revisited with measured data at M8/M9. The
`Detector`/`PoseEstimator` abstraction keeps any later swap cheap.
Ultralytics remains excluded from the deployed path unless legal advice
reopens this ADR.

**Changes it.** Legal advice on AGPL exposure for an on-prem deployment at a
client site; measured accuracy differences on doorway footage.

## ADR-0012 — Violence classifier approach
Date: 2026-09-13 · Accepted 2026-09-17 · Status: **ACCEPTED** (option c)

**Context.** Public violence datasets are access-restricted and often poorly
matched to overhead factory CCTV. No site footage exists. This is the weakest
evidence base in the project.

**Options.** (a) Pose-derived features (velocity, proximity, limb dynamics) into
a small classifier — interpretable, cheap, trainable on little data, weak on
subtlety. (b) A pretrained video-action model fine-tuned on whatever violence
data we can legitimately obtain — stronger in principle, needs data we may not
get, harder to explain. (c) Trigger only, no classifier: pose heuristics send
candidates straight to human review — no ML claims, higher reviewer load.

**Decision.** (c), trigger only. Pose keypoints from `yolo26m-pose` feed a
documented weighted heuristic — wrist speed normalised by torso length,
inter-person proximity, limb extension toward another person, pose energy — which
sends candidates straight to the human review queue. There is no classifier and
no model-derived score presented as a probability.

**Reasoning.** Since the output is always a human queue (ADR-0001), a crude
trigger with decent recall is *useful* immediately, while a weak classifier is
worse than none because it invites unearned trust. Note what is **not** the
reason: ADR-0030 lifted the licence constraint, so (b) is now technically
available. It is still refused, because the datasets that would train it are
hand-held and movie footage, and a classifier trained on them reports confident
nonsense on an overhead CCTV angle (`ARCHITECTURE.md` §9.1). The blocker is
evidence,
not licensing.

**Changes it.** Access to a suitable dataset *matched to the camera angle*; site
footage after M9; measured reviewer load making (c) impractical.

**Consequences.** `violence_candidate.classifier_score` exists in the schema and
stays null. The UI must not imply an ML judgement, and no automated action may
be attached to a candidate (`AGENTS.md` §2.4). An empty queue is not a success
metric (`AGENTS.md` §8).

## ADR-0013 — Event bus / inter-process transport
Date: 2026-09-13 · Accepted 2026-09-16 · Status: **ACCEPTED** (option a)

**Options.** (a) Postgres as the queue (`LISTEN/NOTIFY`, or polling a table).
(b) Redis streams. (c) A real broker (NATS, RabbitMQ).

**Decision.** (a). Single box, modest event rate — doorway events are a handful
per second at peak, not thousands — and one fewer component to operate. Frames
do **not** go through the bus in any case; only events do. Accepted per the
leaning below; no new evidence had arrived.

**Changes it.** Measured event rates or a need for durable replay semantics that
Postgres makes awkward.

## ADR-0014 — Database
Date: 2026-09-13 · Accepted 2026-09-16 · Status: **ACCEPTED** (option a)

**Options.** (a) Postgres. (b) Postgres + TimescaleDB for occupancy samples.
(c) SQLite.

**Decision.** (a), revisit (b) only if occupancy sample volume proves it. (c) is
tempting for a single box but poor under concurrent writers from several
pipelines. Accepted per the leaning below; no new evidence had arrived.

## ADR-0015 — Dashboard / review UI framework
Date: 2026-09-13 · Accepted 2026-09-17 · Status: **ACCEPTED** (option a)

**Options.** (a) Server-rendered templates + minimal JS. (b) React/Next SPA
against an API. (c) A low-code dashboard tool.

**Decision.** (a). FastAPI + Jinja2 + uvicorn, server-rendered, with roughly a
hundred lines of vanilla JS. All MIT or BSD-3.

**Reasoning.** (a) for the review queue, which is the UI that matters and is
mostly "a list, a video player, two buttons". (c) fails on the
video-with-clip-seek requirement. (b) is justified only if the management
dashboards grow, and it would trigger ADR-0017's "JS only if ADR-0015 lands on a
SPA". The deciding technical argument is not taste: `argus.store.db.Database`
wraps `psycopg.AsyncConnection` and `EventBus` is asyncio end to end. A
synchronous framework would mean either a duplicate synchronous database path or
`asyncio.run()` per request; FastAPI reuses `Database`, `Store` and `EventBus`
unchanged, and its typed parameters satisfy `disallow_untyped_defs` without
ceremony.

**Changes it.** Management dashboards growing past what server-rendered pages
handle comfortably.

**Consequences.** The UI reads stored rows and never recomputes derived values at
request time — a number on screen that disagreed with the stored evidence would
undermine the dispute path (ADR-0008). Enforced by a structural test.

## ADR-0016 — Deployment orchestration
Date: 2026-09-13 · Accepted 2026-09-16 · Status: **ACCEPTED** (option a)

**Options.** (a) docker compose + systemd on one box. (b) Kubernetes (k3s).
(c) Bare systemd units, no containers.

**Decision.** (a). One box, intermittent internet, no ops team. (b) is
unjustifiable at this scale. On macOS inference runs natively regardless
(`ARCHITECTURE.md` §5.4), so compose describes the Linux deployment, not the
dev loop. Accepted per the leaning below; no new evidence had arrived.

## ADR-0017 — Monorepo vs polyrepo, language boundaries, Python version
Date: 2026-09-13 · Accepted 2026-09-16 · Status: **ACCEPTED**

**Decision.** Monorepo; Python for pipelines and services; JS only if ADR-0015
lands on a SPA; no second backend language unless a measured bottleneck demands
it. **Python 3.12 pinned** — chosen by what `onnxruntime`/`onnxruntime-gpu` and
the CUDA stack support on Ubuntu 22.04/24.04, a constraint from below, not a
preference. Layout: uv workspace with members under `packages/` and `services/`,
all importing under the `argus.` namespace (`argus.store`, `argus.backends`, …)
so `packages/backends/` remains the only directory that imports inference
runtimes while imports stay unambiguous.

**Changes it.** A dependency that forces a split, or measured throughput that
needs a native component.

## ADR-0018 — Dependency management across platforms
Date: 2026-09-13 · Accepted 2026-09-16 · Status: **ACCEPTED** (option a)

**Context.** `onnxruntime` / `onnxruntime-gpu` / CoreML availability differ by
platform; CUDA-only packages do not install on macOS at all
(`ARCHITECTURE.md` §5.5).

**Options.** (a) uv with platform markers and extras. (b) Poetry with the same.
(c) Per-platform lock files.

**Decision.** (a). Extras `[cpu]`, `[cuda]`, `[macos]` with a lock that resolves
on both platforms; the `[cuda]` extra carries
`sys_platform == 'linux' and platform_machine == 'x86_64'` markers so the
CUDA-only wheel can never resolve on macOS. `onnxruntime` is never a base
dependency (it conflicts with `onnxruntime-gpu`), which is why a bare sync
yields a runnable-but-backendless tree. Accepted per the leaning below; no new
evidence had arrived.

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
Date: 2026-09-13 · Accepted 2026-09-17 · Status: **ACCEPTED** (option a)

**Context.** `ARCHITECTURE.md` §7.3 currently says any flagged interval in a day
zeroes the whole day's overage.

**Options.** (a) Zero the whole day (current, strictest fail-open). (b) Charge
clean intervals, flag the rest.

**Decision.** (a). Any interval in a local day that is not `RESOLVED` — and any
resolved interval missing a clip on either end — flags the day, and a flagged day
contributes exactly zero overage.

**Reasoning.** (a) until shadow-mode data shows how often it triggers. If it
fires on most days, the system is not actually measuring anything and (b) is not
the fix — better capture is. Closing this now rather than leaving it OPEN because
the pairing implementation has to encode one of the two, and an unrecorded choice
made in code is the thing `AGENTS.md` §3 exists to prevent.

**Changes it.** Measured flag rates during M9.

**Consequences.** The rule is **not** configurable. A `day_zeroing` policy field
would put `AGENTS.md` §2.2 semantics — which cases resolve to a deduction —
behind a YAML key. It is hard-coded in `argus.payroll.overage`, restated as a
database check constraint on `dwell_day`, and changing it is a diff a human
reads.

## ADR-0024 — Product name
Date: 2026-09-13 · Accepted 2026-09-17 · Status: **ACCEPTED**

**Context.** `PROJECT_NAME` was a placeholder throughout. The working directory
is `argus`, which is a directory name, not a decision. Worth a moment's thought
that "Argus" — the hundred-eyed watchman — names the surveillance reading of this
system rather than the measurement-and-audit reading we argue for in `SOUL.md`.
Naming is not neutral when a buyer's auditor reads the slide.

**Decision.** The product is **Sparrow Vision**. The scope of the name is
documents and product surfaces — the UI, reports, export artefacts. The code
namespace stays `argus.`: import paths, distribution names, the `ARGUS_` env
prefix, and the Postgres role are unchanged.

**Reasoning.** A rename of the import namespace touches every file for no
behavioural gain, and doing it inside a two-week window before a demonstration is
mechanical risk bought with nothing. The split costs one sentence of explanation
to a new reader and saves a day of churn.

**Changes it.** Nothing technical. A trademark problem would.

**Consequences.** `argus.` in code and `Sparrow Vision` in prose coexist
deliberately. A follow-up rename is possible later and is not scheduled.

## ADR-0025 — CI provider
Date: 2026-09-16 · Status: **ACCEPTED**

**Context.** The docs assume "Linux CI" throughout (lowercase-path check,
import-graph check, golden-frame parity suite, case-sensitive filesystem) but no
ADR ever named the provider. CI was assumed everywhere and decided nowhere.

**Options.** (a) GitHub Actions with Linux runners. (b) Self-hosted runner on
the staging box. (c) Another hosted CI.

**Decision.** (a). The repo lives on GitHub; hosted Linux runners give the
case-sensitive filesystem and container services (Postgres, mediamtx) the
integration tests need, at zero ops cost. (b) is deferred until the macOS parity
leg needs a home with a GPU (it does not have one — see ADR-0022) or hosted
runners become a constraint.

**Consequences.** The macOS leg of the parity suite remains un-homed in CI
(ADR-0022 open, leaning (b): developer-machine runs, recorded not enforced).

## ADR-0026 — Model artefact mechanism
Date: 2026-09-16 · Status: **ACCEPTED**

**Context.** `AGENTS.md` §4 requires large model binaries to stay out of git
history and the mechanism (LFS, pointer file plus fetch script, artefact store)
to be picked before the first model lands. The first model is landing now.

**Options.** (a) Git LFS. (b) Pointer registry plus fetch script with hash
verification. (c) An artefact store / model registry service.

**Decision.** (b). `models/registry.yaml` maps artefact name → (URL, sha256,
licence, size); `models/fetch.py` downloads and verifies; `.gitignore` excludes
the binaries. A committed hash is what the parity suite needs — comparing two
different model files proves nothing — and a fetch script is auditable and
dependency-free, unlike LFS (which also quietly changes clone behaviour for
everyone). (c) is operational machinery this single-box project does not need.

**Consequences.** First sync on a new machine is `uv run python models/fetch.py`
before backend tests will pass; tests skip loudly, not silently, when the
artefact is absent.

## ADR-0027 — Enrolment images are retained, unencrypted, for the demo roster
Date: 2026-09-17 · Status: **ACCEPTED** · Human sign-off: recorded 2026-09-17

**Context.** `ARCHITECTURE.md` §7.2 and `RISKS.md` §9 leave open
whether enrolment images are kept after embedding. Keeping them allows
re-embedding when the face model changes; discarding them shrinks the biometric
footprint. This is a retention decision about biometric data, so `AGENTS.md` §2.5
requires human sign-off, which was given.

**Options.** (a) Discard after embedding. (b) Retain, encrypted at rest.
(c) Retain in the clear.

**Decision.** (c), scoped tightly: 5–10 consenting team members in a private,
non-commercial test environment, images under `var/enrol/<person_id>/` (already
git-ignored, mode 700), with a deletion date carried on the `consent_record` and
enforced by `argus.enrol purge --expired`.

**Reasoning.** The face stack is expected to change during the build, and (a)
would mean gathering everyone back into a room each time. (b) is the right answer
and needs a key-management story this project does not have yet — inventing one
under demo pressure produces the appearance of protection rather than protection.
(c) is honest about what it is.

**Changes it.** Any enrolment of a person who is not a consenting member of the
team. Any move toward a pilot. Either makes (b) a prerequisite, not an
improvement.

**Consequences.** There are plaintext face images and plaintext embeddings on a
single box. That is acceptable only under the scope above and must not be carried
forward silently. `purge` deletes images and embedding together, because
`ARCHITECTURE.md` §9.1 warns that removing the video and leaving the embeddings
behind
is the easy mistake. Template encryption at rest remains unbuilt and is named in
`RISKS.md` §2.

## ADR-0028 — UI access control for the demo
Date: 2026-09-17 · Status: **OPEN**

**Context.** `RISKS.md` §10 defines four access tiers — viewer, reviewer,
payroll, admin — and `RISKS.md` §5 names casual clip browsing as
the misuse most likely to happen and least likely to be reported. Building real
per-person authentication does not fit the demo window.

**Options.** (a) Per-role passphrase plus a typed actor name, bound to localhost.
(b) Real per-person accounts with hashed credentials. (c) No authentication.

**Leaning:** (a), with the limitation written down rather than glossed: it
authenticates a *role*, not a *person*, and the actor name in the audit log is
self-asserted. (c) is refused outright — an unaudited clip-viewing path
contradicts the premise of the system (`ARCHITECTURE.md` §8).

**What is built regardless of this ADR:** every clip view writes a
`clip_access_log` row with actor, role, context and reason. The audit log is the
only control that touches the voyeurism threat, and it does not depend on the
authentication being good.

**Changes it.** Anyone outside the immediate team getting access; the pilot.

## ADR-0029 — PTZ cameras are used at fixed presets, with a drift check
Date: 2026-09-17 · Status: **ACCEPTED**

**Context.** The demo uses Imou pan-tilt cameras on doorways, while the doorway
pipeline depends on a door line that is a fixed configuration constant. A lens
that can move under a geometry that cannot is a latent contradiction, and
`RISKS.md` already names both halves: §5 "supervisor unplugs, reaims or
covers a camera" and §5 "camera knocked out of alignment by cleaning".

**Options.** (a) Fixed preset plus a reference-frame drift check. (b) Re-derive
the door line per frame from scene understanding. (c) Accept the risk.

**Decision.** (a). In each camera: auto-tracking, patrol and auto-home disabled,
the aimed position saved as preset 1 and as the power-on position, and RTSP
consumed through a **view-only account** that cannot drive PTZ even if the
credential leaks. In software: a 160×90 grayscale phase-correlation check against
a stored reference thumbnail every 30 s, hysteresis of three consecutive
failures.

**Reasoning.** (b) is a research project. (c) fails silently and in the worst
direction — crossings attributed to a door the camera is no longer pointing at.
`RISKS.md` §5 already proposed exactly this mitigation and called it cheap.

**Consequences.** On drift, the door pipeline **stops emitting doorway events**
and opens a `stream_gap` with cause `aim_changed`. A camera pointing elsewhere
means "we saw nothing", not "nothing happened" — which makes the fail-open
behaviour automatic rather than remembered. Recalibration is a deliberate,
logged command that renders the configured door line over a fresh frame for a
human to accept; an auto-updating reference would track the drift it exists to
detect.

## ADR-0030 — AGPL model weights for the non-commercial test environment
Date: 2026-09-17 · Status: **ACCEPTED** · Supersedes ADR-0011
Human sign-off: recorded 2026-09-17 (`AGENTS.md` §2.9)

**Context.** ADR-0011 restricted the project to permissively licensed models and
excluded Ultralytics/AGPL, on the premise of an eventual commercial deployment at
a factory. That premise does not currently hold: this is a personal,
non-commercial test environment, the garments deployment is not in scope, and the
bring-up detector chosen under that constraint (`ssd_mobilenet_v1`, 2018-era) is
markedly weaker than current models.

**Options.** (a) Keep ADR-0011 and accept 2018-era accuracy. (b) Permit AGPL
weights, scoped to non-commercial use. (c) Buy commercial licences now.

**Decision.** (b). Detection and pose come from the YOLO26 family
(`yolo26m.onnx`, `yolo26m-pose.onnx`, AGPL-3.0); face recognition from InsightFace
`antelopev2` (weights research-only). Both are recorded in
`models/registry.yaml` with their real licences.

**Reasoning.** The accuracy gap is large and the constraint that motivated
ADR-0011 does not currently apply. Two things keep this from becoming a trap.
First, the artefacts are consumed as **pre-exported ONNX** published by the
upstream project, so `ultralytics` is never imported and no AGPL *code* enters
the runtime — only the weights carry the licence. Second, every model sits behind
`packages/argus_backends`, which application code reaches only through the
registry, so replacing a model is a config change rather than a rewrite.

**The boundary, stated so it cannot be missed.** These weights **may not ship in
a commercial deployment**. Going commercial requires either an Ultralytics
commercial licence and a resolution of the InsightFace weights question, or a
swap to permissively licensed artefacts under ADR-0011's original reasoning.
`models/registry.yaml` carries `commercial_use: false` on every affected entry,
`models/fetch.py` warns on each, and a structural test requires every such
artefact to be named in this document — which is what stops the demo posture
quietly becoming the pilot posture.

**Changes it.** Any move toward commercial use, a paying customer, or deployment
at a real factory. Any of those reactivates ADR-0011.

**Consequences.** ADR-0011 is SUPERSEDED, not deleted; its reasoning is the
reasoning to return to. The backend abstraction becomes load-bearing for a
licensing reason as well as a portability one — if it erodes, the escape hatch
erodes with it.

## ADR-0031 — Pre-trigger buffer holds encoded packets; ingest reads two streams
Date: 2026-09-17 · Status: **ACCEPTED**

**Context.** The pre-trigger ring buffer holds decoded `rgb24` frames. At the
rig's 640×360 that is ~300 MB per camera and unremarkable. At the Imou fixed
lens's 2304×1296 it is **~4 GB per camera** — 12 GB for three cameras, before any
model loads, against a 16 GB target. `ARCHITECTURE.md` §5.8 says GPU memory is a
budget that must be measured; host memory turned out to be the binding one.

**Options.** (a) Keep decoded frames, shrink the window. (b) Buffer encoded
packets and decode on demand. (c) Write continuously to disk and cut clips from
files.

**Decision.** (b), plus a second stream per camera. The ring holds H.264 packets
(~23 MB total for three cameras at 15 s), and clips are produced by **remuxing**
those packets rather than re-encoding decoded frames. Separately, each camera is
opened twice: the **main** stream feeds the packet ring (evidence quality) and the
**substream** (~704×576) feeds the sampled decode that pipelines consume.

**Reasoning.** (a) trades away the pre-roll that violence clips exist for. (c) is
an NVR, which ADR-0009 already declined to build. (b) is ~100× smaller, produces
clips that are bit-identical to what the camera sent — which is what ADR-0008
auditability actually wants — and removes hardware decode from the critical path,
since nothing decodes the main stream continuously any more.

**Consequences.** Clips snap back to the last keyframe at or before the requested
start, so camera GOP length becomes an operational setting: I-frame interval is
set to 1× fps on every camera, and the rig generator pins `gop_size = 30`.
Eviction is GOP-aligned, with a byte cap alongside the duration bound. `pts`/`dts`
are preserved and rebased rather than derived from wallclock. Clip extraction
never materialises a list of decoded frames, and `max_clip_seconds` bounds the
request. The two-stream choice assumes cameras accept two concurrent RTSP
clients; if one refuses, it falls back to a single main-stream connection with
sampled decode and downscale (`RISKS.md` §6 names client-limit exhaustion
as a real failure).
