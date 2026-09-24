# LINUX_SETUP.md

Bring-up of Sparrow Vision on the NVIDIA box, **natively**, step by step, from a
fresh Ubuntu install to a demo you can show.

Read this one on the box. `AGENTS.md` §5–6 stays the authority on what the
commands mean; this file is the order to run them in and what each one proves.

**Native, not containerised.** `ARCHITECTURE.md` §5.4 wants Linux fully
containerised because that is what we intend to *operate*. Bring-up is a
different job: a container adds a CUDA base image and a GPU runtime to the list
of things that can be broken before you have learned anything. So Postgres and
mediamtx run in containers (they are stateless infrastructure and they work),
and everything that touches the GPU runs as a process you can strace.
Containerising the remaining services is the follow-up, with a working native
baseline (and a GPU-capable ingest image) to compare against.

**Bring-up record — this workstation (2026-09-24).** Ubuntu 24.04.5 LTS,
Python 3.12.3, `uv` 0.12.18, Docker 29.8.1/Compose 5.5.1, NVIDIA driver
595.91.07 (CUDA compatibility 13.2), NVIDIA Container Toolkit 1.20.1, CUDA
13.0 runtime libraries, and cuDNN 9.26 for CUDA 13. The RTX 4070 Ti reports
12,282 MiB VRAM. The locked `onnxruntime-gpu` is 1.30.0, built for CUDA 13.0;
the locked PyAV is 18.1.0. On this host both the CUDA inference session and
NVDEC probe succeeded. The native rig path is runnable; real cameras, the
ZKTeco reader, model artefacts other than SSD, identity, and accuracy remain
unverified. The clean final local run left ingest and the console running, with
all four rig cameras up, zero reconnects/gaps, an initial clean snapshot of 31
doorway events and 29 clips, and one simulated gate tap/event; the live synthetic
run continues to add rig events, while the UI health and authenticated pages
returned
HTTP 200. The optional ingest image was also rebuilt with CUDA 13/cuDNN 9 and
smoke-tested in Docker: four streams reached `state=up` with hardware CUDA
decode and zero reconnects.

**Nothing here produces an accuracy number.** The rig draws people as bright
disks. Every measurement in this file is about plumbing — does it connect, does
it decode, does it use the GPU, does it write the row. Accuracy needs real
cameras and a site survey (`ARCHITECTURE.md` §9.2, `PLAN.md` M8).

---

## 0. What you need before you start

| | |
|---|---|
| OS | Ubuntu 22.04 or 24.04 LTS, x86_64 |
| GPU | NVIDIA, RTX-class, ≥ 12 GB VRAM (`ARCHITECTURE.md` §5.6) |
| Disk | 40 GB free: model artefacts, rig footage, clips, Postgres |
| Network | Internet for the first sync; the factory LAN comes later (`INSTRUCTIONS.md`) |
| Access | sudo, and the repository |

Two commands decide how your week goes. They are deliberately run **after**
the `uv sync` in §4.1; before the workspace is synced they only prove that
whatever happens to be installed globally can be imported:

```bash
uv run --no-sync python -c "import onnxruntime as o; print(o.get_available_providers())"
uv run --no-sync python -c "from av.codec.hwaccel import hwdevices_available as h; print(h())"
```

The first says whether inference can use the GPU. The second says whether
*decode* can. They are independent, and a provider list is still only a claim
until §4.4 builds a real session.

---

## 1. Base system

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl ca-certificates ffmpeg netcat-openbsd tcpdump
sudo timedatectl set-timezone Asia/Dhaka
timedatectl show --property=NTPSynchronized --value     # want: yes
```

`ffmpeg` here is the command-line tool, used for `ffprobe` checks. The
application does **not** use it — PyAV carries its own libav. They can and do
have different capabilities; do not conclude anything about the application from
what `ffmpeg -hwaccels` prints.

Timezone is display only. The application stores UTC and takes the policy
timezone from config (`AGENTS.md` §6). NTP matters for real: the server clock is
what payroll arithmetic uses.

Docker, for Postgres and mediamtx only. The Ubuntu packages in the base image
are not sufficient for the GPU-container check; install Docker Engine and its
Compose plugin from Docker's official repository:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
newgrp docker                           # or log out and back in
docker run --rm hello-world
```

