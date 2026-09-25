# INSTRUCTIONS.md — cameras and the badge reader on the factory LAN

How to put real hardware in front of this system: the CCTV cameras and the
ZKTeco badge reader, both on the same router and LAN as the GPU box.

`LINUX_SETUP.md` gets the box running on the virtual rig. This file replaces the
rig with the building. Do them in that order — a camera problem and a CUDA
problem at the same time cost more than the sum of the two.

Two warnings to carry through the whole file:

- **The badge reader code has never spoken to a device.** `ZktTapSource` is
  written from the protocol description and its tests are hand-constructed
  fixtures, not captures. Expect to debug it with the reader in front of you.
  The fallback is `SimulatedTapSource`, which keeps the whole gate path real.
- **A doorway is the worst lighting case in any building.** Camera placement
  decides more about accuracy than any threshold in this repository
  (`PLAN.md` M8).

**Current workstation status (2026-09-24).** This host is
`192.168.10.5/24` on the local LAN. Its native rig path is verified: Postgres
and mediamtx are local-only containers, four synthetic RTSP streams publish,
CUDA inference and NVDEC have passed, and the simulated gate path writes a
`not_attempted` result. No real camera or badge reader has been connected to
this host yet; every hardware checkbox below remains open. The address table
uses the workstation's actual subnet, but the camera/reader addresses are
planned placeholders, not discovered devices.

---

## 1. Network plan

Everything on one LAN behind one router: GPU box, cameras, reader. Nothing
needs to reach the internet, and nothing should be reachable *from* it.

Fill this in before touching a cable, and keep it with the machine:

| Device | Model | MAC | IP | Port(s) | Credential lives in |
|---|---|---|---|---|---|
| GPU box | | | `192.168.10.5` (verified) | 5432 native, 5434 project container, 8554 RTSP, 8080 localhost only | — |
| Canteen door 1 | | | `192.168.10.21` (planned) | 554 | `config/secrets.env` |
| Canteen door 2 | | | `192.168.10.22` (planned) | 554 | `config/secrets.env` |
| Gate camera | | | `192.168.10.23` (planned) | 554 | `config/secrets.env` |
| Floor camera | | | `192.168.10.24` (planned) | 554 | `config/secrets.env` |
| Badge reader | | | `192.168.10.30` (planned) | 4370 | `config/secrets.env` |

The workstation's native PostgreSQL 16 owns `127.0.0.1:5432`; the project's
Compose Postgres is deliberately published on `127.0.0.1:5434` instead. The
camera/reader rows above are a plan for this subnet, not evidence that those
devices exist. Replace them with DHCP reservations or static addresses after
the site survey.

Rules:

- **Static addressing.** A DHCP reservation on the router (by MAC) or a static
  IP on the device. A camera that moves address becomes a stream gap, and a gap
  is a flagged day.
- **No port forwarding. Ever.** Not for the cameras, not for the reader, not for
  the console. If someone needs remote access, that is an SSH tunnel to the box
  (`LINUX_SETUP.md` §8.2), not a hole in the router.
- **One NTP source.** Point the router, the cameras, the reader and the box at
  the same one. The server clock is what payroll arithmetic uses; camera and
  reader clocks are recorded for audit and never computed with — but two devices
  disagreeing is how an exit appears to precede its own enter in a log someone
  is reading during a dispute.
- **A VLAN for the cameras is better** if the router supports it. Not required;
  record which you chose.

Verify reachability before anything else:

```bash
ping -c3 192.168.10.21
nc -vz 192.168.10.21 554      # camera RTSP
nc -vz 192.168.10.30 4370     # reader
```

On this workstation, the equivalent host-only checks before cabling real
hardware are:

```bash
nc -vz 127.0.0.1 5434        # project Postgres container
nc -vz 127.0.0.1 8554        # mediamtx
curl -fsS http://127.0.0.1:8080/login   # after the UI is running
```

---

## 2. CCTV cameras

### 2.1 Placement — decide this before mounting

