# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read these first

**`AGENTS.md`** — operating instructions for agents in this repo: what needs
human sign-off, what needs an ADR, directory and testing conventions, and the
distinction between payroll-affecting code and everything else.

**`SOUL.md`** — the rules that settle arguments. Short.

`README.md` lists the rest of the document set.

## Status

**M0–M2 closed on the CPU leg; M3–M6 built and tested, on rig footage only.**
The canteen path runs end to end — RTSP, decode, detect, track, cross, clip,
write, pair, report, review — and the rig replay finds nine of the manifest's ten
labelled crossings with every direction correct. Services: `ingest` (streams +
canteen and floor pipelines), `pairing` (the runner around `argus.payroll`, plus
`argus.pairing.report`), `enrol`, `gate`, `ui`.

What that does **not** mean:

- **Identity is off.** No face threshold has been measured, so `face.enabled` is
  false, every crossing is `unknown`, and unknown fails open to zero (ADR-0010).
- **Most model artefacts are unresolved.** `yolo26m` was pinned on 2026-09-25
  (box-local `file://` url; golden 7/7 on CPU and CUDA, `DECISIONS.md`
  verification record), but no pipeline config selects it yet. `yolo26m_pose`,
  `scrfd_10g_bnkps` and `glintr100` are still declared with no pinned url or
  sha256; their wrappers are tested against synthetic session outputs only. The
  rig runs on the mock backends.
- **The badge reader has never been spoken to.** `ZktTapSource` is written,
  `SimulatedTapSource` is the default.
- **Every number describes the rig**, which draws people as bright disks
  (`ARCHITECTURE.md` §9.2).

Remaining work needs the NVIDIA box or real cameras: CUDA parity (open M2 exit),
NVDEC (open M1 exit), all sizing numbers, per-camera GOP and the
two-concurrent-client check, live ZKT taps, face thresholds, demo rehearsal.

This deployment is a **personal, non-commercial test environment**: ADR-0030
permits AGPL and research-only model weights on that basis and records the
commercial boundary. Open ADRs: 0020, 0021 (both blocked on the M8 site survey).

## The one thing to get right

Some code paths can reduce a worker's pay (canteen pairing, dwell, overage,
export). They are held to a higher standard than everything else: full case
coverage, property tests, human review, ADR before behaviour changes. If you
cannot tell whether you are in one, stop and find out — do not guess.
`AGENTS.md` §1.

## Commands

Same on every platform (`AGENTS.md` §5).

```bash
uv sync --all-packages --group cpu       # dev env. `--group staging` on the NVIDIA box — NEVER both
uv run pytest -q                         # rig/store/golden tests skip loudly without services
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run python models/fetch.py            # fetch + sha256-verify model artefacts (ADR-0026)
docker compose -f infra/compose.dev.yaml up -d   # Postgres + mediamtx
uv run python rig/synthetic/generate.py  # deterministic rig footage + manifests (video is git-ignored)
uv run python rig/bin/rig_serve.py       # serve the virtual cameras over RTSP
uv run python rig/bin/rig_rigctl.py status|pause|resume|stop|start <stream>   # fault injection
uv run python -m argus.ingest --config config/dev.yaml
uv run python -m argus.ingest --config config/rig_canteen.yaml   # + the canteen pipeline, mock detector
uv run python -m argus.pairing --config config/dev.yaml --once --day 2026-09-18
uv run python -m argus.pairing.report --config config/dev.yaml --day 2026-09-18
uv run python -m argus.gate --config config/dev.yaml --once --badge B-1
uv run python -m argus.ui --config config/dev.yaml     # console on 127.0.0.1:8080 (ADR-0028)
uv run argus-enrol --config config/dev.yaml list
```

Selecting tests — markers are declared in `pyproject.toml` and gate on external
dependencies, so use them instead of skipping by path:

```bash
uv run pytest -m "not rig and not postgres"        # what CI's fast job runs; no docker needed
uv run pytest -m golden                            # needs models/fetch.py to have run
ARGUS_PARITY_BACKEND=onnx-coreml uv run pytest -m golden   # the CoreML leg, on demand (ADR-0022)
uv run pytest tests/unit/test_streams.py::test_name -v    # one test
uv run pytest -k pairing -v                        # one test by name substring
```

`asyncio_mode = "auto"` — async tests need no decorator, and the Postgres/rig
fixtures are session-scoped. The `reader` marker holds the one test that needs a
badge reader on the LAN; nothing selects it.

Environment escape hatches: `ARGUS_PG_HOST_PORT=5434` (host 5432 busy — a native
Postgres on the dev Mac takes it, so every command there needs this) with
`ARGUS_DATABASE__DSN` / `ARGUS_TEST_DSN` pointed at the same port;
`ARGUS_MODELS_ROOT` when model artefacts are not under `./models`.

Credentials live in `config/secrets.env` (mode 600, git-ignored) and reach the
YAML as `${VAR}`; `config/secrets.env.example` lists the names.

## Architecture

A `uv` workspace (`pyproject.toml` `[tool.uv.workspace]`) of distributions under
`packages/*` and `services/*`, all publishing into the **`argus.` namespace**:
the distribution `argus-store` lives at `packages/argus_store/src/argus/store/`.
Directory name, distribution name and import path differ by one separator each —
`import argus.store`, not `import argus_store`.

- `packages/argus_common` — `config.py` (YAML + `ARGUS_SECTION__KEY` env
  overrides, type-coerced; validation lives in dataclass `__post_init__`) and
  `clock.py` (`Clock` protocol, injected so time is an input).