Until the new login shell has the supplementary group, prefix Docker commands
with `sg docker -c '...'` (as used in the commands below). Docker is enabled
and started by the package installation.

---

## 2. NVIDIA driver

First inspect what is already installed; do not reinstall a healthy driver as a
bring-up ritual:

```bash
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
```

On this workstation that reports an RTX 4070 Ti, driver `595.91.07`, and
12,282 MiB. If `nvidia-smi` is missing or fails, install the recommended driver
and reboot:

```bash
sudo ubuntu-drivers install
sudo reboot
nvidia-smi
```

`nvidia-smi` must print your GPU and a driver version. If it does not, stop —
nothing downstream will work, and no amount of Python will fix it.

Note the **CUDA Version** in the top-right of `nvidia-smi`. That is the highest
CUDA runtime this driver supports, and §3 has to land at or below it. A newer
driver can run an older CUDA runtime; it cannot make a CUDA 12 runtime satisfy
an ORT wheel built for CUDA 13.

---

## 3. CUDA runtime and cuDNN

`onnxruntime-gpu` ships **no** CUDA libraries of its own (checked in `uv.lock`:
its only dependencies are flatbuffers, numpy, packaging, protobuf). It dynamically
loads the system ones. If they are missing, the import still succeeds, the
provider still appears in the list, and inference can silently run on the CPU —
which is exactly the failure this project instruments against (§4.3).

**Match the wheel, not an old example.** The current lock pins
`onnxruntime-gpu==1.30.0`; its build metadata and `ort.print_debug_info()` say
`CUDA version used in build: 13.0`, and it requires cuDNN 9. The driver on this
box reports CUDA compatibility 13.2, so install the CUDA 13 component libraries
and cuDNN 9 for CUDA 13.

The `cuda-runtime-*` meta-packages are not component-only: on a machine with
the 595 driver they can propose installing a different `libnvidia-compute`
package or removing the installed driver. Simulate first and read the result:

```bash
# NVIDIA's apt repository for Ubuntu 24.04 x86_64 (use ubuntu2204 on 22.04)
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update

# This must not propose removing/replacing nvidia-driver-595-open.
sudo apt-get -s install cuda-libraries-13-0 libcudnn9-cuda-13

# Runtime/component libraries only; the compiler and full toolkit are not needed.
sudo apt-get install -y cuda-libraries-13-0 libcudnn9-cuda-13
```

`cuda-libraries-13-0` supplies the CUDA runtime, cuBLAS, cuFFT, cuRAND and
related shared libraries. `libcudnn9-cuda-13` supplies cuDNN 9. If the lock is
changed, use the CUDA/cuDNN major named by the new wheel and repeat the
simulation; do not mix a CUDA 12 `libcudnn` with this CUDA 13 ORT build.

Make the loader see the real target directory (the `lib64` entry is a symlink):

```bash
printf '%s\n' /usr/local/cuda-13.0/targets/x86_64-linux/lib \
  | sudo tee /etc/ld.so.conf.d/cuda-13.conf
sudo ldconfig
ldconfig -p | grep -E 'lib(cudart|cublasLt|cudnn)\.so'
```

**Version matching is empirical, not guessable.** ONNX Runtime builds against a
specific CUDA major and cuDNN major, and the wheel/error tells you which one it
wanted the moment it fails: the message names the exact `libcublasLt.so.N` or
`libcudnn.so.N` it could not load. Install the library the wheel names, then
run the post-sync session proof in §4.4. The provider list alone is not proof.

