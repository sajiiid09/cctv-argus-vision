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
Containerising the services is the follow-up, with a working baseline to
compare against.

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

Two commands decide how your week goes. Everything in §3 and §4 exists to get
honest answers out of them:

```bash
uv run python -c "import onnxruntime as o; print(o.get_available_providers())"
uv run python -c "from av.codec.hwaccel import hwdevices_available as h; print(h())"
```

The first says whether inference can use the GPU. The second says whether
*decode* can. They are independent, and the second is the one that usually
disappoints.

---

## 1. Base system

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl ca-certificates ffmpeg
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

Docker, for Postgres and mediamtx only:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"      # log out and back in
docker run --rm hello-world
```

---

## 2. NVIDIA driver

```bash
sudo ubuntu-drivers install
sudo reboot
nvidia-smi
```

`nvidia-smi` must print your GPU and a driver version. If it does not, stop —
nothing downstream will work, and no amount of Python will fix it.

Note the **CUDA Version** in the top-right of `nvidia-smi`. That is the highest
CUDA runtime this driver supports, and §3 has to land at or below it.

---

## 3. CUDA runtime and cuDNN

`onnxruntime-gpu` ships **no** CUDA libraries of its own (checked in `uv.lock`:
its only dependencies are flatbuffers, numpy, packaging, protobuf). It dynamically
loads the system ones. If they are missing, the import still succeeds, the
provider still appears in the list, and inference silently runs on the CPU —
which is exactly the failure this project instruments against (§4.3).

```bash
# NVIDIA's apt repository for your release (24.04 shown; use 2204 for 22.04)
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update

# Runtime libraries only — the full toolkit and its compiler are not needed
sudo apt install -y cuda-runtime-12-6 libcudnn9-cuda-12
```

Then make the loader see them:

```bash
echo /usr/local/cuda/lib64 | sudo tee /etc/ld.so.conf.d/cuda.conf
sudo ldconfig
```

**Version matching is empirical, not guessable.** ONNX Runtime builds against a
specific CUDA major and cuDNN major, and the wheel tells you which one it wanted
the moment it fails: the error names the exact `libcublasLt.so.N` or
`libcudnn.so.N` it could not load. Install to match that name rather than to
match this document. Verify before going further:

```bash
uv run python - <<'EOF'
import onnxruntime as ort
print("providers:", ort.get_available_providers())
EOF
```

(That command needs §4 first if you have not synced yet — run it again after.)

---

## 4. The repository and the Python environment

### 4.1 uv and the sync

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"

git clone <this repo> ~/argus && cd ~/argus
git checkout dev/init

uv sync --all-packages --group staging      # NEVER together with --group cpu
```

`cpu` and `staging` install `onnxruntime` and `onnxruntime-gpu`, which unpack
into the *same* directory and overwrite each other. `uv` refuses both at once,
and `argus.backends.onnx_common.check_ort_environment` refuses to build a
session if it ever finds two (`AGENTS.md` §4). If you have already mixed them:

```bash
uv sync --all-packages --group staging --reinstall-package onnxruntime-gpu
```

### 4.2 Secrets file

```bash
cp config/secrets.env.example config/secrets.env
chmod 600 config/secrets.env          # loading REFUSES anything looser
```

Set at least the console passphrases (`UI_*`) — with none set, nobody can log
in, which is the safe default but not a demo. Camera and reader values come
later, in `INSTRUCTIONS.md`.

### 4.3 The two decisive checks

```bash
uv run python - <<'EOF'
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
| `libav` contains `cuda` | This PyAV build can do NVDEC | `decode: nvidia` is real |
| `libav` has no `cuda` | This PyAV wheel has no NVDEC — normal for a pip wheel (`ARCHITECTURE.md` §5.5) | See §9, "NVDEC is missing" |

### 4.4 Proving CUDA actually loads

The provider list is a claim; a session is evidence.

```bash
uv run python models/fetch.py           # fetches + sha256-verifies ssd_mobilenet_v1