Per canteen door, and the gate. These are the M8 survey questions
(`PLAN.md` M8, `ARCHITECTURE.md` §9.1) and they are answered with a ladder, not
a keyboard:

- **Every entrance and exit identified**, including the one nobody mentions.
  A door the system does not watch produces unpaired events, and unpaired means
  flagged, and a day of flags is a day of manual work.
- **Face capture on the walk-through**: mount at roughly head height plus a
  little, angled *down the line of travel*, so people walk toward the lens. A
  ceiling camera looking straight down sees hair.
- **Backlight is the enemy.** A doorway with daylight behind it silhouettes
  everyone. Move the camera to face away from the bright side, or accept that
  this door is a `no_face` factory.
- **The full door width in frame**, with room either side of the line: the
  crossing logic wants samples on both sides of a hysteresis band
  (`min_samples_per_side`), so someone must be visible for a second or so before
  and after crossing.
- **Fixed view.** If the camera is PTZ, set a preset and leave it (ADR-0029);
  record `preset_id` in the config. A camera that gets nudged is a camera whose
  door line now measures something else.
- **Power and network**: PoE or a socket, and cable run. Note it on the table in
  §1.

### 2.2 Camera settings

On each camera's own web interface:

| Setting | Value | Why |
|---|---|---|
| Codec | **H.264** (not H.265/HEVC) | The packet/evidence path is built and tested around H.264 packets; remux is preferred, with decode+encode as a visible fallback (ADR-0031) |
| Main stream | 1080p, 15–25 fps, CBR | This is the evidence and what a clip contains |
| **I-frame interval (GOP)** | **= the frame rate** (1 second) | Clips are GOP-aligned; a 4-second GOP makes a clip start up to 4 seconds early or fail to extract |
| Sub stream | 640×360–720p, same codec | This is what inference decodes (`analysis_uri`, ADR-0031) |
| Audio | Off | Not used, and it is a privacy liability nobody authorised |
| OSD timestamp | Off or corner | A burned-in clock over a face is a lost face |
| Time | NTP, same source as §1 | |
| Account | **A view-only account per camera** (ADR-0029) | The ingest account must not be able to move, reconfigure or wipe the camera |

### 2.3 Get the RTSP URLs and verify them

Vendor URL shapes (verify, do not trust):

```
Dahua / Imou :  rtsp://USER:PASS@IP:554/cam/realmonitor?channel=1&subtype=0   # main
                rtsp://USER:PASS@IP:554/cam/realmonitor?channel=1&subtype=1   # sub
Hikvision    :  rtsp://USER:PASS@IP:554/Streaming/Channels/101                # main
                rtsp://USER:PASS@IP:554/Streaming/Channels/102                # sub
DVR/NVR      :  the same, with channel= or /10N per channel number
```

Prove each one with the box's own tools before it goes anywhere near config:

```bash
ffprobe -v error -rtsp_transport tcp -show_streams \
  "rtsp://USER:PASS@192.168.10.21:554/cam/realmonitor?channel=1&subtype=0" \
  | grep -E "codec_name|width|height|avg_frame_rate"
```

Want: `codec_name=h264`, the resolution you set, the frame rate you set.

**The two-client check** (ADR-0031, open until measured). Ingest opens the main
stream for evidence *and* the sub stream for analysis — two concurrent clients
per camera. Some cameras and most cheap DVRs refuse the second. Test it:

```bash
ffprobe -v error -rtsp_transport tcp -i "<MAIN URL>" -show_streams >/dev/null &
ffprobe -v error -rtsp_transport tcp -i "<SUB URL>"  -show_streams >/dev/null
wait
```

If the second fails, that camera runs single-stream: leave `analysis_uri` unset
and it analyses the main stream (`CameraConfig.inference_uri` falls back on
purpose). Record which cameras needed this.

### 2.4 Measuring the door line

`door_line` is `[x1, y1, x2, y2]` as **fractions of the frame**, never pixels —
main and sub streams are different sizes and pixel coordinates would be
reinterpreted silently.

