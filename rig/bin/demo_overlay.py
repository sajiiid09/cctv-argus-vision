"""Demo overlay: tracked person boxes and anonymous zone counts, published to mediamtx.

Presentation only. Nothing is written to the database and nobody is identified.
Track numbers are process-local and die with the process; association is by
position and predicted motion only, never appearance (ADR-0002). Zone counts are
anonymous headcounts. The detector comes from the backend registry like
everywhere else.

This tracker is deliberately *not* `argus.pipelines.tracking.ShortTracker`: that
one produces the doorway crossings payroll reads, and changing it needs an ADR.
This one only has to look steady on a screen.

    python rig/bin/demo_overlay.py --source FILE_OR_RTSP_URL [--path demo] [--zones FILE]

Watch it at http://localhost:8888/<path>/ (HLS, served by mediamtx). From
another machine, tunnel first: ssh -L 8888:localhost:8888 <box>.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

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

# Camera 12 (BestBuy). Polygons are fractions of the frame, never pixels, and a
# person is in a zone when the bottom-centre of their box (their feet) is.
# Earlier zones win where polygons overlap.
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


# --- drawing ------------------------------------------------------------------

# 3x5 bitmap digits for track numbers; ffmpeg's drawtext cannot follow a box.
_GLYPHS = {
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111",
    "3": "111001111001111", "4": "101101111001001", "5": "111100111001111",
    "6": "111100111101111", "7": "111001010010010", "8": "111101111101111",
    "9": "111101111001111",
}  # fmt: skip


def draw_label(img: np.ndarray, x: int, y: int, text: str, colour, scale: int = 3) -> None:
    """A filled tag in the track colour with black digits, bottom-left at (x, y)."""
    h, w = img.shape[:2]
    tw, th = len(text) * 4 * scale + scale, 7 * scale
    x, y = max(0, min(x, w - tw)), max(th, min(y, h))
    img[y - th : y, x : x + tw] = colour
    for i, ch in enumerate(text):
        glyph = np.array([int(b) for b in _GLYPHS[ch]], dtype=bool).reshape(5, 3)
        big = np.kron(glyph, np.ones((scale, scale), dtype=bool))
        gx, gy = x + scale + i * 4 * scale, y - th + scale
        img[gy : gy + 5 * scale, gx : gx + 3 * scale][big] = 0


def draw_box(img: np.ndarray, box: Box, colour, t: int = 3, dashed: bool = False) -> None:
    h, w = img.shape[:2]
    x1, x2 = (int(np.clip(v, 0, w - 1)) for v in (box.x1, box.x2))
    y1, y2 = (int(np.clip(v, 0, h - 1)) for v in (box.y1, box.y2))
    if x2 - x1 <= 2 * t or y2 - y1 <= 2 * t:
        return
    step = 20 if dashed else max(x2 - x1, y2 - y1)  # dashed: a coasting, predicted track
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
    label_at: tuple[int, int]  # pixels, top-left of the name tag
    mask: np.ndarray  # bool, frame-sized
    outline: np.ndarray  # bool, frame-sized


def _inside(poly: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Even-odd ray casting, vectorised over points."""
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
    """Blend every zone tint and outline in one fixed-point pass.

    The per-pixel alpha and premultiplied colour depend only on which zones are
    occupied, so they are built once per combination; per-zone boolean indexing
    of a full frame was the slowest thing in the loop.
    """
    key = frozenset(occupied)
    if key not in _TINTS:
        alpha = np.zeros(img.shape[:2], dtype=np.uint16)
        colour = np.zeros(img.shape, dtype=np.uint16)
        for z in reversed(zones):  # earlier zones win, so they paint last
            a = 64 if z.name in occupied else 16  # of 256
            alpha[z.mask], colour[z.mask] = a, z.colour
            alpha[z.outline], colour[z.outline] = 256, z.colour
        _TINTS[key] = (256 - alpha[..., None], colour * alpha[..., None])
    keep, premul = _TINTS[key]
    img[:] = ((img * keep + premul) >> 8).astype(np.uint8)


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

    def predicted(self, now: float) -> np.ndarray:
        # Extrapolate briefly and ever more timidly: a detector miss is usually
        # a person standing behind a planter, not a person accelerating.
        dt = min(now - self.last_update, 0.5)
        return self.box + self.velocity * dt * (1.0 - dt)

    def correct(self, box: np.ndarray, now: float) -> None:
        dt = max(now - self.last_update, 1e-3)
        prior = self.predicted(now)
        # alpha-beta filter: trust the detection for position, smooth the velocity
        # Velocity of the centre only, applied to both corners: box size noise
        # from a flaky detector must not grow or shrink a coasting box.
        centre = ((box[:2] + box[2:]) - (self.box[:2] + self.box[2:])) / 2 / dt
        self.velocity = 0.7 * self.velocity + 0.3 * np.tile(centre, 2)
        speed_cap = 1.5 * max(box[3] - box[1], 1.0)  # a body height per second, and a half
        self.velocity = np.clip(self.velocity, -speed_cap, speed_cap)
        self.box = 0.75 * box + 0.25 * prior
        self.hits += 1
        self.last_seen = self.last_update = now


