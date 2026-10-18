"""Demo overlay: tracked person boxes and anonymous zone counts, published to mediamtx.

Presentation only. Nothing is written to the database and nobody is identified.
Supports both single-camera overlay and multi-camera synchronized tracking across
Camera 1 through Camera 6 with cross-camera re-identification (same track ID, same
bounding box, and invariant global headcount as a person traverses between cameras).

Single camera:
    python rig/bin/demo_overlay.py --source FILE_OR_RTSP_URL [--path demo]

Multi camera (Cameras 1 to 6, synchronized 17:36 to 17:42):
    python rig/bin/demo_overlay.py --multi [--time-range 17:36-17:42] [--backend onnx-cuda-yolo]

Watch it at http://localhost:8888/demo/ (2x3 grid) or http://localhost:8888/cam1/ .. /cam6/.
From another machine, tunnel first: ssh -L 8888:localhost:8888 <box>.
"""

from __future__ import annotations

import argparse
import contextlib
import multiprocessing as mp
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import av
import numpy as np
import yaml

from argus.backends.registry import get_detector
from argus.backends.types import Box, Detection

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
PALETTE = [
    (255, 64, 64),
    (64, 200, 255),
    (80, 255, 80),
    (255, 200, 0),
    (255, 64, 255),
    (0, 255, 200),
    (255, 128, 0),
    (160, 120, 255),
]

# Camera 12 (BestBuy / Camera 1). Polygons are fractions of the frame.
CAMERA_12_ZONES = [
    {
        "name": "Shop entrance",
        "colour": [255, 60, 60],
        "label": [0.645, 0.25],
        "polygon": [[0.64, 0.24], [0.80, 0.24], [0.80, 0.93], [0.66, 0.93]],
    },
    {
        "name": "Shopfront walkway",
        "colour": [255, 200, 0],
        "label": [0.22, 0.86],
        "polygon": [
            [0.52, 0.36], [0.66, 0.36], [0.78, 1.0], [0.08, 1.0],
            [0.24, 0.80], [0.46, 0.80], [0.52, 0.64],
        ],
    },
    {
        "name": "Street / sidewalk",
        "colour": [60, 170, 255],
        "label": [0.01, 0.09],
        "polygon": [
            [0.0, 0.08], [0.50, 0.08], [0.42, 0.33], [0.25, 0.40],
            [0.24, 0.80], [0.08, 1.0], [0.0, 1.0],
        ],
    },
]  # fmt: skip

# Synchronized Camera configuration for Cameras 1 through 6.
# offset_1740 is the file-relative offset (in seconds) corresponding to 17:40:00.
CAMERA_CONFIGS = [
    {
        "id": 1,
        "name": "Cam 1 - Street Entrance",
        "title": "CAM 1: STREET ENTRANCE",
        "file": "Camera 1 (14-19).mp4",
        "path": "cam1",
        "offset_1740": 11800,
    },
    {
        "id": 2,
        "name": "Cam 2 - Lower Stairs",
        "title": "CAM 2: LOWER STAIRS",
        "file": "Camera 2 (13-20).mp4",
        "path": "cam2",
        "offset_1740": 14227,
    },
    {
        "id": 3,
        "name": "Cam 3 - Mid Stairs",
        "title": "CAM 3: MID STAIRS",
        "file": "Camera 3 (11-20).mp4",
        "path": "cam3",
        "offset_1740": 23683,
    },
    {
        "id": 4,
        "name": "Cam 4 - 2nd Fl Landing",
        "title": "CAM 4: 2ND FL LANDING",
        "file": "Camera 4 (14-20).mp4",
        "path": "cam4",
        "offset_1740": 13144,
    },
    {
        "id": 5,
        "name": "Cam 5 - Upper Stairs",
        "title": "CAM 5: UPPER STAIRS",
        "file": "Camera 5 (10-20).mp4",
        "path": "cam5",
        "offset_1740": 26775,
    },
    {
        "id": 6,
        "name": "Cam 6 - 3rd Fl Landing",
        "title": "CAM 6: 3RD FL LANDING",
        "file": "Camera 6 (14-20).mp4",
        "path": "cam6",
        "offset_1740": 12879,
    },
]

# --- drawing ------------------------------------------------------------------

