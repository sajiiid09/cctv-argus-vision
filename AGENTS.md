# AGENTS.md

Operating instructions for AI coding agents (and humans) working in this repo.
Absorbs the former `TESTING.md` and `DEV_SETUP.md`.

Read `SOUL.md` first. It is short, and it decides most arguments this document
would otherwise have to have.

**Current state, 2026-09-24.** M0–M2 are closed on the CPU and NVIDIA staging
legs. M3–M6 are built and tested **on rig footage**: the canteen path runs end
to end and writes doorway events, clips, pairing runs and payroll lines; the
gate, occupancy, violence trigger, enrolment CLI and operator console all
exist. On the RTX 4070 Ti workstation, the CUDA inference session, NVDEC probe,
native ingest path and SSD CUDA golden leg have passed. Five services: `ingest`,
`pairing`, `enrol`, `gate`, `ui`.

Three things that sound like features and are not, so nobody claims them:
identity is **off** (no face threshold has been measured, so every crossing is
`unknown`, which fails open to zero); the model artefacts for detection, pose
and faces are **unresolved** in `models/registry.yaml` and the rig runs on mock
backends; and the badge reader client has **never been spoken to**.

Outstanding and hardware-blocked: every sizing number, per-camera GOP and the
two-concurrent-client check, live ZKT taps, real-camera validation, face
thresholds, and demo rehearsal. Update this paragraph when it stops being true.

**This deployment is a personal, non-commercial test environment.** ADR-0030
permits AGPL and research-only model weights on that basis and states the
boundary: they may not ship commercially.

---

## 1. The one distinction that matters

Code here falls into two categories, worked on differently.

**Payroll-affecting.** Canteen doorway events, the pairing state machine, dwell
and overage, the payroll export, the shadow-mode flag, the clock/timezone
handling they depend on, and the retention rules protecting their clips.

**Everything else.** Occupancy, violence detection, dashboards, ingest plumbing,
tooling.

If you cannot tell which you are in, **stop and find out**. Do not guess.

| | Payroll-affecting | Everything else |
|---|---|---|
| Tests | Every branch; property tests over the state machine; fixed clock | Normal coverage |
| Review | Human reads the diff before merge | Normal |
| Changing behaviour | ADR required | Not required |
| Ambiguity | Must resolve to zero deduction + flag | Use judgement |
| Refactors | Behaviour-preserving only, proven by tests, separate commit | Normal |

## 2. Human sign-off — required, no exceptions

Agents do not do these autonomously. Stop, state what you propose, wait for an
answer. The team is one engineer plus agents, so these gates are kept few and
cheap enough to actually be used.

1. **Turning on the payroll export flag** (leaving shadow mode), or any change
   that makes export possible without it.
2. **Changing the pairing state machine's semantics** — which cases resolve to a
   deduction, thresholds, allowance windows, day-boundary attribution.
3. **Connecting occupancy data to a person or to pay**, including a schema change
   that creates the join path.
4. **Adding automated action to the violence pipeline** — notification, naming,
   incident creation. The output is a human review queue. That is the design.
5. **Changing retention or deletion** of clips, events or biometric templates.
6. **Moving biometric data or clips off the site**, including to a cloud GPU, an
   error tracker, a log aggregator or a model vendor's API.
7. **Adding cross-camera re-identification or floor-wide tracking** (ADR-0002).
8. **Changing anything asserted in `SOUL.md`.**
9. **Adding a dependency whose licence restricts commercial deployment.** This
   gate was exercised on 2026-09-17: ADR-0030 permits AGPL model weights for the
   non-commercial test environment. The gate moved, it did not retire — anything
   heading for commercial use reactivates ADR-0011's reasoning.

A pattern that looks like an exception and is not: "just for the demo". Demo
pressure is the most common reason these gates get skipped, and a demo that
deducts real pay from real people is the exact failure this project exists to
avoid.

## 3. What needs an ADR before changing

An ADR (`DECISIONS.md`) is needed to change any ACCEPTED decision and to close
any OPEN one. In particular: scope (0001), doorway-only identity (0002), two
separate numbers (0003), fail-open (0004), shadow mode (0005), the
no-write-to-payroll boundary (0006), auditability (0008).

Closing an OPEN ADR is a decision — record it, do not just start using the thing.
Write the ADR *before* the code. An ADR written afterwards is a justification,
not a decision.

## 4. Directory conventions