```bash
# one frame from the stream the pipeline will analyse
ffmpeg -rtsp_transport tcp -i "<SUB URL>" -frames:v 1 door1.png
```

Open `door1.png` in any image viewer that shows cursor coordinates. Pick the two
ends of the line people cross — the threshold itself, not the door frame — and
divide: `x_fraction = x_pixels / image_width`, same for y.

Then the sign convention, which is the part that gets reversed:

> For a line drawn **top to bottom**, `inside_sign: 1` means **inside is east**
> (larger x) of the line. Walking east is *entering*.

If the geometry is the other way round at that door, use `inside_sign: -1`. Do
not swap the line's endpoints to fix a direction — endpoint order and sign
together decide, and changing both gets you back where you started.

**Verify with your own feet**, once ingest is running: walk in, walk out, and
read the two rows. If enter and exit are swapped, flip `inside_sign`. This takes
two minutes and it is the difference between charging someone for the time they
were at work and the time they were at lunch.

### 2.5 Credentials, and one schema trap worth knowing

The camera row in the database may not contain a credential — a `CHECK`
constraint refuses `user:pass@` in a stored URI, and config keeps the
**un-expanded** `${VAR}` template precisely so a password never reaches a row, a
log or a diff.

The trap: `rtsp://${CAMERA_USER}:${CAMERA_PASSWORD}@host/path` **also matches**
that constraint — the check sees `something:something@` and cannot tell a
variable from a password. Ingest would fail on `camera_source_uri_has_no_credentials`.

**So put the whole URI in one variable.** In `config/secrets.env` (mode 600):

```sh
CANTEEN_01_URI='rtsp://argus:the-password@192.168.10.21:554/cam/realmonitor?channel=1&subtype=0'
CANTEEN_01_SUB='rtsp://argus:the-password@192.168.10.21:554/cam/realmonitor?channel=1&subtype=1'
```

and in the site config only the variable name appears, which is what is stored:

```yaml
  - camera_id: canteen_door_01
    role: canteen_door
    space_id: canteen
    door_id: c1
    direction_hint: east
    source_uri: ${CANTEEN_01_URI}
    analysis_uri: ${CANTEEN_01_SUB}
    is_virtual: false
    door_line: [0.48, 0.05, 0.52, 0.95]     # measured in §2.4
    inside_sign: 1
    analysis_fps: 15
```

### 2.6 The site config

Copy `config/staging_canteen.yaml` to `config/site.yaml`, replace the camera
block with the real rows, and keep everything else. `is_virtual: false` is the
only other change that matters — it is how a row says the footage was a building
and not the rig.

```bash
cp config/staging_canteen.yaml config/site.yaml
# edit the cameras: block
uv run --no-sync python -c "from argus.common.config import load_config; load_config('config/site.yaml')"
export ARGUS_DATABASE__DSN='postgresql://argus:argus@localhost:5434/argus'
uv run --no-sync python -m argus.ingest --config config/site.yaml
```

The copied staging file already contains the explicit `ui.role_passphrases`
mapping, so the four `UI_*_PASSPHRASE` values in the mode-600 secrets file are
usable. The load check catches a bad `door_line`, a missing `space_id` on a
canteen door and an unset `${VAR}` before a camera ever gets opened. The
secrets file is excluded from Docker build contexts; the Linux Compose file
mounts the config directory at the process-relative path used by the loader at
runtime rather than baking secrets into an image or exporting every secret as
an environment variable.

First run, watch for: each camera reaching `state=up`, `frames` climbing,
`reconnects` staying at 0, and no `stream_gap` rows accumulating.

### 2.7 What is verified on this workstation

The following is **not** a camera installation result; it is the virtual-rig
baseline that must pass before real hardware is connected:

- four local H.264 RTSP streams publish through mediamtx on `127.0.0.1:8554`;
- native ingest selected `hardware decode via cuda` for all four streams;
- the mock canteen detector wrote doorway events and pairing/report/gate rows;
- the console is running on `127.0.0.1:8080` and is reached through an
  SSH tunnel.

No camera, door line, two-client result, GOP setting, or badge assignment has
been verified. Keep those boxes below unchecked until the corresponding device
is physically present and measured.

---

## 3. The ZKTeco badge reader

### 3.1 What the code expects

`argus.pipelines.gate.zkt` speaks the ZKTeco TCP protocol directly — connect,
subscribe to live events, read the attendance log, disconnect:

- **TCP port 4370** (`gate.port`, default 4370).
- A **communication key / device password**, if the device has one set
  (`gate.password`, from `ZKT_PASSWORD` in `secrets.env`). A wrong one comes
  back as `reader refused the connection: wrong or missing password`.
- Two channels: **live** taps (verification is attempted) and **replay** from
  the device's own log for windows when nothing was listening. Replayed taps are
  always `not_attempted` — the person walked past hours ago and nothing was
  compared. That distinction is in the schema, not just the code.

### 3.2 Device setup

On the reader's keypad or its web interface:

1. **Network**: static IP from §1, correct subnet mask and gateway.
2. **Communication**: enable Ethernet/TCP, confirm the port is 4370, note the
   comm key if one is set. Disable the cloud/ADMS push service — this system
   pulls, and a device talking to a vendor cloud is a data-export decision
   nobody made (`AGENTS.md` §2.6).
3. **Time**: set it, and point it at the same NTP source as everything else. The
   reader's own timestamps are stored for audit and never used in arithmetic,
   but a reader six hours out makes a replay window unreadable by a human.
4. **Badges**: register the cards. The badge number the device reports is what
   the system will look up.

Verify from the box:

```bash
ping -c3 192.168.10.30
nc -vz 192.168.10.30 4370          # must connect
```

### 3.3 Map badges to people

A tap is only attributable if the badge is assigned. Enrolment is the only path,
`--by` is mandatory and consent is enforced twice:

```bash
uv run --no-sync argus-enrol --config config/site.yaml person add   --person p1 --employee-ref HR-1 --by "your name"
uv run --no-sync argus-enrol --config config/site.yaml consent record --person p1 --expires 2026-12-31 --by "your name"
uv run --no-sync argus-enrol --config config/site.yaml badge assign --person p1 --badge 0012345 --by "your name"
uv run --no-sync argus-enrol --config config/site.yaml list
```

Badge assignment is historical: a tap resolves to whoever held that badge **at
that moment**, so reassigning a card later does not rewrite the past.

### 3.4 Switch the gate service to the real reader

In `config/site.yaml`:

```yaml
gate:
  tap_source: zkt              # was: simulated
  host: 192.168.10.30
  port: 4370
  password: ${ZKT_PASSWORD}    # set in config/secrets.env, mode 600
  reader_id: gate_reader_01
  camera_id: gate_door
  poll_interval_s: 1.0
  dedupe_window_s: 5.0
  reader_gap_timeout_s: 30.0
  replay_lookback_hours: 24
```

Then, in this order:

```bash
# 1. the path itself, with no hardware involved — proves db, camera row, writes
uv run --no-sync python -m argus.gate --config config/staging_canteen.yaml --once --badge B-1

# 2. the reader's own log for the last day: read-only, no live subscription
uv run --no-sync python -m argus.gate --config config/site.yaml --replay-since 2026-09-20T00:00:00Z

# 3. live
uv run --no-sync python -m argus.gate --config config/site.yaml
```

Step 2 before step 3 on purpose: reading the log exercises connect, auth,
framing and record parsing without depending on the live-event subscription,
which is the least certain part of the client.

Then tap a card and watch for a `gate_tap` row and a `gate_event`. **The tap is
written before verification is attempted** — a crash between the two loses a
verification result and never the attendance evidence.

### 3.5 What the outcomes mean

