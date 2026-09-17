# DEV_SETUP.md

Getting a working development environment.

> **Status 2026-09-16:** the Linux/CPU path below has been **executed and
> verified** on a GPU-less Linux x86_64 box (Docker 29, containers for
> Postgres + mediamtx, native `uv` environment): full test suite, lint,
> typecheck, and an end-to-end ingest smoke with fault injection (stall,
> kill, recover) all pass. The macOS sections remain **UNVERIFIED** until
> someone runs them on the Mac. The NVIDIA decode path (`decode: nvidia`) is
> **UNVERIFIED** until exercised on the RTX staging box.
>
> Note the third machine: the GPU-less Linux dev box runs the same tree with
> the CPU reference backend. It proves logic, not CoreML, not CUDA
> (`ARCHITECTURE.md` §5.7).

---

## 1. The two-platform reality

| | macOS dev | Ubuntu staging/prod |
|---|---|---|
| Arch | Apple Silicon arm64 | x86_64 |
| Accelerator | Apple GPU / ANE via CoreML | NVIDIA via CUDA/TensorRT |
| GPU in Docker | **Impossible** | Yes, NVIDIA Container Toolkit |
| Decode | VideoToolbox | NVDEC / VAAPI |
| Filesystem | APFS, case-**insensitive** | ext4, case-**sensitive** |
| Inference runs | **Natively** | In containers |
| Infra (DB, broker, mediamtx) | In containers | In containers |

The consequence: on macOS you run a hybrid — containerised infrastructure,
native Python for anything touching the GPU. This is not a temporary workaround.
Docker Desktop cannot pass through the Apple GPU, and CPU-only inference in a
container is slow enough that what you learn from it is misleading. Plan for the
hybrid; do not keep trying to fix it.

---

## 2. Linux dev box (any x86_64/arm64 box, GPU optional) — VERIFIED 2026-09-16

```bash
# uv (installs the pinned Python 3.12 via .python-version)
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --all-packages --group cpu   # the `cpu` group carries onnxruntime

# infrastructure in containers (Postgres + mediamtx)
docker compose -f infra/compose.dev.yaml up -d
# If host port 5432 is taken: ARGUS_PG_HOST_PORT=5434 docker compose ... up -d
# and export ARGUS_DATABASE__DSN=postgresql://argus:argus@localhost:5434/argus

# model artefact (ADR-0026: fetch + sha256 verify, never committed)
uv run python models/fetch.py

# rig footage + manifests (deterministic; manifests are committed, video is not)
uv run python rig/synthetic/generate.py

# serve the virtual cameras, then run the stream manager against them
uv run python rig/bin/rig_serve.py &
uv run python -m argus.ingest --config config/dev.yaml
```

Fault injection while it runs (TESTING.md §4 faults, each with a test):

```bash
uv run python rig/bin/rig_rigctl.py status
uv run python rig/bin/rig_rigctl.py pause canteen_door_01   # WiFi stall
uv run python rig/bin/rig_rigctl.py resume canteen_door_01
uv run python rig/bin/rig_rigctl.py stop canteen_door_01    # camera unplugged
uv run python rig/bin/rig_rigctl.py start canteen_door_01
```

Tests: `uv run pytest -q` (rig/store tests skip loudly if docker services are
down). Lint/type: `uv run ruff check . && uv run mypy`.

## 3. macOS setup — UNVERIFIED

### 3.1 Base tools

```bash
# Homebrew, if not present: https://brew.sh
brew install git ffmpeg
brew install --cask docker        # Docker Desktop
```

`ffmpeg` from Homebrew includes VideoToolbox support. It does **not** include
NVDEC, obviously, and code must not assume a specific hardware decoder is
available — probe, and fall back to software decode.

### 3.2 Python — VERIFIED on Linux; the uv layout is the same on macOS

Python 3.12 pinned (ADR-0017); uv with workspace members and platform groups
(ADR-0018):

```bash
uv sync --all-packages --group cpu   # the `cpu` group installs onnxruntime (CoreML EP
                              # ships in the standard macOS onnxruntime wheel)
```