```
services/        long-running processes (ingest, analysis, gate, ui)
packages/        shared libraries
  argus_backends/   inference backends — the ONLY place a runtime is imported
  argus_pipelines/  gate, canteen, occupancy, violence
  argus_payroll/    pairing, overage — the high-care code, isolated
  argus_store/      event store, clip store, event bus
  argus_common/     config, clock
infra/           compose files, dockerfiles, mediamtx config
rig/             virtual camera rig: footage manifests, serving, fault injection
tests/golden/    golden frames and reference outputs
models/          ONNX artefacts referenced by hash, never committed
```

The documents stay at the repository root. Every document cross-references root
paths; moving them is churn with no payoff.

Rules:

- **All code paths lowercase with underscores.** The root `.md` files are exempt.
  A Mac is case-insensitive and the server is not; `models/Face.onnx` works on one
  and not the other. Enforced by a structural test on Linux, not by care.
- **`argus_payroll` imports no vision code, no database and no wall clock.**
  Pairing and overage are pure functions over stored events. Keeping
  probabilistic code out of wage arithmetic is what makes the wage arithmetic
  testable.
- **`argus_backends` is the only place** `onnxruntime`, `torch`, TensorRT or a
  vendor SDK may be imported. Application code asks the registry for a detector.
  This abstraction is load-bearing twice over: it keeps platforms interchangeable
  and it keeps a licensed model swappable (ADR-0030).
- **Exactly one onnxruntime distribution per environment.** `onnxruntime` and
  `onnxruntime-gpu` unpack into the same directory and overwrite each other;
  removing one can delete the directory the other needs. Use `--group cpu` or
  `--group staging`, never both. `argus.backends.onnx_common` refuses to build a
  session if it finds two.
- **Model binaries never enter git history** (ADR-0026): `models/registry.yaml`
  maps artefact → url, sha256 and licence; `models/fetch.py` verifies.

## 5. Commands

Same on every platform.

```bash
uv sync --all-packages --group cpu      # dev box and CI
uv sync --all-packages --group staging  # NVIDIA box instead — never both
uv run pytest -q                        # rig/store/golden skip loudly without services
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run python models/fetch.py           # fetch + hash-verify artefacts (ADR-0026)
docker compose -f infra/compose.dev.yaml up -d   # Postgres + mediamtx
uv run python rig/synthetic/generate.py # deterministic footage + manifests
uv run python rig/bin/rig_serve.py      # serve the virtual cameras
uv run python rig/bin/rig_rigctl.py status|pause|resume|stop|start <stream>
uv run python -m argus.ingest --config config/dev.yaml
uv run python -m argus.ingest --config config/rig_canteen.yaml   # + canteen analysis (mock detector)
uv run python -m argus.pairing --config config/dev.yaml --once --day 2026-09-18
uv run python -m argus.pairing.report --config config/dev.yaml --day 2026-09-18
uv run python -m argus.gate --config config/dev.yaml --once --badge B-1
uv run python -m argus.ui --config config/dev.yaml       # 127.0.0.1:8080 (ADR-0028)
uv run argus-enrol --config config/dev.yaml list
```

Credentials are never in the YAML: values may contain `${VAR}`, resolved from
the environment or `config/secrets.env` (mode 600 or loading refuses, and
git-ignored). `config/secrets.env.example` lists the names. The camera rows keep
the **un-expanded** template, and the schema has a CHECK that refuses a
credential-bearing URI — so a password cannot reach a row, a log, or a diff.

Selecting tests — markers gate on external dependencies, so use them rather than
paths:

```bash
uv run pytest -m "not rig and not postgres"   # CI's fast job; no docker needed
uv run pytest -m golden                       # needs models/fetch.py to have run
uv run pytest tests/payroll -q                # the high-care tier
ARGUS_PARITY_BACKEND=onnx-cuda uv run pytest -m golden     # the CUDA leg
ARGUS_PARITY_BACKEND=onnx-coreml uv run pytest -m golden   # the CoreML leg (ADR-0022, on demand)
```

The `reader` marker holds the one test that needs a badge reader on the LAN.
Nothing selects it, and that is the point: the command exists before somebody is
standing next to the hardware.

Escape hatches: `ARGUS_PG_HOST_PORT=5434` when 5432 is busy — a native Postgres
on the dev Mac takes it, so every command there needs this — with
`ARGUS_DATABASE__DSN` / `ARGUS_TEST_DSN` pointed at the same port;
`ARGUS_MODELS_ROOT` when artefacts are not under `./models`.