uv run python - <<'EOF'
import logging
logging.basicConfig(level=logging.INFO)
from argus.backends.registry import get_detector
d = get_detector("onnx-cuda")
print("model:", d.model_ref)
print("ACTIVE PROVIDERS:", d.providers_active)
EOF
```

`ACTIVE PROVIDERS` must contain `CUDAExecutionProvider`. If it contains only
`CPUExecutionProvider`, the run logs an ERROR naming what did not load — that is
the CUDA/cuDNN mismatch from §3, and it is the single most expensive thing to
discover late, because everything still *works*, just ten times slower.

`models/fetch.py` fetches only `ssd_mobilenet_v1`. The other four artefacts
(`yolo26m`, `yolo26m_pose`, `scrfd_10g_bnkps`, `glintr100`) are declared
`status: unresolved` — no pinned url, no sha256 — and fetch refuses them by
design (ADR-0026, ADR-0030). Any backend that needs one raises at construction
naming `models/registry.yaml`. That is expected, not a broken install.

---

## 5. Infrastructure containers

```bash
docker compose -f infra/compose.dev.yaml up -d
docker compose -f infra/compose.dev.yaml ps      # both healthy/running
```

Postgres on 5432, mediamtx on 8554. On this box nothing else competes for 5432,
so the `ARGUS_PG_HOST_PORT` escape hatch from `AGENTS.md` §5 is a Mac problem
and you can ignore it.

Migrations are applied by the services themselves at startup; there is no
separate migrate step. To start over:

```bash
docker compose -f infra/compose.dev.yaml down -v   # -v drops the data volume
```

---

## 6. The virtual camera rig

```bash
uv run python rig/synthetic/generate.py      # deterministic footage + manifests
uv run python rig/bin/rig_serve.py           # publishes to mediamtx; leave running
```

In a second terminal:

```bash
uv run python rig/bin/rig_rigctl.py status
ffprobe -v error -show_streams rtsp://localhost:8554/canteen_door_01 | grep codec_name
```

**Only one rig session at a time.** Two `rig_serve.py` processes publishing the
same paths produce a stream that looks alive and is wrong.

Fault injection, for the demo and for testing reconnect:

```bash
uv run python rig/bin/rig_rigctl.py pause canteen_door_01
uv run python rig/bin/rig_rigctl.py resume canteen_door_01
uv run python rig/bin/rig_rigctl.py stop canteen_door_01
uv run python rig/bin/rig_rigctl.py start canteen_door_01
```

---

## 7. Tests, in the order that tells you the most

```bash
# 1. logic only, no services — should be all green in seconds
uv run pytest -m "not rig and not postgres" -q

# 2. lint/type gate, the same one CI runs
uv run ruff check . && uv run ruff format --check . && uv run mypy

# 3. everything: rig over RTSP, Postgres, golden parity (needs §5 and §6 up)
ARGUS_MODELS_ROOT="$PWD/models" uv run pytest -q

# 4. THE M2 EXIT — the CUDA leg of the golden parity suite
ARGUS_MODELS_ROOT="$PWD/models" ARGUS_PARITY_BACKEND=onnx-cuda uv run pytest -m golden -q
```

Step 4 is the one this box exists for. It compares CUDA detections against the
committed CPU reference at the tolerances in `ARCHITECTURE.md` §5.3, over the
same artefact hash. If it *skips*, the backend was unavailable and you are back
at §4.4; a skip is not a pass. Record the result in `DECISIONS.md` (ADR-0022
asks for exactly that) and update `PLAN.md` M2.

Once `yolo26m` is pinned (§7.1), its leg runs beside SSD's and against its own
committed reference:

```bash
ARGUS_MODELS_ROOT="$PWD/models" ARGUS_PARITY_ARTEFACT=yolo26m \
  ARGUS_PARITY_BACKEND=onnx-cuda-yolo uv run pytest -m golden -q
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
4. `uv run python models/fetch.py` — fetches and verifies.
5. Produce the reference **on a CPU box** (the reference leg is always CPU,
   `ARCHITECTURE.md` §5.3) and commit it:
   `ARGUS_MODELS_ROOT=models uv run python tests/golden/make_reference.py yolo26m`
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
uv run python -m argus.ingest --config config/staging_canteen.yaml
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
will be **zero crossings**: SSD MobileNet looks for people and the rig draws
disks. That is the expected outcome of the swap — it exercises CUDA, it does not
demonstrate the pipeline. Use the mock detector for the demo and the parity
suite for the GPU.