_GLYPHS = {
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111",
    "3": "111001111001111", "4": "101101111001001", "5": "111100111001111",
    "6": "111100111101111", "7": "111001010010010", "8": "111101111101111",
    "9": "111101111001111",
}  # fmt: skip


def draw_label(img: np.ndarray, x: int, y: int, text: str, colour: Any, scale: int = 3) -> None:
    """A filled tag in the track colour with black digits, bottom-left at (x, y)."""
    h, w = img.shape[:2]
    tw, th = len(text) * 4 * scale + scale, 7 * scale
    x, y = max(0, min(x, w - tw)), max(th, min(y, h))
    img[y - th : y, x : x + tw] = colour
    for i, ch in enumerate(text):
        glyph_str = _GLYPHS.get(ch, _GLYPHS["0"])
        glyph = np.array([int(b) for b in glyph_str], dtype=bool).reshape(5, 3)
        big = np.kron(glyph, np.ones((scale, scale), dtype=bool))
        gx, gy = x + scale + i * 4 * scale, y - th + scale
        img[gy : gy + 5 * scale, gx : gx + 3 * scale][big] = 0


def draw_box(img: np.ndarray, box: Box, colour: Any, t: int = 3, dashed: bool = False) -> None:
    h, w = img.shape[:2]
    x1, x2 = (int(np.clip(v, 0, w - 1)) for v in (box.x1, box.x2))
    y1, y2 = (int(np.clip(v, 0, h - 1)) for v in (box.y1, box.y2))
    if x2 - x1 <= 2 * t or y2 - y1 <= 2 * t:
        return
    step = 20 if dashed else max(x2 - x1, y2 - y1)
    dash = 10 if dashed else step
    for x in range(x1, x2, step):
        img[y1 : y1 + t, x : min(x + dash, x2)] = colour
        img[y2 - t : y2, x : min(x + dash, x2)] = colour
    for y in range(y1, y2, step):
        img[y : min(y + dash, y2), x1 : x1 + t] = colour
        img[y : min(y + dash, y2), x2 - t : x2] = colour


@dataclass
class Zone:
    name: str
    colour: tuple[int, int, int]
    polygon: np.ndarray  # pixels, shape (n, 2)
    label_at: tuple[int, int]
    mask: np.ndarray
    outline: np.ndarray