### 3.1 NVIDIA Container Toolkit (for GPU containers)

The toolkit supplies the driver device nodes to Docker; it does not replace the
CUDA runtime libraries above. On Ubuntu 24.04 x86_64:

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#^deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
nvidia-ctk --version
```

The real container check is:

```bash
sg docker -c 'docker run --rm --gpus all \
  nvidia/cuda:13.0.0-base-ubuntu24.04 nvidia-smi'
```

The result must show the same GPU and driver. A successful `nvidia-smi` inside
that image proves device visibility only; the Python session in §4.4 is still
the proof that ORT can load its CUDA and cuDNN dependencies. The ingest image
now installs the same CUDA 13 component libraries and cuDNN 9, so the optional
container check is:

```bash
sg docker -c 'docker buildx build --load -t argus-ingest:validation -f infra/ingest.dockerfile .'
```

A one-off `argus-ingest:validation` container was also run on this workstation
against the local Docker network: all four streams reached `state=up` with
`hardware decode via cuda` and zero reconnects. The first image build is large
(about 6.7 GB on this host) because it carries the CUDA runtime and cuDNN;
cache it rather than rebuilding it casually. `infra/compose.linux.yaml` still
defines infrastructure plus ingest only; pairing, gate, enrolment, and UI are
not yet full Compose services.

---

## 4. The repository and the Python environment

### 4.1 uv and the sync

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"

cd /path/to/cctv-argus-vision
uv lock --check
uv sync --frozen --all-packages --group staging   # NEVER together with --group cpu
```

Use the actual checkout path; the old `~/argus` name is not a requirement.
After this sync, prefer `uv run --no-sync ...` so a command cannot silently
change the locked environment. `cpu` and `staging` install `onnxruntime` and
`onnxruntime-gpu`, which unpack into the *same* directory and overwrite each
other. `uv` refuses both at once, and
`argus.backends.onnx_common.check_ort_environment` refuses to build a session
if it ever finds two (`AGENTS.md` §4). If an old environment was mixed, remove
only the virtual environment and recreate it:

```bash
rm -rf .venv
uv sync --frozen --all-packages --group staging
```

### 4.2 Secrets file

```bash
cp config/secrets.env.example config/secrets.env
chmod 600 config/secrets.env          # loading REFUSES anything looser
```

Set all four console passphrases (`UI_*`) for a usable local console. The
loader does **not** invent `ui.role_passphrases` from those variable names;
`config/staging_canteen.yaml` now contains the explicit mapping:

```yaml
ui:
  role_passphrases:
    viewer: ${UI_VIEWER_PASSPHRASE}
    reviewer: ${UI_REVIEWER_PASSPHRASE}
    payroll: ${UI_PAYROLL_PASSPHRASE}
    admin: ${UI_ADMIN_PASSPHRASE}
```

With none set, loading the staging config is the safe failure and nobody can
log in. Camera and reader values come later, in `INSTRUCTIONS.md`. The file is
mode 600, git-ignored, and excluded from Docker build contexts; it is still
sensitive on this workstation.

### 4.3 The two decisive checks

```bash
uv run --no-sync python - <<'EOF'
import onnxruntime as ort
from av.codec.hwaccel import hwdevices_available
print("ort  :", ort.get_available_providers())
print("libav:", hwdevices_available())
EOF
```

Read it like this:

| Output | Meaning | Do |
|---|---|---|
| `ort` contains `CUDAExecutionProvider` | The wheel offers CUDA. **Not** proof it loads | §4.4 |
| `ort` has CPU only | Wrong wheel, or a sync that installed the `cpu` group | Re-sync with `--group staging` |
| `libav` contains `cuda` | This PyAV build can do NVDEC | `decode: nvidia` is real; confirm the ingest log |
| `libav` has no `cuda` | This PyAV build has no NVDEC | Accept software decode and measure CPU load, or build an NVDEC-enabled PyAV |