### 8.2 Pairing, report, gate, console

```bash
day="$(TZ=Asia/Dhaka date +%F)"

uv run python -m argus.pairing --config config/staging_canteen.yaml --once --day "$day"
uv run python -m argus.pairing.report --config config/staging_canteen.yaml --day "$day"
uv run python -m argus.gate --config config/staging_canteen.yaml --once --badge B-1
uv run python -m argus.ui --config config/staging_canteen.yaml
```

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
uv run argus-enrol --config config/staging_canteen.yaml list
```

---

## 9. When it does not work

| Symptom | Cause | Fix |
|---|---|---|
| `ACTIVE PROVIDERS` is CPU-only, and an ERROR names a `.so` | CUDA/cuDNN major mismatch with the wheel | Install the library the error names (§3) |
| `multiple onnxruntime distributions installed` | `cpu` and `staging` both synced | `uv sync --all-packages --group staging --reinstall-package onnxruntime-gpu` |
| `models root not found (no registry.yaml ...)` | Running from outside the repo | `cd ~/argus`, or set `ARGUS_MODELS_ROOT="$PWD/models"` |
| `artefact yolo26m is unresolved` | By design — no pinned url/sha256 (ADR-0030) | Use `detector_backend: mock`, or pin the artefact first |
| NVDEC missing: `libav` has no `cuda` | pip PyAV is built without ffnvcodec | Either accept software decode (the packet ring keeps decode off the evidence path, ADR-0031) or build PyAV against an NVDEC-enabled ffmpeg. **Measure CPU load first** — this is only worth doing if decode is actually the bottleneck |
| Ingest reconnects forever, `stream_gap` rows pile up | mediamtx has no publisher | `rig_serve.py` must be running (§6) |
| `config/secrets.env is mode 644` | Permissions | `chmod 600 config/secrets.env` |
| Pairing exits 4 | Empty window | Expected before ingest produces events |
| `port is already allocated` on 5432 | Another Postgres | Stop it, or set `ARGUS_PG_HOST_PORT` and point `ARGUS_DATABASE__DSN` at it |
| Migration refuses to apply | A migration file changed after being applied | `docker compose -f infra/compose.dev.yaml down -v` and start clean. Never edit an applied migration |

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
WorkingDirectory=/home/argus/argus
ExecStart=/home/argus/.local/bin/uv run --no-sync python -m argus.ingest --config config/staging_canteen.yaml
Restart=on-failure
RestartSec=5
# The clock the payroll arithmetic uses, and the timezone the policy reads
Environment=TZ=Asia/Dhaka

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now argus-ingest
journalctl -u argus-ingest -f
```

A pairing run is a timer, not a service — it is a batch job over a finished day:

```ini
# /etc/systemd/system/argus-pairing.service   (Type=oneshot)
ExecStart=/bin/sh -c '/home/argus/.local/bin/uv run --no-sync python -m argus.pairing \
  --config config/staging_canteen.yaml --once --day "$(TZ=Asia/Dhaka date +%%F)" || [ $? -eq 4 ]'
```

`|| [ $? -eq 4 ]` keeps "the window was empty" from looking like a failure.

---

## 12. What this box still has not proven

Write these down after bring-up; they are the honest list for the demo and for
`PLAN.md`.

- **NVDEC** (M1 exit) — proven only if §4.3 showed `cuda` in `hwdevices_available()`
  *and* the ingest log said `hardware decode via cuda`.
- **CUDA parity** (M2 exit) — the §7 step 4 result, recorded in `DECISIONS.md`.
- **Sizing** — how many cameras this GPU carries, decode included. ADR-0020 is
  open and cannot close on rig footage (`ARCHITECTURE.md` §5.7, "GPU memory").
- **Two concurrent clients per camera** (ADR-0031) — needs real cameras.
- **Per-camera GOP** — needs real cameras; the rig sets its own.
- **Identity** — no measured threshold, so `face.enabled` stays false and every
  crossing is `unknown` (ADR-0010).
- **The badge reader** — see `INSTRUCTIONS.md`; the client has never exchanged a
  byte with a device.
- **Any accuracy claim whatsoever** — synthetic footage cannot support one.