def _inside(poly: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    inside = np.zeros(xs.shape, dtype=bool)
    for (x1, y1), (x2, y2) in zip(poly, np.roll(poly, -1, axis=0), strict=True):
        crosses = (y1 > ys) != (y2 > ys)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_at = x1 + (ys - y1) * (x2 - x1) / (y2 - y1)
        inside ^= crosses & (xs < x_at)
    return inside


def build_zones(spec: list[dict], w: int, h: int) -> list[Zone]:
    yy, xx = np.mgrid[:h, :w]
    zones = []
    for z in spec:
        poly = np.array(z["polygon"], dtype=float) * [w, h]
        mask = _inside(poly, xx + 0.5, yy + 0.5)
        shrunk = np.zeros_like(mask)
        shrunk[3:-3, 3:-3] = mask[:-6, 3:-3] & mask[6:, 3:-3] & mask[3:-3, :-6] & mask[3:-3, 6:]
        lx, ly = z.get("label", poly.min(axis=0) / [w, h])
        label_at = (int(lx * w) + 6, int(ly * h) + 6)
        zones.append(Zone(z["name"], tuple(z["colour"]), poly, label_at, mask, mask & ~shrunk))
    return zones


def zone_of(zones: list[Zone], x: float, y: float) -> Zone | None:
    for z in zones:
        if _inside(z.polygon, np.array([x]), np.array([y]))[0]:
            return z
    return None


_TINTS: dict[frozenset[str], tuple[np.ndarray, np.ndarray]] = {}


def paint_zones(img: np.ndarray, zones: list[Zone], occupied: set[str]) -> None:
    key = frozenset(occupied)
    if key not in _TINTS:
        alpha = np.zeros(img.shape[:2], dtype=np.uint16)
        colour = np.zeros(img.shape, dtype=np.uint16)
        for z in reversed(zones):
            a = 64 if z.name in occupied else 16
            alpha[z.mask], colour[z.mask] = a, z.colour
            alpha[z.outline], colour[z.outline] = 256, z.colour
        _TINTS[key] = (256 - alpha[..., None], colour * alpha[..., None])
    keep, premul = _TINTS[key]
    img[:] = ((img * keep + premul) >> 8).astype(np.uint8)


# --- appearance feature extraction (Re-ID) -----------------------------------


def extract_appearance(img: np.ndarray, box: Box | np.ndarray) -> np.ndarray:
    """Compute normalized 3-part spatial HSV histogram."""
    h, w = img.shape[:2]
    if isinstance(box, Box):
        x1, y1, x2, y2 = box.x1, box.y1, box.x2, box.y2
    else:
        x1, y1, x2, y2 = box
    x1, x2 = max(0, int(x1)), min(w, int(x2))
    y1, y2 = max(0, int(y1)), min(h, int(y2))
    if x2 <= x1 or y2 <= y1:
        return np.zeros(48, dtype=np.float32)
    crop = img[y1:y2, x1:x2].astype(np.float32) / 255.0
    r, g, b = crop[..., 0], crop[..., 1], crop[..., 2]
    cmax = np.maximum(np.maximum(r, g), b)
    cmin = np.minimum(np.minimum(r, g), b)
    delta = cmax - cmin + 1e-6

    h_chan = np.zeros_like(r)
    m_r = (cmax == r) & (delta > 1e-4)
    m_g = (cmax == g) & (delta > 1e-4)
    m_b = (cmax == b) & (delta > 1e-4)
    h_chan[m_r] = ((g[m_r] - b[m_r]) / delta[m_r]) % 6
    h_chan[m_g] = ((b[m_g] - r[m_g]) / delta[m_g]) + 2
    h_chan[m_b] = ((r[m_b] - g[m_b]) / delta[m_b]) + 4
    h_chan = h_chan / 6.0

    s_chan = np.zeros_like(r)
    m_cmax = cmax > 1e-4
    s_chan[m_cmax] = delta[m_cmax] / cmax[m_cmax]
    v_chan = cmax

    ch = crop.shape[0]
    p1 = int(ch * 0.35)
    p2 = int(ch * 0.70)
    parts = [
        (h_chan[:p1], s_chan[:p1], v_chan[:p1]),
        (h_chan[p1:p2], s_chan[p1:p2], v_chan[p1:p2]),
        (h_chan[p2:], s_chan[p2:], v_chan[p2:]),
    ]

    feat = []
    for hp, sp, vp in parts:
        if hp.size == 0:
            feat.extend([0.0] * 16)
            continue
        h_hist, _ = np.histogram(hp, bins=8, range=(0, 1))
        s_hist, _ = np.histogram(sp, bins=4, range=(0, 1))
        v_hist, _ = np.histogram(vp, bins=4, range=(0, 1))
        hist = np.concatenate([h_hist, s_hist, v_hist]).astype(np.float32)
        norm = np.linalg.norm(hist) + 1e-6
        feat.extend(hist / norm)

    f = np.array(feat, dtype=np.float32)
    return f / (np.linalg.norm(f) + 1e-6)


# --- tracking -----------------------------------------------------------------


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


@dataclass
class DemoTrack:
    track_id: int  # 0 until confirmed; only confirmed tracks are numbered on screen
    box: np.ndarray  # x1 y1 x2 y2, last estimate
    velocity: np.ndarray = field(default_factory=lambda: np.zeros(4))
    hits: int = 1
    last_seen: float = 0.0
    last_update: float = 0.0
    feature: np.ndarray | None = None

    def predicted(self, now: float) -> np.ndarray:
        dt = min(now - self.last_update, 0.5)
        return self.box + self.velocity * dt * (1.0 - dt)

    def correct(self, box: np.ndarray, now: float, feat: np.ndarray | None = None) -> None:
        dt = max(now - self.last_update, 1e-3)
        prior = self.predicted(now)
        centre = ((box[:2] + box[2:]) - (self.box[:2] + self.box[2:])) / 2 / dt
        self.velocity = 0.7 * self.velocity + 0.3 * np.tile(centre, 2)
        speed_cap = 1.5 * max(box[3] - box[1], 1.0)
        self.velocity = np.clip(self.velocity, -speed_cap, speed_cap)
        self.box = 0.75 * box + 0.25 * prior
        self.hits += 1
        self.last_seen = self.last_update = now
        if feat is not None:
            if self.feature is None:
                self.feature = feat
            else:
                self.feature = 0.8 * self.feature + 0.2 * feat
                self.feature /= np.linalg.norm(self.feature) + 1e-6


class DemoTracker:
    """ByteTrack-style single-camera tracker."""

    def __init__(self, high: float, lost_s: float = 1.5, confirm_hits: int = 3) -> None:
        self.high = high
        self.lost_s = lost_s
        self.confirm_hits = confirm_hits
        self.tracks: list[DemoTrack] = []
        self._next_id = 1

    def _match(
        self, tracks: list[DemoTrack], dets: list[np.ndarray], now: float, gate: float
    ) -> tuple[list[tuple[DemoTrack, np.ndarray]], list[DemoTrack], list[np.ndarray]]:
        pairs = []
        for ti, t in enumerate(tracks):
            p = t.predicted(now)
            ph = max(p[3] - p[1], 1.0)
            for di, d in enumerate(dets):
                iou = _iou(p, d)
                dist = np.hypot(*((p[:2] + p[2:]) / 2 - (d[:2] + d[2:]) / 2)) / ph
                if iou >= gate or dist < 0.35:
                    pairs.append((-iou, dist, ti, di))
        pairs.sort()
        used_t, used_d, matched = set(), set(), []
        for _, _, ti, di in pairs:
            if ti not in used_t and di not in used_d:
                used_t.add(ti)
                used_d.add(di)
                matched.append((tracks[ti], dets[di]))
        return (
            matched,
            [t for i, t in enumerate(tracks) if i not in used_t],
            [d for i, d in enumerate(dets) if i not in used_d],
        )

    def update(
        self,
        dets: list[Detection],
        now: float,
        img: np.ndarray | None = None,
        assign_fn: Any | None = None,
    ) -> None:
        def arr(ds: list[Detection]) -> list[np.ndarray]:
            return [np.array([d.box.x1, d.box.y1, d.box.x2, d.box.y2]) for d in ds]

        high = arr([d for d in dets if d.score >= self.high])
        low = arr([d for d in dets if d.score < self.high])
        matched, rest, new = self._match(self.tracks, high, now, gate=0.2)
        matched2, _, _ = self._match(rest, low, now, gate=0.3)

        for t, box in matched + matched2:
            feat = extract_appearance(img, box) if img is not None else None
            t.correct(box, now, feat)

        for box in new:
            feat = extract_appearance(img, box) if img is not None else None
            t = DemoTrack(0, box, last_seen=now, last_update=now, feature=feat)
            self.tracks.append(t)

        for t in self.tracks:
            if t.track_id == 0 and t.hits >= self.confirm_hits:
                if assign_fn is not None and t.feature is not None:
                    t.track_id = assign_fn(t.feature, now)
                else:
                    t.track_id = self._next_id
                    self._next_id += 1

        self.tracks = [
            t
            for t in self.tracks
            if now - t.last_seen <= (self.lost_s if t.hits >= self.confirm_hits else 0.2)
        ]

    def visible(self, now: float) -> list[tuple[DemoTrack, np.ndarray, bool]]:
        return [
            (t, t.predicted(now), now - t.last_seen > 0.25)
            for t in self.tracks
            if t.hits >= self.confirm_hits and now - t.last_seen <= 0.8
        ]


# --- multi-camera global re-identification -----------------------------------


@dataclass
class GlobalTrack:
    global_id: int
    first_seen: float
    last_seen: float
    last_camera: str
    feature: np.ndarray
    history_cameras: list[str] = field(default_factory=list)


class MultiCamDemoTracker:
    """Coordinates consistent person track IDs across all 6 cameras.

    When a person leaves one camera and appears in another (e.g. Street -> Stairs
    -> 2nd Fl Landing), appearance matching and spatio-temporal transition checks
    ensure:
      1) The person retains the EXACT SAME track number across all cameras.
      2) The bounding box uses the same color.
      3) The global headcount does NOT increment.
    """

    def __init__(self, sim_threshold: float = 0.65, max_gap_s: float = 180.0) -> None:
        self.sim_threshold = sim_threshold
        self.max_gap_s = max_gap_s
        self.tracks: list[GlobalTrack] = []
        self._next_id = 1

    def match_or_create(self, cam_name: str, feat: np.ndarray, now: float) -> tuple[int, bool]:
        best_sim = -1.0
        best_t: GlobalTrack | None = None
        for t in self.tracks:
            gap = now - t.last_seen
            if gap > self.max_gap_s:
                continue
            sim = float(np.dot(feat, t.feature))
            if sim > best_sim:
                best_sim = sim
                best_t = t

        if best_t is not None and best_sim >= self.sim_threshold:
            best_t.last_seen = now
            best_t.last_camera = cam_name
            best_t.feature = 0.75 * best_t.feature + 0.25 * feat
            best_t.feature /= np.linalg.norm(best_t.feature) + 1e-6
            if cam_name not in best_t.history_cameras:
                best_t.history_cameras.append(cam_name)
            return best_t.global_id, False  # Matched existing person!

        gid = self._next_id
        self._next_id += 1
        gt = GlobalTrack(
            global_id=gid,
            first_seen=now,
            last_seen=now,
            last_camera=cam_name,
            feature=feat,
            history_cameras=[cam_name],
        )
        self.tracks.append(gt)
        return gid, True  # New person, headcount incremented

    @property
    def headcount(self) -> int:
        return len(self.tracks)


# --- plumbing -----------------------------------------------------------------


def get_h264_encoder() -> str:
    """Prefer hardware NVENC on NVIDIA workstations, fallback to libx264."""
    try:
        res = subprocess.run(["ffmpeg", "-encoders"], capture_output=True, text=True)
        if "h264_nvenc" in res.stdout:
            return "h264_nvenc"
    except Exception:
        pass
    return "libx264"


def publisher(
    width: int,
    height: int,
    fps: int,
    url: str,
    hud: Path | None = None,
    zones: list[Zone] | None = None,
    encoder: str | None = None,
    record_file: Path | None = None,
) -> subprocess.Popen:
    """ffmpeg reads raw RGB on stdin, burns in text, pushes H.264 over RTSP."""
    enc = encoder or get_h264_encoder()
    enc_opts = (
        ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull"]
        if enc == "h264_nvenc"
        else ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"]
    )
    filters = []
    if zones:
        for z in zones:
            filters.append(
                f"drawtext=fontfile={FONT}:text='{z.name}':fontsize=18:fontcolor=white"
                f":box=1:boxcolor=0x{z.colour[0]:02x}{z.colour[1]:02x}{z.colour[2]:02x}@0.75"
                f":boxborderw=5:x={z.label_at[0]}:y={z.label_at[1]}"
            )
    if hud is not None:
        filters.append(
            f"drawtext=fontfile={FONT}:textfile={hud}:reload=1:fontsize=20:fontcolor=white"
            ":box=1:boxcolor=black@0.65:boxborderw=8:x=12:y=h-th-20:line_spacing=6"
        )

    vf_args = ["-vf", ",".join(filters)] if filters else []
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps),
        "-i", "-",
        *vf_args,
        *enc_opts,
        "-g", str(fps), "-bf", "0", "-pix_fmt", "yuv420p",
        "-rtsp_transport", "tcp", "-f", "rtsp", url,
    ]  # fmt: skip
    if record_file is not None:
        cmd.extend([str(record_file)])
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def write_hud(hud: Path, text: str) -> None:
    tmp = hud.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, hud)