The lock currently contains PyAV 18.1.0, whose `hwdevices_available()` on this
host is `['cuda', 'qsv', 'drm', 'amf']`. That is an observed property of this
wheel, not a guarantee for every PyAV build.

### 4.4 Proving CUDA actually loads

The provider list is a claim; a session is evidence.

```bash
uv run --no-sync python models/fetch.py           # fetches + sha256-verifies ssd_mobilenet_v1

uv run --no-sync python - <<'EOF'
import logging
logging.basicConfig(level=logging.INFO)
from argus.backends.registry import get_detector
d = get_detector("onnx-cuda")
print("model:", d.model_ref)
print("ACTIVE PROVIDERS:", d.providers_active)
assert "CUDAExecutionProvider" in d.providers_active
EOF
```

`ACTIVE PROVIDERS` must contain `CUDAExecutionProvider`. If it contains only
`CPUExecutionProvider`, the run logs an ERROR naming what did not load — that is
the CUDA/cuDNN mismatch from §3, and it is the single most expensive thing to
discover late, because everything still *works*, just ten times slower. The
CUDA golden test now also fails instead of silently passing when the requested
provider falls back.

`models/fetch.py` fetches only `ssd_mobilenet_v1`. The other four artefacts
(`yolo26m`, `yolo26m_pose`, `scrfd_10g_bnkps`, `glintr100`) are declared
`status: unresolved` — no pinned url, no sha256 — and fetch refuses them by
design (ADR-0026, ADR-0030). Any backend that needs one raises at construction
naming `models/registry.yaml`. That is expected, not a broken install.

---

## 5. Infrastructure containers

```bash
# Normal case when host port 5432 is free:
sg docker -c 'docker compose -f infra/compose.dev.yaml up -d'
sg docker -c 'docker compose -f infra/compose.dev.yaml ps'
```

On this workstation PostgreSQL 16 is already installed natively and owns
`127.0.0.1:5432`. Keep it untouched and publish the project's disposable
Postgres container on `5434` instead:

```bash
export ARGUS_PG_HOST_PORT=5434
export ARGUS_DATABASE__DSN='postgresql://argus:argus@localhost:5434/argus'
export ARGUS_TEST_DSN="$ARGUS_DATABASE__DSN"
sg docker -c 'ARGUS_PG_HOST_PORT=5434 docker compose -f infra/compose.dev.yaml up -d'
sg docker -c 'ARGUS_PG_HOST_PORT=5434 docker compose -f infra/compose.dev.yaml ps'
```

The compose file binds Postgres, RTSP, HLS and WebRTC to `127.0.0.1`; no
project port is published on the factory LAN. Migrations are applied by the
services themselves at startup; there is no separate migrate step. To start
over:

```bash
sg docker -c 'ARGUS_PG_HOST_PORT=5434 docker compose -f infra/compose.dev.yaml down -v'   # -v drops the project data volume
```

---

## 6. The virtual camera rig

```bash
uv run --no-sync python rig/synthetic/generate.py      # deterministic footage + manifests
uv run --no-sync python rig/bin/rig_serve.py           # publishes to mediamtx; leave running
```

In a second terminal:

```bash
uv run --no-sync python rig/bin/rig_rigctl.py status
ffprobe -v error -show_streams rtsp://localhost:8554/canteen_door_01 | grep codec_name
```

**Only one rig session at a time.** Two `rig_serve.py` processes publishing the
same paths produce a stream that looks alive and is wrong.

Fault injection, for the demo and for testing reconnect:

```bash
uv run --no-sync python rig/bin/rig_rigctl.py pause canteen_door_01
uv run --no-sync python rig/bin/rig_rigctl.py resume canteen_door_01
uv run --no-sync python rig/bin/rig_rigctl.py stop canteen_door_01
uv run --no-sync python rig/bin/rig_rigctl.py start canteen_door_01
```

---