class DemoTracker:
    """ByteTrack-style: predicted motion, and a second pass that lets
    low-confidence detections keep an existing track alive (they never start
    one). A track that loses its person coasts on its prediction for
    ``lost_s`` before it is dropped."""

    def __init__(self, high: float, lost_s: float = 1.5, confirm_hits: int = 3) -> None:
        self.high = high
        self.lost_s = lost_s
        self.confirm_hits = confirm_hits
        self.tracks: list[DemoTrack] = []
        self._next_id = 1

    def _match(self, tracks, dets, now, gate) -> tuple[list, list, list]:
        pairs = []
        for ti, t in enumerate(tracks):
            p = t.predicted(now)
            ph = max(p[3] - p[1], 1.0)
            for di, d in enumerate(dets):
                iou = _iou(p, d)
                # small people move more than their own width between frames;
                # accept a near centre when the boxes do not overlap enough
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

    def update(self, dets: list[Detection], now: float) -> None:
        def arr(ds):
            return [np.array([d.box.x1, d.box.y1, d.box.x2, d.box.y2]) for d in ds]

        high = arr([d for d in dets if d.score >= self.high])
        low = arr([d for d in dets if d.score < self.high])
        matched, rest, new = self._match(self.tracks, high, now, gate=0.2)
        matched2, _, _ = self._match(rest, low, now, gate=0.3)
        for t, box in matched + matched2:
            t.correct(box, now)
        for box in new:
            self.tracks.append(DemoTrack(0, box, last_seen=now, last_update=now))
        for t in self.tracks:
            if t.track_id == 0 and t.hits >= self.confirm_hits:
                t.track_id = self._next_id
                self._next_id += 1
        self.tracks = [
            t
            for t in self.tracks
            if now - t.last_seen <= (self.lost_s if t.hits >= self.confirm_hits else 0.2)
        ]

    def visible(self, now: float) -> list[tuple[DemoTrack, np.ndarray, bool]]:
        """(track, box to draw, coasting) for confirmed tracks. A miss or two is
        normal for this detector; only a longer silence is shown as coasting."""
        return [
            (t, t.predicted(now), now - t.last_seen > 0.25)
            for t in self.tracks
            if t.hits >= self.confirm_hits and now - t.last_seen <= 0.8
        ]


# --- plumbing -----------------------------------------------------------------