Until a gate threshold is measured, every tap resolves to `not_attempted`, and
that is correct rather than broken (ADR-0010):

| Outcome | Meaning |
|---|---|
| `not_attempted` | Replayed tap, or no template for the badge holder, or no measured threshold. Nothing was compared |
| `no_face` | Nobody usable in the camera's verification window. **A camera problem** |
| `false` | A different face from the badge holder. **A person problem**, and a review item with a clip — never a block, never an alarm |
| `true` | Matched the badge holder |

`no_face` never collapses into `false`. Not seeing a face and seeing the wrong
face are the only distinction this pipeline exists to make.

### 3.6 When the reader does not answer

Expected, the first time. Work down this list:

| Symptom | Likely cause |
|---|---|
| `nc` cannot connect | Wrong IP/subnet, Ethernet not enabled, or the device is on the vendor cloud mode only |
| `reader refused the connection: wrong or missing password` | Comm key mismatch — set `ZKT_PASSWORD`, or clear it on the device |
| Connects, no taps arrive | The live-event subscription. Use `--replay-since` and poll the log while you debug |
| `not a ZK TCP frame: b'...'` | A different protocol dialect than the one implemented. Capture and read it |
| `reader_gap` rows appearing | The service was not listening. That is the point of the row — the window is recorded, not lost |

Capture the conversation rather than guessing:

```bash
sudo tcpdump -i any -n host 192.168.10.30 and port 4370 -w /tmp/zkt.pcap
```

If the dialect does not match, keep `tap_source: simulated` and demo the path.
A reader that needs protocol work is a known risk; a demo that silently invents
attendance is not recoverable.

---

## 4. Before you call it installed

The first group is the **virtual baseline** verified on 2026-09-24; the second
group is intentionally open until real devices are present:

- [x] Virtual rig: mediamtx local-only, four H.264 streams, mock detector
- [x] Virtual ingest: all streams `state=up`, CUDA decode, events and clips written
- [x] UI config loads with all four explicit role passphrases; `secrets.env` is mode 600
- [x] No project port is published on the LAN; UI is localhost/SSH-tunnel only
- [ ] Every real device pings, and `nc` reaches 554 / 4370
- [ ] Every camera is H.264, GOP = fps, sub stream configured
- [ ] `ffprobe` succeeds on main and sub for every camera
- [ ] Two-client test recorded per camera (pass, or single-stream fallback)
- [ ] Door lines measured, and **walked through** to confirm enter/exit direction
- [ ] All URIs are whole-URI `${VAR}`s; `config/secrets.env` is mode 600
- [ ] `load_config('config/site.yaml')` passes
- [ ] Ingest: every real camera `state=up`, reconnects 0, no gap rows accumulating
- [ ] A real-camera clip extracts and plays, and starts at a keyframe
- [ ] Badges assigned to people with recorded consent
- [ ] A live tap produces `gate_tap` + `gate_event`
- [ ] Clocks: box, cameras and reader all on one NTP source
- [ ] No port forwarding on the router; console reached by SSH tunnel only

---

## 5. Once real people are in frame

The moment the cameras see workers rather than volunteers, the rules in
`RISKS.md` stop being theoretical:

- Face templates need recorded consent (enforced in code, twice).
- Enrolment images are retained in the clear **for the demo roster only**
  (ADR-0027). That does not survive contact with real workers — a decision is
  owed before it does.
- Clips are evidence about people. Every view is logged, including refusals.
- Shadow mode stays on. There is no export, and a structural test asserts its
  absence. Turning any of that around is a human sign-off plus an ADR
  (`AGENTS.md` §2), not a config change.
- Site footage does not leave the site — not to a cloud GPU, not to an error
  tracker, not to a vendor API (`AGENTS.md` §2.6, `RISKS.md` §2).

---

## 6. Live detection demo

A presentation view: person boxes with a track number, and anonymous per-zone
headcounts, drawn over a recording or a live camera and served to a browser by
mediamtx. It is `rig/bin/demo_overlay.py`.

