# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read these first

**`AGENTS.md`** — operating instructions for agents in this repo: what needs
human sign-off, what needs an ADR, directory and testing conventions, and the
distinction between payroll-affecting code and everything else.

**`SOUL.md`** — the rules that settle arguments. Short.

`README.md` lists the rest of the document set.

## Status

**M0 closed, M1 and M2 implemented; the Linux/CPU leg is verified.** M3+ is in
progress against a two-week demo deadline (`PLAN.md`, and the plan file the work
is following). The pairing state machine in `packages/argus_payroll` is **built
and tested** — full `ARCHITECTURE.md` §7.3 case table plus eight property tests —
but nothing produces doorway events yet, so it runs on no real data.

This deployment is a **personal, non-commercial test environment**: ADR-0030
permits AGPL and research-only model weights on that basis and records the
commercial boundary. Open ADRs: 0019, 0020, 0021, 0022, 0028.

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
```

Selecting tests — markers are declared in `pyproject.toml` and gate on external
dependencies, so use them instead of skipping by path:

```bash
uv run pytest -m "not rig and not postgres"        # what CI's fast job runs; no docker needed
uv run pytest -m golden                            # needs models/fetch.py to have run
uv run pytest tests/unit/test_streams.py::test_name -v    # one test
uv run pytest -k pairing -v                        # one test by name substring
```

`asyncio_mode = "auto"` — async tests need no decorator, and the Postgres/rig
fixtures are session-scoped.

Environment escape hatches: `ARGUS_PG_HOST_PORT=5434` (host 5432 busy) with
`ARGUS_DATABASE__DSN` / `ARGUS_TEST_DSN` pointed at the same port;
`ARGUS_MODELS_ROOT` when model artefacts are not under `./models`.

CI (`.github/workflows/ci.yml`) runs lint + mypy + the no-services tests on one
job, then the full suite plus an ingest smoke test against real Postgres and
mediamtx containers on another. Both on Linux — `AGENTS.md` §5: the Linux box is
the arbiter.

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
  may be imported. `interfaces.py` defines four narrow Protocols (`Detector`,
  `PoseEstimator`, `FaceEmbedder`, `ClipClassifier`); `registry.py` selects by
  *capability probe*, never a platform check, in preference order
  `onnx-cuda → onnx-coreml → onnx-cpu`, and logs `backend=… model=…` on
  construction. Application code calls `get_detector()`; a forced name
  (`get_detector("onnx-cpu")`) is the "does this repro on CPU?" lever.
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
- `packages/argus_pipelines` — still a skeleton; the layer that will connect
  decoded frames to the registry and write doorway events.
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

`tests/structural/test_structure.py` enforces, as failing tests, things review
would otherwise have to catch: no backend runtime imported outside
`packages/argus_backends`, no vision imports in `argus/payroll`, no wall-clock
call in `argus/payroll`, all code paths lowercase (the dev Mac is
case-insensitive, the production box is not), golden reference committed with its
artefact hash, rig manifests carrying `licence:` and `consent:`. If a change
makes one of these fail, the change is wrong — do not relax the test.

Two traps worth knowing before you edit anything under `argus/payroll`: the
wall-clock check is a **raw substring scan of the file text, comments and
docstrings included**, so a comment saying "never call `datetime.now` here" fails
the test that enforces not calling it. And the banned-import list now includes
`psycopg`, `argus.store` and `hypothesis` — payroll must be importable and
testable with no database at all.