CUDA-only packages (`onnxruntime-gpu`, TensorRT bindings, `nvidia-*`) **do not
install on macOS at all**; they live in the `staging` group with Linux x86_64
markers, which is how the lock expresses "this wheel only on Linux x86_64".

### 3.3 Infrastructure in Docker — same as Linux (§2)

`/dev/shm` is sized explicitly in the compose files where it matters.

### 3.4 Running analysis natively — UNVERIFIED on macOS

```bash
uv run python -m argus.ingest --config config/dev.yaml
```

Backends are selected by capability probe, not by a platform flag. On a Mac the
detector resolves to `onnx-coreml`; the process logs which backend and model
hash it chose on startup. If it silently picks CPU when CoreML was expected,
that is worth chasing immediately.

## 4. Ubuntu staging (RTX 3060/4060) — UNVERIFIED

### 4.1 Driver and container toolkit

```bash
sudo ubuntu-drivers install
# NVIDIA Container Toolkit — follow NVIDIA's current instructions
sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

That last command is the real check: if `nvidia-smi` works inside a container,
the rest is ordinary work. If it does not, nothing downstream will.

### 4.2 Everything in containers

```bash
docker compose -f infra/compose.linux.yaml up -d --build
```

Containers invoke the **same entrypoint** used natively on the dev boxes
(`python -m argus.ingest --config …`). If a container needs a different command
to work, that difference is a bug, not a platform quirk.

### 4.3 GPU extras + decode

```bash
uv sync --all-packages --group staging     # onnxruntime-gpu (Linux x86_64 marker)
# NEVER pass --group cpu as well. Both distributions unpack into the same
# `onnxruntime` directory: the second overwrites the first, and later removing
# either can delete the shared directory, leaving a distribution that reports
# itself installed and will not import. Repair is
#   uv sync --all-packages --group cpu --reinstall-package onnxruntime
```

`config/staging.yaml` sets `decode: nvidia` (NVDEC probe with loud fallback to
software). **UNVERIFIED until run on the box** — this is the remaining M1 exit
criterion. First TensorRT engine build (when ADR-0021 lands that way) is cached
to disk and can take minutes; do not read a slow first start as a hang.

### 4.4 Timezone

```bash
timedatectl set-timezone Asia/Dhaka      # display only
```

The application stores UTC and reads the **policy timezone from config**, not
from the system. A server accidentally left on UTC must not silently shift every
pay-period boundary by six hours.

---

## 5. Cross-platform hygiene

- **Lowercase paths with underscores, always.** `models/Face.onnx` and
  `models/face.onnx` are the same file on a Mac and different files on the
  server. A structural test + CI catch this; your local machine never will.
- **LF line endings** via `.gitattributes`.
- **Never format numbers or parse dates with the ambient locale**, especially in
  the payroll export.
- **Do not depend on file watchers** in the runtime path: FSEvents and inotify
  differ, and inotify limits bite inside containers.
- **Log backend and model hash on every startup.** The first question in any
  "why do the numbers differ" conversation is which backend each side ran.

## 6. Verifying the setup

The smoke list, executable as tests and by hand:

1. `uv run pytest -q` — full suite (rig/store/golden skip loudly without
   services/artefacts).
2. Rig streams reachable: `uv run ffprobe rtsp://localhost:8554/canteen_door_01`
   (with `rig_serve.py` running).
3. Ingest connects, decodes, reconnects after `rigctl stop`/`start`, and records
   `stream_gap` rows with honest causes (stall/offline) — covered by
   `tests/rig/test_rig_ingest.py`.
4. Golden-frame suite runs and reports divergence within tolerance
   (`tests/golden/test_parity.py`).
5. Startup log names the backend and model hash (`get_detector` logs it).

If (4) fails on one platform and passes on the other, that is the parity suite
doing its job. Read the divergence before changing the tolerance.

## 7. What is not documented here yet

- Enrolling a test face (needs the face stack — ADR-0010, M3).
- Seeding a roster (M3; schema lands with it).
- The dashboard/review UI (ADR-0015, M3+).
- The macOS parity leg in CI (ADR-0022) — run manually on the Mac and record
  results until decided otherwise.

These are open because the decisions behind them are open, not because they were
forgotten.