- `packages/argus_backends` — the **only** place `onnxruntime`/`torch`/TensorRT
  may be imported. `interfaces.py` defines five narrow Protocols (`Detector`,
  `FaceDetector`, `PoseEstimator`, `FaceEmbedder`, `ClipClassifier` — the last
  registered by nothing, because ADR-0012 permits a trigger and a human, not a
  classifier). `registry.py` is a `(kind, name)` registry selecting by
  *capability probe*, never a platform check, in preference order
  `onnx-cuda → onnx-coreml → onnx-cpu`, and logs `kind= backend= model=` on
  construction. Application code calls `get_detector()` / `get_face_embedder()`;
  a forced name (`get_detector("onnx-cpu")`) is the "does this repro on CPU?"
  lever, and `"mock"` is selectable for every kind but never preferred.
- `packages/argus_store` — Postgres access (`db.py` migrations from
  `schema/*.sql`, `store.py` typed writes) and `bus.py`. **Postgres is the bus**
  (ADR-0013): every doorway-event and stream-gap write NOTIFYs `argus_events`;
  frames never travel over it.
- `packages/argus_payroll` — the pairing state machine, dwell and overage. Pure
  functions over its own frozen types, importing no vision code, no database and
  no wall clock. `run_pairing` is the only entry point. There is no export, and
  a test asserts its absence. **Read `pairing.py`'s module docstring before
  changing anything here** — the walk is per (person, space) and *not* per day,
  for a reason that looks like a bug if you do not know it.
- `packages/argus_pipelines` — decoded frames to evidence. `base.py` (Protocols
  the ingest service satisfies structurally, plus `GapKeeper`: a pipeline that
  stops analysing opens a gap), `runtime.py` (one thread per model session;
  drops the oldest frame and counts it), `tracking.py` (geometry only, no
  appearance features, ids never stored — ADR-0002), `doorway.py` (**read the
  sign convention**: for a line drawn top-to-bottom, inside is east), `faces.py`
  (quality gate, best-of-K, threshold *and* margin, no default thresholds),
  `aim.py` (ADR-0029 drift check), `canteen.py` (the only writer of doorway
  events), `occupancy.py`, `violence.py`, `gate/` (stdlib-only tap contract,
  the four-way outcome, the ZKT client), `metrics.py`.
- `services/pairing` — the orchestrator around `argus.payroll`, which may not
  import a database: `load.py` reads evidence (space from the camera join, gaps
  per space, open gaps kept, a lead-in window so a boundary cannot decide who is
  charged), `persist.py` writes a run in one transaction with the NOTIFY last
  and inside it, `report.py` prints the `RISKS.md` §8 metrics.
- `services/enrol` — the only path by which a face template exists. `--by` is
  required everywhere and has no default; consent is enforced twice; `purge
  --expired` takes templates and images together.
- `services/gate` — badge taps, the four-way outcome, `reader_gap`. The tap is
  written before verification is attempted.
- `services/ui` — the console. Renders stored rows, imports no `argus.payroll`
  (structural test), logs every clip view including refusals.
- `services/ingest` — `streams.py` (RTSP connect, reconnect-with-backoff, stall
  detection, `stream_gap` rows; buffers **encoded packets** and decodes only at
  `analysis_fps`), `ringbuffer.py` (`PacketRingBuffer` for evidence,
  `RingBuffer` for the short decoded tap), `clip.py` (remux first, decode+encode
  fallback), `main.py`.
- `rig/` — the virtual camera rig: `synthetic/generate.py` produces deterministic
  footage, `bin/rig_serve.py` publishes it to mediamtx over RTSP, `bin/rig_rigctl.py`
  injects faults. Tests consume video **through RTSP**, never from disk, because
  decode/reconnect/timing is where the bugs are. `rig/bin/*` scripts bootstrap
  `sys.path` and are exempt from isort (`pyproject.toml` per-file-ignores).

Three invariants that shape the code more than anything else:

1. **Identity only at doorways** (ADR-0002). No cross-camera tracking or re-ID.
2. **The server clock is authoritative.** `ts_server` is used in arithmetic;
   `ts_media` and any camera-reported time are recorded for audit and never
   computed with. A gap means "we saw nothing", which stays distinguishable from
   "nothing happened" downstream.
3. **Clips are remuxed, not re-encoded** (ADR-0031). The pre-trigger buffer holds
   H.264 packets, so eviction and extraction are GOP-aligned and a clip always
   starts at a keyframe. Measured on PyAV 18: only `add_stream_from_template`
   works for this — `add_mux_stream` and a rebuilt encoder stream are both
   rejected by the mp4 muxer — so the fast path needs a live session and there is
   a decode+encode fallback for when there is not.

## Structural tests are the architecture

`tests/structural/test_structure.py` and `tests/structural/test_sql_contracts.py`
enforce, as failing tests, things review would otherwise have to catch: no
backend runtime imported outside `packages/argus_backends`, no vision imports in
`argus/payroll`, no wall-clock call in `argus/payroll`, all code paths lowercase
(the dev Mac is case-insensitive, the production box is not), golden reference
committed with its artefact hash, rig manifests carrying `licence:` and
`consent:`, `argus.pipelines` importing no payroll, the console importing no
payroll, one writer of derived payroll rows, the reader protocol in exactly one
module, no module reading both face thresholds, no auto-dismissal, no export
anywhere, every non-commercial artefact named in `DECISIONS.md`, and the
enum↔SQL-CHECK vocabularies agreeing in both directions. If a change
makes one of these fail, the change is wrong — do not relax the test.

Two traps worth knowing before you edit anything under `argus/payroll`: the
wall-clock check is a **raw substring scan of the file text, comments and
docstrings included**, so a comment saying "never call `datetime.now` here" fails
the test that enforces not calling it. And the banned-import list now includes
`psycopg`, `argus.store` and `hypothesis` — payroll must be importable and
testable with no database at all.