Iterating on a migration means `docker compose -f infra/compose.dev.yaml down -v`,
never editing an applied file: `apply_migrations` pins each file's sha256 and
refuses a changed one.

## 6. Environments

**The platform reality.** macOS dev is arm64 with CoreML and **cannot** reach the
GPU from Docker; Ubuntu staging is x86_64 with NVIDIA. The intended production
layout is containerised, but the verified workstation path is native analysis
with infrastructure in Docker; the GPU-capable ingest image is also smoke-tested,
while the remaining service Compose definitions are follow-up work. So on a Mac
you run a permanent hybrid: infrastructure in Docker, analysis natively. That is
the platform, not a workaround to fix later. There is also a GPU-less Linux box
that runs the same tree on the CPU reference backend; it proves logic, not CoreML
and not CUDA.

**The Linux box is the arbiter.** macOS proves logic; Linux proves the system. A
change is not done because it works on the Mac.

**Containers invoke the same entrypoint** a developer runs natively. If a
container needs a different command to work, the difference is a bug, not a
platform quirk. Configuration comes from the same file/env mechanism everywhere;
no ad-hoc command-line arguments that exist only on someone's laptop.

**NVIDIA box bring-up.** The native path is verified on the RTX workstation:
CUDA 13 runtime/cuDNN 9, NVIDIA Container Toolkit, hardware NVDEC, and the
pinned SSD CUDA golden leg all passed. The container check remains:

```bash
docker run --rm --gpus all nvidia/cuda:13.0.0-base-ubuntu24.04 nvidia-smi
```

That proves driver visibility, not application CUDA loading; the post-sync ORT
session is still the real check. `config/staging_canteen.yaml` has been
observed using `decode: nvidia`; `config/staging.yaml` remains the
container-oriented path. The ingest image now carries the matching CUDA 13 and
cuDNN 9 runtime and its four-stream Docker smoke test passed, but the Linux
Compose file still covers infrastructure plus ingest only, not every service.
A first TensorRT engine build, if ADR-0021 lands that way, is cached and can
take minutes — do not read a slow first start as a hang.

**Timezone.** `timedatectl set-timezone Asia/Dhaka` is display only. The
application stores UTC and reads the policy timezone from config. A server left
on UTC must not silently shift every pay-period boundary by six hours.

**Cross-platform hygiene.** Lowercase paths; LF endings via `.gitattributes`;
never format numbers or parse dates with the ambient locale; do not depend on
file watchers in the runtime path (FSEvents and inotify differ, and inotify
limits bite inside containers); log backend and model hash on every startup,
because the first question in any "why do the numbers differ" conversation is
which backend each side ran.

## 7. Testing

The governing idea: **the tests are asymmetric because the consequences are.** A
bug in the occupancy dashboard is embarrassing. A bug in the pairing state
machine takes money from someone who cannot easily contest it.

| Tier | What | Bar |
|---|---|---|
| **Payroll-affecting** | Pairing, dwell, overage, export, the clock code they depend on | Every case in `ARCHITECTURE.md` §7; property tests; fixed clocks; human review of diffs |
| **Vision** | Detection, pose, embedding, classification | Golden-frame parity within tolerance; no accuracy claims from synthetic footage |
| **Pipeline** | Ingest, clipping, reconnection, fault behaviour | Integration tests over RTSP from the rig, including faults |
| **Everything else** | Dashboards, tooling, review UI | Normal coverage |

**Payroll.** Every row of the `ARCHITECTURE.md` §7 case table is a test; adding a
row means adding a test in the same commit. Property tests matter more than the
examples, because examples only prove the cases someone thought of:

1. No sequence of events produces non-zero overage in a non-`RESOLVED` state.
   *This is the fail-open guarantee. If it fails, stop and fix it before
   anything else.*
2. Overage is never negative and never exceeds the day's measured dwell.
3. A `stream_gap` overlapping an interval forces zero for that day.
4. Recomputing over the same inputs yields identical output.
5. A late-arriving event cannot create a charge on an already-computed old day.
6. No event ordering produces an interval with `end < start`.
7. Output is invariant under input permutation — out-of-order arrival is the
   real case, and this is stronger than (6).
8. Every day that contributes a charge is fully auditable — clips on both ends.

Fixed clocks throughout; the policy timezone is configuration; `Asia/Dhaka`
boundary cases are tested explicitly. There is no DST in Bangladesh, which
removes one bug class and tempts people into shortcuts that break the moment
someone assumes UTC.