def publisher(width: int, height: int, fps: int, url: str, hud: Path, zones) -> subprocess.Popen:
    """ffmpeg reads raw RGB on stdin, burns in text, pushes H.264 over RTSP."""
    labels = [
        f"drawtext=fontfile={FONT}:text='{z.name}':fontsize=18:fontcolor=white"
        f":box=1:boxcolor=0x{z.colour[0]:02x}{z.colour[1]:02x}{z.colour[2]:02x}@0.75"
        f":boxborderw=5:x={z.label_at[0]}:y={z.label_at[1]}"
        for z in zones
    ]
    hud_text = (
        f"drawtext=fontfile={FONT}:textfile={hud}:reload=1:fontsize=20:fontcolor=white"
        ":box=1:boxcolor=black@0.65:boxborderw=8:x=12:y=h-th-20:line_spacing=6"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps),
        "-i", "-",
        "-vf", ",".join([*labels, hud_text]),
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-g", str(fps), "-bf", "0", "-pix_fmt", "yuv420p",
        "-rtsp_transport", "tcp", "-f", "rtsp", url,
    ]  # fmt: skip
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def write_hud(hud: Path, text: str) -> None:
    tmp = hud.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, hud)  # drawtext re-reads every frame; never let it see half a file


def frames(source: str, start_s: float, loop: bool):
    """Yield decoded frames. A file is paced to real time and looped; a URL is not."""
    is_url = "://" in source
    while True:
        opts = {"rtsp_transport": "tcp"} if source.startswith("rtsp") else {}
        container = av.open(source, options=opts, timeout=10.0 if is_url else None)
        stream = container.streams.video[0]
        if start_s and not is_url:
            container.seek(int(start_s / stream.time_base), stream=stream)
        wall0 = pts0 = None
        for frame in container.decode(stream):
            if not is_url and frame.pts is not None:
                pts = float(frame.pts * stream.time_base)
                if wall0 is None:
                    wall0, pts0 = time.monotonic(), pts
                ahead = (pts - pts0) - (time.monotonic() - wall0)
                if ahead > 0:
                    time.sleep(ahead)
            yield frame
        container.close()
        if is_url or not loop:
            return
        yield None  # tells the caller the recording restarted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="video file or rtsp:// URL")
    parser.add_argument("--path", default="demo", help="mediamtx path to publish to")
    parser.add_argument("--rtsp", default="rtsp://localhost:8554")
    parser.add_argument("--backend", default="onnx-cuda")
    parser.add_argument("--high", type=float, default=0.35, help="score that starts a track")
    parser.add_argument("--low", type=float, default=0.15, help="score that keeps one alive")
    parser.add_argument("--zones", type=Path, help="YAML list of {name, colour, polygon}")
    parser.add_argument("--start", type=float, default=0.0, help="seconds into a file")
    parser.add_argument("--fps", type=int, default=15, help="output frame rate")
    parser.add_argument("--no-loop", action="store_true")
    args = parser.parse_args()

    av.logging.set_level(av.logging.FATAL)  # HEVC seeks log missing-reference noise
    detector = get_detector(args.backend)
    zone_spec = yaml.safe_load(args.zones.read_text()) if args.zones else CAMERA_12_ZONES
    tracker = DemoTracker(high=args.high)
    hud = Path(tempfile.gettempdir()) / f"argus_demo_{args.path}.txt"
    write_hud(hud, "ARGUS  starting...")
    url = f"{args.rtsp.rstrip('/')}/{args.path}"
    proc, zones = None, []
    t0, shown, rate = time.monotonic(), 0, 0.0
    try:
        for frame in frames(args.source, args.start, loop=not args.no_loop):
            if frame is None:
                tracker = DemoTracker(high=args.high)
                continue
            img = frame.to_ndarray(format="rgb24")
            if proc is None:
                zones = build_zones(zone_spec, img.shape[1], img.shape[0])
                proc = publisher(img.shape[1], img.shape[0], args.fps, url, hud, zones)
                print(f"publishing {url}  watch: http://localhost:8888/{args.path}/", flush=True)
            now = time.monotonic()
            people = [
                d for d in detector.detect(img, score_threshold=args.low) if d.label == "person"
            ]
            tracker.update(people, now)

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