## 7. Tests, in the order that tells you the most

```bash
# 1. logic only, no services — should be all green in seconds
uv run --no-sync pytest -m "not rig and not postgres" -q

# 2. lint/type gate, the same one CI runs
uv run --no-sync ruff check . && uv run --no-sync ruff format --check . && uv run --no-sync mypy

# 3. everything: rig over RTSP, Postgres, golden parity (needs §5 and §6 up)
export ARGUS_PG_HOST_PORT=5434
export ARGUS_TEST_DSN='postgresql://argus:argus@localhost:5434/argus'
export ARGUS_MODELS_ROOT="$PWD/models"
sg docker -c 'uv run --no-sync pytest -q'

# 4. THE M2 EXIT — the CUDA leg of the golden parity suite
ARGUS_MODELS_ROOT="$PWD/models" ARGUS_PARITY_BACKEND=onnx-cuda \
  uv run --no-sync pytest -m golden -q
```

On this workstation the recorded results are 356 passed / 1 skipped for step 1,
465 passed / 1 skipped for the full suite, all lint/type checks green, 7 passed
for the CPU golden leg, and 7 passed for the CUDA golden leg. The CUDA test
asserts an active `CUDAExecutionProvider`; a CPU fallback is now a failure, not
a misleading pass. Step 3 is run with the port variables above because native
PostgreSQL already owns 5432.

Step 4 is the one this box exists for. It compares CUDA detections against the
committed CPU reference at the tolerances in `ARCHITECTURE.md` §5.3, over the
same artefact hash. If it *skips*, the backend was unavailable and you are back
at §4.4; a skip is not a pass. Record the result in `DECISIONS.md` (ADR-0022
asks for exactly that) and update `PLAN.md` M2.

Once `yolo26m` is pinned (§7.1), its leg runs beside SSD's and against its own
committed reference:

```bash
ARGUS_MODELS_ROOT="$PWD/models" ARGUS_PARITY_ARTEFACT=yolo26m \
  ARGUS_PARITY_BACKEND=onnx-cuda-yolo uv run --no-sync pytest -m golden -q
```

Step 3 includes `tests/rig/test_canteen_replay.py`, which replays the manifest
through RTSP and expects nine of ten labelled crossings with every direction
correct. That is the end-to-end proof, and the tenth is a known merge of two
people crossing 1.05 s apart.

---

### 7.1 Resolving the YOLO artefact

The detector the canteen path is meant to use (`yolo26m`, ADR-0030) is
registered as `onnx-cpu-yolo` / `onnx-cuda-yolo` / `onnx-coreml-yolo` and is
**unresolved**: `models/registry.yaml` pins no url and no sha256, so
constructing it raises a `ModelArtefactError` naming that file. Nothing selects
it by accident — SSD stays the default for every plain backend name.

The model itself is pretrained; there is nothing to train here, ever. What is
missing is the file and its hash.

1. Obtain `yolo26m.onnx`. ADR-0030's construction is that the artefact is a
   **pre-exported ONNX**, so no AGPL code enters this runtime. If you export it
   yourself with `ultralytics`, do it in a throwaway virtualenv, never in this
   project's environment.
2. `sha256sum yolo26m.onnx`.
3. Fill in `sha256:` and `url:` in `models/registry.yaml` and **delete the
   `status: unresolved` line**. A `file:///srv/...` path is a valid url and
   keeps `models/fetch.py`'s hash check in the loop. Leave
   `commercial_use: false` alone.
4. `uv run --no-sync python models/fetch.py` — fetches and verifies.
5. Produce the reference **on a CPU box** (the reference leg is always CPU,
   `ARCHITECTURE.md` §5.3) and commit it:
   `ARGUS_MODELS_ROOT=models uv run --no-sync python tests/golden/make_reference.py yolo26m`
6. Run the CUDA leg above against that reference, and record the observed
   divergence. YOLO's tolerances are not SSD's and are not inherited.