What it is **not**: it writes nothing to the database, identifies nobody, and
feeds nothing to pairing. Its tracker is display-only (position and motion,
never appearance, ADR-0002) and is deliberately not the `ShortTracker` the
canteen pipeline uses. Adding a face or a name to it is the AGENTS.md §2 gate
(#3 and #7), not a demo tweak.

### 6.1 Once, after pulling

```bash
cd ~/Documents/cctv-argus-vision
git pull
uv sync --frozen --all-packages --group staging      # only if uv.lock changed
uv run --no-sync python models/fetch.py              # verifies yolo26m from ~/models
```

`models/fetch.py` expects `~/models/yolo26m.onnx` (the pinned `file://` url,
sha256 `984a899c…`). If it reports a hash mismatch, the file there is not the
one the reference was made from — re-copy it; do not edit the hash.

### 6.2 Start it

```bash
cd ~/Documents/cctv-argus-vision
sg docker -c 'ARGUS_PG_HOST_PORT=5434 docker compose -f infra/compose.dev.yaml up -d mediamtx'

# a recording, looped in real time (YOLO on the GPU)
uv run --no-sync python rig/bin/demo_overlay.py \
  --source "$HOME/Documents/Footage/Camera 12 (BestBuy)/A12_20260925101800.mp4" \
  --backend onnx-cuda-yolo
```

It prints `publishing rtsp://localhost:8554/demo` once the first frame is out.
Leave it running; Ctrl-C stops it (from another terminal:
`pkill -f "rig/bin/demo_overlay.py"`).

Watch it:

- **On the box:** a browser at <http://localhost:8888/demo/> (use `localhost`,
  not `127.0.0.1` — mediamtx's cookie check can refuse the latter), or VLC →
  Open Network Stream → `rtsp://localhost:8554/demo` for lower latency.
- **From another machine:** `ssh -N -L 8888:localhost:8888 raco-ai@<box>`, then
  the same URL there. Nothing is published on the LAN.

The HLS page runs a few seconds behind; that is the protocol, not the box.

### 6.3 Options

| Flag | Default | Use |
|---|---|---|
| `--source` | — | a video file, or `rtsp://USER:PASS@IP:554/...` for a live camera |
| `--backend` | `onnx-cuda` (SSD) | `onnx-cuda-yolo` for the demo; SSD misses small and distant people |
| `--start` | `0` | seconds into a file, to open on a busy moment |
| `--high` / `--low` | `0.35` / `0.15` | score that starts a track / that keeps one alive |
| `--zones` | Camera 12 zones | YAML list of `{name, colour, label, polygon}`, fractions of the frame |
| `--path` | `demo` | mediamtx path, so two cameras can run side by side |

A person is in a zone when the bottom-centre of their box — their feet — is
inside its polygon; earlier zones win where polygons overlap. The Camera 12
defaults (`CAMERA_12_ZONES` in the script) are Shop entrance, Shopfront walkway
and Street / sidewalk. For another camera, grab a frame
(`ffmpeg -i <source> -frames:v 1 frame.png`), read off the corners as fractions,
and write a zones file:

```yaml
- name: Doorway
  colour: [255, 60, 60]
  label: [0.60, 0.20]
  polygon: [[0.55, 0.20], [0.80, 0.20], [0.80, 0.95], [0.55, 0.95]]
```

### 6.4 What to expect

Measured on this box with YOLO on Camera 12: a steady 15 fps (the camera's own
rate), 5–6 people tracked at once, about 6% GPU. The detector costs ~36 ms a
frame, of which ~27 ms is CPU letterboxing that stays pure numpy for parity
(ARCHITECTURE.md §5.3). A solid box is a detection this frame; a dashed box is a
track coasting on its prediction through a short miss (dropped after 0.8 s).

The Camera 12 recordings are HEVC. The demo decodes them fine; the ingest
evidence path still wants H.264 (§2.2), so set a camera to H.264 before it goes
anywhere near `config/site.yaml`.