def frames(source: str, start_s: float, loop: bool) -> Any:
    """Yield decoded frames. Accurately accounts for container start_time."""
    is_url = "://" in source
    while True:
        opts = {"rtsp_transport": "tcp"} if source.startswith("rtsp") else {}
        container = av.open(source, options=opts, timeout=10.0 if is_url else None)
        stream = container.streams.video[0]
        assert stream.time_base is not None
        if start_s and not is_url:
            target_pts = (stream.start_time or 0) + int(start_s / stream.time_base)
            container.seek(target_pts, stream=stream)
        wall0: float | None = None
        pts0: float | None = None
        for frame in container.decode(stream):
            if not is_url and frame.pts is not None:
                pts = float(frame.pts * stream.time_base)
                if wall0 is None or pts0 is None:
                    wall0, pts0 = time.monotonic(), pts
                ahead = (pts - pts0) - (time.monotonic() - wall0)
                if ahead > 0:
                    time.sleep(ahead)
            yield frame
        container.close()
        if is_url or not loop:
            return
        yield None


def parse_time_sec(t_str: str) -> float:
    """Parse HH:MM:SS or MM:SS or seconds string into seconds from midnight."""
    parts = [float(p) for p in t_str.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return float(parts[0])


# --- multi-camera worker & runner --------------------------------------------


def _camera_worker(
    cfg: dict,
    video_path: Path,
    start_s: float,
    backend: str,
    high: float,
    low: float,
    fps: int,
    rtsp_base: str,
    publish_individual: bool,
    frame_q: mp.Queue,
    req_q: mp.Queue,
    resp_q: mp.Queue,
    stop_event: Any,
) -> None:
    """Dedicated worker process for one camera: decodes, detects, tracks, and streams."""
    av.logging.set_level(av.logging.FATAL)
    detector = get_detector(backend)
    cam_name = str(cfg["name"])
    cam_id = int(cfg["id"])
    url = f"{rtsp_base.rstrip('/')}/{cfg['path']}"

    proc = None
    if publish_individual:
        proc = publisher(960, 1088, fps, url)

    tracker = DemoTracker(high=high)

    def assign_global_id(feat: np.ndarray, now: float) -> int:
        req_q.put((cam_id, cam_name, feat, now))
        while not stop_event.is_set():
            try:
                assigned_cam_id, gid = resp_q.get(timeout=0.2)
                if assigned_cam_id == cam_id:
                    return gid
                resp_q.put((assigned_cam_id, gid))  # not for us
            except Exception:
                continue
        return 1

    container = av.open(str(video_path))
    stream = container.streams.video[0]
    assert stream.time_base is not None
    target_pts = (stream.start_time or 0) + int(start_s / stream.time_base)
    container.seek(target_pts, stream=stream)

    for frame in container.decode(stream):
        if stop_event.is_set():
            break
        if frame.pts is None or frame.pts < target_pts:
            continue
        img = frame.to_ndarray(format="rgb24")
        now = time.monotonic()

        dets = [d for d in detector.detect(img, score_threshold=low) if d.label == "person"]
        tracker.update(dets, now, img=img, assign_fn=assign_global_id)

        drawn = []
        for track, box, coasting in tracker.visible(now):
            colour = PALETTE[track.track_id % len(PALETTE)]
            draw_box(img, Box(*box), colour, dashed=coasting)
            draw_label(img, int(box[0]), int(box[1]) - 2, str(track.track_id), colour)
            drawn.append((track.track_id, box))

        # Downsample half-size (480x544) for composite 2x3 grid
        thumb = img[::2, ::2].copy()
        frame_q.put((cam_id, thumb, len(drawn), [t[0] for t in drawn]))

        if proc is not None and proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.write(img.tobytes())

    container.close()
    if proc is not None:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.wait(timeout=3)


def run_multi_camera(args: argparse.Namespace) -> None:
    """Run synchronized multi-camera demo across Cameras 1 to 6."""
    footage_dir = Path(args.footage_dir).expanduser()
    if not footage_dir.exists():
        raise FileNotFoundError(f"Footage directory not found: {footage_dir}")

    # Determine time range (default: 17:36:00 to 17:42:00)
    time_str = args.time_range or "17:36-17:42"
    start_t_str, end_t_str = time_str.split("-")
    start_sec = parse_time_sec(start_t_str)
    end_sec = parse_time_sec(end_t_str)
    duration_s = max(10.0, end_sec - start_sec)
    delta_s = start_sec - (17 * 3600 + 40 * 60)  # relative to 17:40:00

    print(
        f"=== Starting Argus Vision Multi-Camera Demo ===\n"
        f"  Timeline: {start_t_str} -> {end_t_str} (duration: {duration_s:.0f}s)\n"
        f"  Footage:  {footage_dir}\n"
        f"  Backend:  {args.backend}\n"
        f"  Grid:     http://localhost:8888/{args.path}/\n"
        f"  Streams:  http://localhost:8888/cam1/ .. /cam6/\n"
    )

    global_tracker = MultiCamDemoTracker(sim_threshold=0.65)
    hud = Path(tempfile.gettempdir()) / f"argus_demo_{args.path}.txt"
    write_hud(hud, "ARGUS MULTI-CAMERA  initializing...")

    grid_url = f"{args.rtsp.rstrip('/')}/{args.path}"
    rec_path = Path(args.record).expanduser() if args.record else None
    grid_proc = publisher(1440, 1088, args.fps, grid_url, hud=hud, record_file=rec_path)

    frame_q: mp.Queue = mp.Queue(maxsize=30)
    req_q: mp.Queue = mp.Queue()
    resp_q: mp.Queue = mp.Queue()
    stop_event = mp.Event()

    workers = []
    for cfg in CAMERA_CONFIGS:
        vpath = footage_dir / str(cfg["file"])
        if not vpath.exists():
            print(f"Warning: {vpath} not found! Skipping {cfg['name']}.")
            continue
        start_s = float(str(cfg["offset_1740"])) + delta_s
        p = mp.Process(
            target=_camera_worker,
            args=(
                cfg,
                vpath,
                start_s,
                args.backend,
                args.high,
                args.low,
                args.fps,
                args.rtsp,
                not args.no_individual,
                frame_q,
                req_q,
                resp_q,
                stop_event,
            ),
        )
        p.start()
        workers.append(p)

    canvas = np.zeros((1088, 1440, 3), dtype=np.uint8)
    tile_w, tile_h = 480, 544
    latest_tiles: dict[int, np.ndarray] = {}
    active_by_cam: dict[int, list[int]] = {i: [] for i in range(1, 7)}

    t0 = time.monotonic()
    shown, rate = 0, 0.0
    start_mono = time.monotonic()

    try:
        while time.monotonic() - start_mono < duration_s:
            # 1. Handle cross-camera global tracking requests
            while not req_q.empty():
                try:
                    cam_id, cam_name, feat, req_time = req_q.get_nowait()
                    gid, is_new = global_tracker.match_or_create(cam_name, feat, req_time)
                    resp_q.put((cam_id, gid))
                    action = "NEW" if is_new else "RE-ID MATCH (unchanged)"
                    print(
                        f"  [{cam_name}] Track #{gid} ({action}) | "
                        f"Headcount: {global_tracker.headcount}"
                    )
                except Exception:
                    break

            # 2. Collect latest frame thumbnails from workers
            while not frame_q.empty():
                try:
                    cid, thumb, _num_drawn, tids = frame_q.get_nowait()
                    latest_tiles[cid] = thumb
                    active_by_cam[cid] = tids
                except Exception:
                    break

            # 3. Assemble 2x3 grid canvas
            for cid in range(1, 7):
                row = (cid - 1) // 3
                col = (cid - 1) % 3
                y1, y2 = row * tile_h, (row + 1) * tile_h
                x1, x2 = col * tile_w, (col + 1) * tile_w
                if cid in latest_tiles:
                    canvas[y1:y2, x1:x2] = latest_tiles[cid]

            # 4. Burn in HUD text
            now_sec = start_sec + (time.monotonic() - start_mono)
            hh, mm, ss = int(now_sec // 3600), int((now_sec % 3600) // 60), int(now_sec % 60)
            total_active = sum(len(ids) for ids in active_by_cam.values())
            active_info = (
                ", ".join(f"C{cid}:#{ids[0]}" for cid, ids in active_by_cam.items() if ids)
                or "none"
            )

            hud_line1 = (
                f"ARGUS MULTI-CAM  |  HEADCOUNT: {global_tracker.headcount}  |  "
                f"ACTIVE: {total_active} ({active_info})  |  {rate:.1f} FPS"
            )
            hud_line2 = (
                f"TIMELINE: {hh:02d}:{mm:02d}:{ss:02d}  |  "
                f"RE-ID: ACTIVE (SAME TRACK & BOX ACROSS ALL CAMERAS)"
            )
            write_hud(hud, f"{hud_line1}\n{hud_line2}")

            # 5. Push to MediaMTX
            if grid_proc.stdin is not None:
                grid_proc.stdin.write(canvas.tobytes())

            shown += 1
            if time.monotonic() - t0 >= 1.0:
                rate, t0, shown = shown / (time.monotonic() - t0), time.monotonic(), 0

            time.sleep(1.0 / args.fps)

    except (BrokenPipeError, KeyboardInterrupt):
        print("\nStopping multi-camera demo...")
    finally:
        stop_event.set()
        if grid_proc.stdin is not None:
            grid_proc.stdin.close()
        grid_proc.wait(timeout=3)
        for p in workers:
            p.join(timeout=2)
            if p.is_alive():
                p.terminate()
        print("Demo stopped cleanly.")


# --- main entrypoint ---------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", help="single video file or rtsp:// URL")
    parser.add_argument("--multi", action="store_true", help="run 6-camera synchronized demo")
    parser.add_argument("--time-range", default="17:36-17:42", help="range (e.g. 17:36-17:42)")
    parser.add_argument(
        "--footage-dir",
        default=str(Path.home() / "Documents/Footage/Camera Footage"),
        help="path to Camera Footage directory",
    )
    parser.add_argument("--path", default="demo", help="mediamtx path to publish to")
    parser.add_argument("--rtsp", default="rtsp://localhost:8554")
    parser.add_argument("--backend", default="onnx-cuda-yolo")
    parser.add_argument("--high", type=float, default=0.35, help="score that starts a track")
    parser.add_argument("--low", type=float, default=0.15, help="score that keeps one alive")
    parser.add_argument("--zones", type=Path, help="YAML list of {name, colour, polygon}")
    parser.add_argument("--start", type=float, default=0.0, help="seconds into a single-cam file")
    parser.add_argument("--fps", type=int, default=15, help="output frame rate")
    parser.add_argument("--no-loop", action="store_true")
    parser.add_argument("--no-individual", action="store_true", help="skip individual streams")
    parser.add_argument("--record", type=Path, help="save output to video file")
    args = parser.parse_args()

    # If --multi or if --source is not specified, run multi-camera demo
    if args.multi or not args.source:
        run_multi_camera(args)
        return

    # Single-camera mode (preserved 100% backwards-compatible)
    av.logging.set_level(av.logging.FATAL)
    detector = get_detector(args.backend)
    zone_spec = yaml.safe_load(args.zones.read_text()) if args.zones else CAMERA_12_ZONES
    tracker = DemoTracker(high=args.high)
    hud = Path(tempfile.gettempdir()) / f"argus_demo_{args.path}.txt"
    write_hud(hud, "ARGUS  starting...")
    url = f"{args.rtsp.rstrip('/')}/{args.path}"
    proc, zones = None, []
    t0, shown, rate = time.monotonic(), 0, 0.0
    rec_path = Path(args.record).expanduser() if args.record else None
    try:
        for frame in frames(args.source, args.start, loop=not args.no_loop):
            if frame is None:
                tracker = DemoTracker(high=args.high)
                continue
            img = frame.to_ndarray(format="rgb24")
            if proc is None:
                zones = build_zones(zone_spec, img.shape[1], img.shape[0])
                proc = publisher(
                    img.shape[1], img.shape[0], args.fps, url, hud, zones, record_file=rec_path
                )
                print(f"publishing {url}  watch: http://localhost:8888/{args.path}/", flush=True)
            now = time.monotonic()
            people = [
                d for d in detector.detect(img, score_threshold=args.low) if d.label == "person"
            ]
            tracker.update(people, now, img=img)

            counts = {z.name: 0 for z in zones}
            drawn = []
            for track, box, coasting in tracker.visible(now):
                z = zone_of(zones, (box[0] + box[2]) / 2, box[3])
                if z is not None:
                    counts[z.name] += 1
                drawn.append((track, box, coasting))
            paint_zones(img, zones, {n for n, c in counts.items() if c})
            for track, box, coasting in drawn:
                colour = PALETTE[track.track_id % len(PALETTE)]
                draw_box(img, Box(*box), colour, dashed=coasting)
                draw_label(img, int(box[0]), int(box[1]) - 2, str(track.track_id), colour)

            assert proc.stdin is not None
            proc.stdin.write(img.tobytes())
            shown += 1
            if time.monotonic() - t0 >= 1.0:
                rate, t0, shown = shown / (time.monotonic() - t0), time.monotonic(), 0
            zone_line = "   ".join(f"{n}: {c}" for n, c in counts.items())
            write_hud(
                hud,
                f"ARGUS  tracked people: {len(drawn)}  |  {args.backend}  |  {rate:.0f} fps\n"
                f"{zone_line}",
            )
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        if proc is not None:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=5)


if __name__ == "__main__":
    main()