7. Only then point a pipeline at it: `detector_backend: onnx-cuda-yolo`.

If the export's output tensor is neither `(1,N,6)` nor `(1,84,N)`, construction
refuses with the shape it saw. That is the wrapper working — a guessed layout
produces boxes that are wrong in a way no test notices.

## 8. Running it

Each service is one process. Run them in separate terminals (or see §11 for
systemd units).

### 8.1 Ingest, with the canteen pipeline

```bash
export ARGUS_DATABASE__DSN='postgresql://argus:argus@localhost:5434/argus'
export ARGUS_MODELS_ROOT="$PWD/models"
uv run --no-sync python -m argus.ingest --config config/staging_canteen.yaml
```

`config/staging_canteen.yaml` is the box's analysis config: door lines,
`analysis_fps: 15`, `decode: nvidia`, mock detector. `config/staging.yaml` has
no door lines and records without analysing — use it only for a stream-only
test.

Read the first twenty log lines before anything else. They tell you:

- `camera <id>: hardware decode via cuda` — NVDEC is on. Or
  `ingest.decode='nvidia' but this libav build offers [...] -- decoding in SOFTWARE`,
  which is the honest answer and does not stop anything.
- `detector backend=mock model=... providers=n/a` — the mock detector, as
  configured. With `detector_backend: onnx-cuda` it prints the real providers,
  and that line is the GPU proof for the pipeline path.
- `analysing 2 canteen door(s) in this process (ADR-0033)`.

Swapping to the real detector is a one-line edit in that config, and the result
will be **zero crossings** on this rig: SSD MobileNet looks for people and the
rig draws disks. That is the expected outcome of the swap — it exercises CUDA,
it does not demonstrate the pipeline. Use the mock detector for the demo and
the parity suite for the GPU.

On the verified workstation the first twenty lines reported `hardware decode
via cuda` for all four rig streams. Clip extraction preferred remux but the
PyAV 18 muxer returned `EINVAL`; the documented decode-and-encode fallback then
produced playable clips. A remux failure is therefore a performance/quality
warning, not a lost event, but it should be investigated before real cameras
are treated as an evidence archive.

### 8.2 Pairing, report, gate, console

```bash
export ARGUS_DATABASE__DSN='postgresql://argus:argus@localhost:5434/argus'
export ARGUS_MODELS_ROOT="$PWD/models"
day="$(TZ=Asia/Dhaka date +%F)"

uv run --no-sync python -m argus.pairing --config config/staging_canteen.yaml --once --day "$day"
uv run --no-sync python -m argus.pairing.report --config config/staging_canteen.yaml --day "$day"
uv run --no-sync python -m argus.gate --config config/staging_canteen.yaml --once --badge B-1
uv run --no-sync python -m argus.ui --config config/staging_canteen.yaml
```

If the project Postgres is on the normal host port 5432, omit the
`ARGUS_DATABASE__DSN` override. The UI roles are populated from the four
`UI_*_PASSPHRASE` values through the explicit mapping in
`config/staging_canteen.yaml`; setting only the environment variables is not
enough.

Pairing exit codes: `0` ok, `2` config/policy, `3` database, `4` the window was
empty. **4 is not a failure** — it means no doorway events in that day, which is
what you get before ingest has run.

The console binds to `127.0.0.1` by ADR-0028, because it authenticates a *role*
and not a person. From your laptop:

```bash
ssh -L 8080:127.0.0.1:8080 user@gpu-box     # then open http://localhost:8080
```

Do not set `ui.bind_host` to the LAN. That needs `acknowledged_lan_exposure` and
a new ADR, and it puts a page full of clips on the factory network.

Enrolment, if you are demonstrating the roster (consent is enforced twice and
`--by` is mandatory):

```bash
uv run --no-sync argus-enrol --config config/staging_canteen.yaml list
```

---

## 9. When it does not work