**Structural tests** enforce architecture rather than behaviour, because these
properties erode quietly: no backend runtime imported outside `argus_backends`;
`argus_payroll` importing no vision, database or property-testing code; no
wall-clock call in payroll; no path from occupancy storage to a person or a
payroll table; lowercase paths; a committed golden reference naming its artefact
hash; rig manifests recording licence and consent.

Two traps when editing payroll: the wall-clock check is a **raw substring scan
of the file text, comments included**, so a comment quoting the banned call fails
the test enforcing it. And the banned-import list includes the database and
Hypothesis, because payroll must be importable and testable with nothing running.

**Never run two pytest sessions against the rig at once.** They share mediamtx
and the publisher pidfiles, so the second kills the first's streams and both
fail in ways that look like real bugs: 404s from mediamtx, stalls that never
stall, clips that will not extract. If the rig tier starts failing for no
reason, check for a leftover `ffmpeg` and a stale pidfile before reading the
code.

**The rig's publishers are tracked by pidfile**, and "is this pid alive?" is
subtler than it looks: a killed-but-unreaped ffmpeg is a zombie and answers
`kill(pid, 0)` happily. Believing one is alive is not cosmetic -- fault
injection then signals a corpse, the stream never stalls, and the test meant to
prove stall detection fails with no hint why. The check reads the process state
from `/proc` on Linux and from `ps` elsewhere, and confirms the command is still
ffmpeg in case the pid was recycled.

**Video tests go through the rig over RTSP**, never by reading frames from disk.
Reading files bypasses decode, reconnection and timing, which is where the bugs
are. Fault injection is the valuable part and the part most likely to be skipped
under time pressure: stream drop, stall (connection open, no frames — the WiFi
failure mode, nastier than a clean drop), resolution change mid-stream, latency,
and a DVR refusing a connection. Each has an expected behaviour and a test. The
invariant behind all of them: **a gap is always recorded**, because "we saw
nothing" and "nothing happened" must never be confused downstream.

**Golden-frame parity** makes backend divergence visible and bounded, not zero.
Tolerances start from `ARCHITECTURE.md` §5 and are widened only with a recorded
reason in the commit message — a widened tolerance is a decision to care less and
should read like one. Changing models means re-measuring them rather than
carrying them over.

**What we cannot test yet**, stated rather than papered over: real-world
accuracy (any number produced before a site pilot describes the rig), throughput
at the real camera count, violence performance, long-run stability, and the macOS
GPU leg in CI (ADR-0022). Two more, added as the code arrived: the **badge
reader client** has never exchanged a byte with a device — its tests parse
fixtures hand-constructed from the protocol description, and the one test that
needs hardware carries the `reader` marker that nothing selects — and **no face
threshold exists**, so identity is off and every crossing is `unknown`, which is
the fail-open outcome rather than a missing feature. `ARCHITECTURE.md` §9 lists
what cannot be validated without real cameras at all. Offering those lists
unprompted is much better than being asked for them.

## 8. Working style

- **Write down uncertainty.** "We have not decided" is a complete sentence here.
  A confident invention is worse than an acknowledged gap, because it is
  invisible in review.
- **Do not smooth over gaps.** `PROVISIONAL`, `UNVERIFIED` and `OPEN` are
  load-bearing. Removing one claims you verified something — so verify it, or
  leave it.
- **No synthetic accuracy claims.** Numbers from the rig describe the rig.
- **Do not design around the demo.** The demo is a milestone, not the product.
- **Prefer deleting a feature to weakening a guarantee.** If occupancy is too
  noisy to be useful, ship less occupancy — do not make it useful by tying it to
  something it should not touch.
- **Small diffs in payroll code.** A 600-line refactor of pairing cannot be
  reviewed by a tired human at 11pm, which is when it will be reviewed.
- **When blocked on a decision, propose and stop.** Do not pick a stack, a model
  or a threshold to keep moving. State the options, state a leaning, wait.

## 9. Things that look helpful and are not

- Adding a "best guess" for an unpaired event.
- Adding a confidence-weighted partial deduction.
- Caching a person's last known location "to help with pairing" — that is re-ID
  with a friendlier name.
- Logging clips or face crops to an error tracker for debugging.
- Backfilling missing events from a schedule or an average.
- Auto-dismissing low-score violence candidates. The queue is the product; an
  empty queue is not a success metric.
- Renaming `unknown` to something that sorts better next to real identities.
