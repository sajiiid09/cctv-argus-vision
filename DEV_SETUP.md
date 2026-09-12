# DEV_SETUP.md

Getting a working development environment.

> **Everything in this document is UNVERIFIED.** No code exists, the stack is not
> chosen (`DECISIONS.md` ADR-0009 through ADR-0018 are all open), and none of
> these steps have been executed. This is a shape for the setup, written now so
> that the platform differences are visible before they cost a week. Treat every
> command as illustrative. Update this file as steps are actually run, and delete
> the UNVERIFIED markers only for steps you have personally executed.

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

The consequence: on macOS you run a hybrid — containerised infrastructure, native
Python for anything touching the GPU. This is not a temporary workaround. Docker
Desktop cannot pass through the Apple GPU, and CPU-only inference in a container
is slow enough that what you learn from it is misleading. Plan for the hybrid;
do not keep trying to fix it.

---

## 2. macOS setup — UNVERIFIED

### 2.1 Base tools

```bash
# Homebrew, if not present: https://brew.sh
brew install git ffmpeg
brew install --cask docker        # Docker Desktop
```

`ffmpeg` from Homebrew includes VideoToolbox support. It does **not** include
NVDEC, obviously, and code must not assume a specific hardware decoder is
available — probe, and fall back to software decode.

### 2.2 Python — UNVERIFIED

Version and tool are **OPEN** (ADR-0017, ADR-0018). The leaning is `uv` with
platform markers and optional extras, so the illustrative shape is:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --extra macos        # CPU + CoreML wheels; no CUDA packages
```

The `macos` extra exists because CUDA-only packages (`onnxruntime-gpu`,
TensorRT bindings, `nvidia-*`) **do not install on macOS at all**. A single
unconditional dependency list cannot work across both platforms; whatever tool we
choose must express this without a runtime hack.

### 2.3 Infrastructure in Docker — UNVERIFIED

Postgres, a broker (if ADR-0013 lands on Redis), and mediamtx for the virtual
camera rig:

```bash
docker compose -f infra/compose.dev.yaml up -d
```

Note `/dev/shm` sizing if anything uses shared memory — the 64 MB default causes
confusing failures. Set it explicitly in compose.

### 2.4 The virtual camera rig — UNVERIFIED

mediamtx serving looped footage over RTSP, so the pipeline sees something
indistinguishable from a camera:

```bash
# illustrative; real config lives in rig/
ffmpeg -re -stream_loop -1 -i rig/footage/canteen_door_01.mp4 \
       -c copy -f rtsp rtsp://localhost:8554/canteen_door_01
```

Footage is **not** committed to git (size, and in future possibly faces). See
`FOOTAGE.md` for what to obtain and where it is stored.

### 2.5 Running analysis natively — UNVERIFIED

```bash
uv run services/ingest/main.py       --config config/dev.macos.yaml
uv run services/canteen_worker/main.py --config config/dev.macos.yaml
```

Backends are selected by capability probe, not by a platform flag. On this
machine that should resolve to the CoreML execution provider; the process logs
which backend and model hash it chose on startup. If it silently picks CPU, that
is a bug worth chasing immediately — you will otherwise spend a week benchmarking
the wrong thing.

---

## 3. Ubuntu 22.04 / 24.04 setup — UNVERIFIED

### 3.1 Driver and container toolkit

```bash
# NVIDIA driver (version depends on the GPU — ADR-0020 is open)
sudo ubuntu-drivers install
# NVIDIA Container Toolkit — follow NVIDIA's current instructions
sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

That last command is the real check: if `nvidia-smi` works inside a container,
the rest is ordinary work. If it does not, nothing downstream will.

### 3.2 Everything in containers

```bash
docker compose -f infra/compose.linux.yaml up -d
```

Containers invoke the **same entrypoints** used natively on macOS. If a container
needs a different command to work, that difference is a bug, not a platform
quirk — it is exactly the drift that makes staging results untransferable.

### 3.3 CUDA/TensorRT extras — UNVERIFIED

```bash
uv sync --extra cuda
```

First run may build a TensorRT engine, which can take minutes and is cached to
disk keyed by (model hash, GPU, TRT version, driver). This is intentional
(ADR-0021) — do not interpret a slow first start as a hang. Log the build so it
is visibly happening.

### 3.4 Timezone

```bash
timedatectl set-timezone Asia/Dhaka      # display only
```

The application stores UTC and reads the **policy timezone from config**, not
from the system. A server accidentally left on UTC must not silently shift every
pay-period boundary by six hours. Setting the system zone is a convenience for
whoever reads the logs, not a functional dependency.

---

## 4. Cross-platform hygiene

- **Lowercase paths with underscores, always.** `models/Face.onnx` and
  `models/face.onnx` are the same file on your Mac and different files on the
  server. A CI check on Linux catches this; your local machine never will.
- **LF line endings** via `.gitattributes`.
- **Never format numbers or parse dates with the ambient locale**, especially in
  the payroll export.
- **Do not depend on file watchers** in the runtime path: FSEvents and inotify
  differ, and inotify limits bite inside containers.
- **Log backend and model hash on every startup.** The first question in any
  "why do the numbers differ" conversation is which backend each side ran.

---

## 5. Verifying the setup — UNVERIFIED

Once code exists, this should be the smoke test on both platforms:

1. Rig streams reachable: `ffprobe rtsp://localhost:8554/canteen_door_01`.
2. Ingest connects, decodes, reconnects after you kill and restart the stream.
3. A clip can be extracted for a given camera and time range, and plays.
4. Golden-frame suite runs and reports divergence within tolerance
   (`TESTING.md`).
5. Startup log names the backend and model hash, and they are what you expect.

If (4) fails on one platform and passes on the other, that is the parity suite
doing its job. Read the divergence before changing the tolerance.

---

## 6. What is not documented here yet

- Enrolling a test face (needs the face stack — ADR-0010).
- Seeding a roster (needs the schema — `DATA_MODEL.md` is provisional).
- Running the dashboard (needs ADR-0015).
- CI setup, particularly the macOS leg of the parity suite (ADR-0022).

These are open because the decisions behind them are open, not because they were
forgotten.