| Symptom | Cause | Fix |
|---|---|---|
| `ACTIVE PROVIDERS` is CPU-only, and an ERROR names a `.so` | CUDA/cuDNN major mismatch with the wheel | Install the library the error names (§3); for the current lock use CUDA 13 + cuDNN 9 |
| `multiple onnxruntime distributions installed` | `cpu` and `staging` were both synced into one environment | Recreate `.venv` with `uv sync --frozen --all-packages --group staging` |
| `models root not found (no registry.yaml ...)` | Running from outside the repo | `cd` to the checkout, or set `ARGUS_MODELS_ROOT="$PWD/models"` |
| `UI_*_PASSPHRASE` is set but the console has no roles | The shipped YAML has no mapping for those variables | Use `config/staging_canteen.yaml`, which maps all four roles explicitly |
| `artefact yolo26m is unresolved` | By design — no pinned url/sha256 (ADR-0030) | Use `detector_backend: mock`, or pin the artefact first |
| NVDEC missing: `libav` has no `cuda` | This PyAV build has no NVDEC | Accept software decode and measure CPU load, or build PyAV against an NVDEC-enabled FFmpeg; do not infer this from `ffmpeg -hwaccels` |
| Ingest reconnects forever, `stream_gap` rows pile up | mediamtx has no publisher, or two rig sessions are fighting | Stop stale `rig_serve.py` processes and leave exactly one running (§6) |
| `config/secrets.env is mode 644` | Permissions | `chmod 600 config/secrets.env`; keep it out of Docker build contexts |
| Pairing exits 4 | Empty window | Expected before ingest produces events |
| `port is already allocated` on 5432 | Native PostgreSQL or another service owns it | Keep it, set `ARGUS_PG_HOST_PORT=5434`, and point `ARGUS_DATABASE__DSN`/`ARGUS_TEST_DSN` at 5434 |
| Migration refuses to apply | A migration file changed after being applied | `docker compose -f infra/compose.dev.yaml down -v` and start clean. Never edit an applied migration |
| Clip log says `remux failed ... falling back to decode+encode` | The PyAV template muxer rejected the packet stream | Confirm the fallback MP4 with `ffprobe`; investigate remux before relying on bit-identical clips |

---

## 10. The demo, in order

Thirty minutes, and say the caveats out loud rather than hoping nobody asks.

1. **Start**: infra (§5), rig (§6), ingest with `config/staging_canteen.yaml`,
   console over the SSH tunnel.
2. **Show the stream arriving** — the status log line: frames, packets, ring
   bytes, reconnects.
3. **Let the rig run a few minutes**, then pair and report (§8.2). Show the
   canteen page: crossings, intervals, dwell.
4. **Open a clip from a line.** This is the point of the whole system: any
   payroll-affecting number resolves to video in under a minute, by someone who
   did not write the code (`SOUL.md`).
5. **Break a camera live**: `rig_rigctl.py pause canteen_door_01`. Show the
   `stream_gap` row and that the affected day is flagged, not charged. "We saw
   nothing" stays distinguishable from "nothing happened".
6. **Say what it is not**: identity is off, so every crossing is `unknown` and
   unknown deducts zero (ADR-0010, ADR-0004). Shadow mode — there is no export
   and a test asserts its absence. The footage is synthetic, so no accuracy
   number here means anything (`ARCHITECTURE.md` §9.2).

The fifth step is the one that persuades people, and the sixth is the one that
keeps you honest when they ask for a pilot next week.

---

## 11. Optional: systemd, for a run that survives logout

Only after §7 and §8 pass by hand. `%h` is the service user's home.

```ini
# /etc/systemd/system/argus-ingest.service
[Unit]
Description=Sparrow Vision ingest
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=argus
WorkingDirectory=/home/argus/cctv-argus-vision
ExecStart=/home/argus/.local/bin/uv run --no-sync python -m argus.ingest --config config/staging_canteen.yaml
Restart=on-failure
RestartSec=5
# The clock the payroll arithmetic uses, and the timezone the policy reads
Environment=TZ=Asia/Dhaka
# This workstation keeps native PostgreSQL on 5432; project Compose uses 5434.
Environment=ARGUS_DATABASE__DSN=postgresql://argus:argus@localhost:5434/argus
Environment=ARGUS_MODELS_ROOT=/home/argus/cctv-argus-vision/models

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now argus-ingest
journalctl -u argus-ingest -f
```

A pairing run is a timer, not a service — it is a batch job over a finished
day. Create a real service user and use the actual checkout path first; this
template is deliberately complete rather than a fragment:

```ini
# /etc/systemd/system/argus-pairing.service
[Unit]
Description=Sparrow Vision daily pairing
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
User=argus
WorkingDirectory=/home/argus/cctv-argus-vision
Environment=TZ=Asia/Dhaka
Environment=ARGUS_DATABASE__DSN=postgresql://argus:argus@localhost:5434/argus
ExecStart=/usr/local/sbin/argus-pairing-today
```

Pairing needs one service instance per day, so the usual deployment is a
wrapper that substitutes today's Dhaka date and treats exit code 4 as an empty
window. Keep that wrapper in a reviewed script rather than embedding a complex
shell expression in a unit:

```ini
# /etc/systemd/system/argus-pairing.timer
[Unit]
Description=Run Sparrow Vision pairing after the factory day closes

[Timer]
OnCalendar=*-*-* 19:00:00 Asia/Dhaka
Persistent=true

[Install]
WantedBy=timers.target
```

`|| [ $? -eq 4 ]` belongs in that reviewed wrapper: it keeps "the window was
empty" from looking like a failure. For example, the wrapper can be:

```sh
#!/bin/sh
set -u
day="$(TZ=Asia/Dhaka date +%F)"
/home/argus/.local/bin/uv run --no-sync python -m argus.pairing \
  --config config/staging_canteen.yaml --once --day "$day"
rc=$?
[ "$rc" -eq 4 ] && exit 0
exit "$rc"
```

Do not enable either unit until the path, user, database port, secrets file, and
dry-run command have been checked on this host.

---

## 12. What this box still has not proven

Write these down after bring-up; they are the honest list for the demo and for
`PLAN.md`.

- **NVDEC (M1 exit)** — **PROVEN on this workstation**: §4.3 reported `cuda`,
  the native ingest log said `hardware decode via cuda` for all four rig
  streams, and the optional ingest container did the same. This still says
  nothing about real-camera packet compatibility.
- **CUDA parity (M2 exit)** — **PROVEN for the pinned SSD artefact**: the CUDA
  golden leg passed 7/7 with an active `CUDAExecutionProvider`. Record the
  result and date in `DECISIONS.md`; the unresolved YOLO/face artefacts remain
  separate legs.
- **Sizing** — how many cameras this GPU carries, decode included. ADR-0020 is
  open and cannot close on rig footage (`ARCHITECTURE.md` §5.7, "GPU memory").
- **Two concurrent clients per camera** (ADR-0031) — needs real cameras.
- **Per-camera GOP** — needs real cameras; the rig sets its own.
- **Identity** — no measured threshold, so `face.enabled` stays false and every
  crossing is `unknown` (ADR-0010).
- **The badge reader** — see `INSTRUCTIONS.md`; the client has never exchanged a
  byte with a device.
- **Any accuracy claim whatsoever** — synthetic footage cannot support one.

The 2026-09-24 validation run also exposed two operational details to carry
forward: a deliberate `timeout`/SIGINT shutdown used to be recorded as a
`crash` gap (now fixed by not treating task cancellation as a pipeline
exception), and PyAV 18.1.0 fell back from packet remux to decode+encode for
clips. Both are visible in the runbook because a green stream test is not the
same as a clean evidence archive.
