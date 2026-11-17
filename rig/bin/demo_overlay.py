"""Demo overlay: tracked person boxes, stairwell counting, and best-score face snapshots.

Supports both single-camera overlay and six-camera synchronized/live views:
- Camera 1 (A12 - Street Entrance) runs local-only person tracking and is excluded from
  the stairwell cross-camera person counter and face snapshot pipeline.
- Cameras 2 through 6 (A7, A10, A5, A13, A2) count the same person as ONE across all
  stairwell cameras and capture a zoomed-in face snapshot at peak face visibility/angle.
  If a camera never achieves a proper face score/angle, it skips taking the picture and
  delegates the pending snapshot to the next camera in the sequence.

Single camera:
    python rig/bin/demo_overlay.py --source FILE_OR_RTSP_URL [--path demo]

Multi camera (Cameras 1 to 6, synchronized 17:36 to 17:42):
    python rig/bin/demo_overlay.py --multi [--time-range 17:36-17:42] [--backend onnx-cuda-yolo]

Live multi camera (NVR 960x1088 mainstreams):
    python rig/bin/demo_overlay.py --live --no-individual --backend onnx-cuda-yolo
"""

from __future__ import annotations

import argparse
import contextlib
import multiprocessing as mp
import os
import struct
import subprocess
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import av
import numpy as np
import yaml

from argus.backends.face_align import align_face
from argus.backends.preprocessing import resize_bilinear
from argus.backends.registry import get_detector, get_face_detector, get_face_embedder
from argus.backends.types import Box, Detection, FaceDetection

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
        "code": "A12",
        "name": "Cam 1 (A12) - Street Entrance",
        "title": "CAM 1 (A12): STREET [LOCAL]",
        "file": "Camera 1 (14-19).mp4",
        "path": "cam1",
        "offset_1740": 11800,
        "is_cam12": True,
    },
    {
        "id": 2,
        "code": "A7",
        "name": "Cam 2 (A7) - Lower Stairs",
        "title": "CAM 2 (A7): LOWER STAIRS",
        "file": "Camera 2 (13-20).mp4",
        "path": "cam2",
        "offset_1740": 14227,
        "is_cam12": False,
    },
    {
        "id": 3,
        "code": "A10",
        "name": "Cam 3 (A10) - Mid Stairs",
        "title": "CAM 3 (A10): MID STAIRS",
        "file": "Camera 3 (11-20).mp4",
        "path": "cam3",
        "offset_1740": 23683,
        "is_cam12": False,
    },
    {
        "id": 4,
        "code": "A5",
        "name": "Cam 4 (A5) - 2nd Fl Landing",
        "title": "CAM 4 (A5): 2ND FL LANDING",
        "file": "Camera 4 (14-20).mp4",
        "path": "cam4",
        "offset_1740": 13144,
        "is_cam12": False,
    },
    {
        "id": 5,
        "code": "A13",
        "name": "Cam 5 (A13) - Upper Stairs",
        "title": "CAM 5 (A13): UPPER STAIRS",
        "file": "Camera 5 (10-20).mp4",
        "path": "cam5",
        "offset_1740": 26775,
        "is_cam12": False,
    },
    {
        "id": 6,
        "code": "A2",
        "name": "Cam 6 (A2) - 3rd Fl Landing",
        "title": "CAM 6 (A2): 3RD FL LANDING",
        "file": "Camera 6 (14-20).mp4",
        "path": "cam6",
        "offset_1740": 12879,
        "is_cam12": False,
    },
]

# Live NVR camera sequence: A12 -> A7 -> A10 -> A5 -> A13 -> A2
# Uses full-resolution 960x1088 H.264 mainstreams (1201, 701, 1001, 501, 1301, 201)
# for high visual quality, accurate person detection, and clear face snapshots.
LIVE_CAMERA_CONFIGS = [
    {
        "id": 1,
        "code": "A12",
        "name": "Cam 1 (A12) - Street Entrance",
        "title": "CAM 1 (A12): STREET [LOCAL]",
        "channel": 1201,
        "fallback_channel": 1202,
        "path": "cam1",
        "is_cam12": True,
    },
    {
        "id": 2,
        "code": "A7",
        "name": "Cam 2 (A7) - Lower Stairs",
        "title": "CAM 2 (A7): LOWER STAIRS",
        "channel": 701,
        "fallback_channel": 702,
        "path": "cam2",
        "is_cam12": False,
    },
    {
        "id": 3,
        "code": "A10",
        "name": "Cam 3 (A10) - Mid Stairs",
        "title": "CAM 3 (A10): MID STAIRS",
        "channel": 1001,
        "fallback_channel": 1002,
        "path": "cam3",
        "is_cam12": False,
    },
    {
        "id": 4,
        "code": "A5",
        "name": "Cam 4 (A5) - 2nd Fl Landing",
        "title": "CAM 4 (A5): 2ND FL LANDING",
        "channel": 501,
        "fallback_channel": 502,
        "path": "cam4",
        "is_cam12": False,
    },
    {
        "id": 5,
        "code": "A13",
        "name": "Cam 5 (A13) - Upper Stairs",
        "title": "CAM 5 (A13): UPPER STAIRS",
        "channel": 1301,
        "fallback_channel": 1302,
        "path": "cam5",
        "is_cam12": False,
    },
    {
        "id": 6,
        "code": "A2",
        "name": "Cam 6 (A2) - 3rd Fl Landing",
        "title": "CAM 6 (A2): 3RD FL LANDING",
        "channel": 201,
        "fallback_channel": 202,
        "path": "cam6",
        "is_cam12": False,
    },
]


def get_nvr_credentials(secrets_path: Path | None = None) -> tuple[str, str, str]:
    """Get NVR host, user, password from env or config/secrets.env."""
    user = os.environ.get("CAMERA_USER")
    password = os.environ.get("CAMERA_PASSWORD")
    host = os.environ.get("CAMERA_HOST", "192.168.1.78")

    sec = secrets_path or Path("config/secrets.env")
    if (not user or not password) and sec.exists():
        for line in sec.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k == "CAMERA_USER" and not user:
                    user = v
                elif k == "CAMERA_PASSWORD" and not password:
                    password = v
                elif k == "CAMERA_HOST" and host == "192.168.1.78":
                    host = v

    return host or "192.168.1.78", user or "sparrow", password or "Sparrow123"


def write_png(path: Path, img: np.ndarray) -> None:
    """Write an HxWx3 uint8 RGB array to a PNG file using pure stdlib zlib/struct."""
    path.parent.mkdir(parents=True, exist_ok=True)
    h, w, _ = img.shape
    raw = b"".join(b"\x00" + img[y].tobytes() for y in range(h))

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", crc)

    ihdr = struct.pack("!IIBBBBB", w, h, 8, 2, 0, 0, 0)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 6))
        + _chunk(b"IEND", b"")
    )
    tmp = path.with_suffix(".tmp.png")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


# --- drawing ------------------------------------------------------------------

_GLYPHS = {
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111",
    "3": "111001111001111", "4": "101101111001001", "5": "111100111001111",
    "6": "111100111101111", "7": "111001010010010", "8": "111101111101111",
    "9": "111101111001111", "A": "010101111101101", "B": "110101110101110",
    "C": "011100100100011", "D": "110101101101110", "E": "111100110100111",
    "F": "111100110100100", "G": "011100101101011", "H": "101101111101101",
    "I": "111010010010111", "J": "001001001101010", "K": "101101110101101",
    "L": "100100100100111", "M": "101111111101101", "N": "101111111111101",
    "O": "010101101101010", "P": "110101110100100", "Q": "010101101111011",
    "R": "110101110101101", "S": "011100010001110", "T": "111010010010010",
    "U": "101101101101111", "V": "101101101101010", "W": "101101111111101",
    "X": "101101010101101", "Y": "101101010010010", "Z": "111001010100111",
    ":": "000010000010000", ".": "000000000000010", "-": "000000111000000",
    "#": "101111101111101", ">": "100010001010100", "/": "001001010100100",
    " ": "000000000000000", "[": "110100100100110", "]": "011001001001011",
}  # fmt: skip


def draw_label(
    img: np.ndarray,
    x: int,
    y: int,
    text: str,
    colour: Any,
    scale: int = 3,
    text_colour: int = 0,
) -> None:
    """A filled tag in the track colour with bitmap characters, bottom-left at (x, y)."""
    text = text.upper()
    h, w = img.shape[:2]
    tw, th = len(text) * 4 * scale + scale, 7 * scale
    if tw >= w or th >= h:
        return
    x, y = max(0, min(x, w - tw)), max(th, min(y, h))
    img[y - th : y, x : x + tw] = colour
    for i, ch in enumerate(text):
        glyph_str = _GLYPHS.get(ch, _GLYPHS[" "])
        glyph = np.array([int(b) for b in glyph_str], dtype=bool).reshape(5, 3)
        big = np.kron(glyph, np.ones((scale, scale), dtype=bool))
        gx, gy = x + scale + i * 4 * scale, y - th + scale
        img[gy : gy + 5 * scale, gx : gx + 3 * scale][big] = text_colour


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


def draw_pip_snapshot(
    tile: np.ndarray,
    crop_rgb: np.ndarray,
    label_top: str,
    label_bottom: str,
    border_colour: tuple[int, int, int] = (60, 255, 90),
) -> None:
    """Render a zoomed-in face snapshot Picture-in-Picture inset in the top-right of a tile."""
    th, tw = tile.shape[:2]
    pip_size = 96
    if crop_rgb.shape[0] != pip_size or crop_rgb.shape[1] != pip_size:
        pip_img = resize_bilinear(crop_rgb, pip_size, pip_size).clip(0, 255).astype(np.uint8)
    else:
        pip_img = crop_rgb

    pad = 8
    b = 3
    x2 = tw - pad
    x1 = x2 - (pip_size + 2 * b)
    y1 = pad
    y2 = y1 + (pip_size + 2 * b)
    if x1 < 0 or y2 >= th:
        return

    tile[y1:y2, x1:x2] = border_colour
    tile[y1 + b : y2 - b, x1 + b : x2 - b] = pip_img
    draw_label(tile, x1, y1 + 14, label_top, border_colour, scale=2, text_colour=0)
    if label_bottom:
        draw_label(tile, x1, y2 + 14, label_bottom, (20, 20, 20), scale=2, text_colour=255)


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


# --- detection filtering & appearance features -------------------------------


def is_valid_person_detection(det: Detection, frame_w: int, frame_h: int) -> bool:
    """Filter out faulty/hallucinated bounding boxes (e.g. Cam 10 full-frame wall artifacts)."""
    if det.label != "person":
        return False
    bw = float(det.box.x2 - det.box.x1)
    bh = float(det.box.y2 - det.box.y1)
    if bw <= 1.0 or bh <= 1.0 or frame_w <= 0 or frame_h <= 0:
        return False

    area_ratio = (bw * bh) / float(frame_w * frame_h)
    w_ratio = bw / float(frame_w)
    h_ratio = bh / float(frame_h)
    aspect = bh / bw

    # Reject full-frame or wall-spanning false positives (Cam 10 substream had area_ratio=0.938)
    if area_ratio > 0.45 or w_ratio > 0.65 or h_ratio > 0.90:
        return False
    # Reject tiny noise detections
    if h_ratio < 0.08 or w_ratio < 0.035:
        return False
    # Reject non-human aspect ratios (flat horizontal railings or extreme vertical slivers)
    return not (aspect < 1.15 or aspect > 5.5)


def extract_appearance(img: np.ndarray, box: Box | np.ndarray) -> np.ndarray:
    """Compute normalized 3-part spatial HSV + Luma histogram for cross-camera person matching."""
    h, w = img.shape[:2]
    if isinstance(box, Box):
        x1, y1, x2, y2 = box.x1, box.y1, box.x2, box.y2
    else:
        x1, y1, x2, y2 = box
    x1, x2 = max(0, int(x1)), min(w, int(x2))
    y1, y2 = max(0, int(y1)), min(h, int(y2))
    if x2 - x1 < 4 or y2 - y1 < 6:
        return np.zeros(48, dtype=np.float32)

    # Crop central 70% width to minimize background wall bleed
    cw = x2 - x1
    margin_x = int(cw * 0.15)
    crop = (
        img[y1:y2, x1 + margin_x : max(x1 + margin_x + 2, x2 - margin_x)].astype(np.float32) / 255.0
    )
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
    p1 = max(1, int(ch * 0.35))
    p2 = max(p1 + 1, int(ch * 0.70))
    parts = [
        (h_chan[:p1], s_chan[:p1], v_chan[:p1]),
        (h_chan[p1:p2], s_chan[p1:p2], v_chan[p1:p2]),
        (h_chan[p2:], s_chan[p2:], v_chan[p2:]),
    ]

    feat: list[float] = []
    for hp, sp, vp in parts:
        if hp.size == 0:
            feat.extend([0.0] * 16)
            continue
        h_hist, _ = np.histogram(hp, bins=8, range=(0, 1))
        s_hist, _ = np.histogram(sp, bins=4, range=(0, 1))
        v_hist, _ = np.histogram(vp, bins=4, range=(0, 1))
        hist = np.concatenate([h_hist, s_hist, v_hist]).astype(np.float32)
        norm = float(np.linalg.norm(hist)) + 1e-6
        feat.extend((hist / norm).tolist())

    f = np.array(feat, dtype=np.float32)
    return f / (float(np.linalg.norm(f)) + 1e-6)


# --- face visibility scoring & zoomed snapshot extraction --------------------


def _laplacian_sharpness(crop: np.ndarray) -> float:
    """Variance of a 3x3 Laplacian in pure numpy."""
    if crop.size < 9 or crop.shape[0] < 3 or crop.shape[1] < 3:
        return 0.0
    gray = crop.mean(axis=2).astype(np.float64)
    centre = gray[1:-1, 1:-1]
    lap = gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:] - 4.0 * centre
    return float(lap.var())


@dataclass(frozen=True)
class FaceVisibilityResult:
    score: float
    passes_gate: bool
    det_score: float
    angle_score: float
    sharpness: float
    box_px: float
    yaw_asymmetry: float
    zoom_crop: np.ndarray | None
    aligned_crop: np.ndarray | None


def extract_zoomed_face(img: np.ndarray, face_box: Box, out_size: int = 160) -> np.ndarray:
    """Extract a zoomed-in square portrait around a detected face with head/chin context."""
    h, w = img.shape[:2]
    fw = max(1.0, face_box.x2 - face_box.x1)
    fh = max(1.0, face_box.y2 - face_box.y1)
    cx = (face_box.x1 + face_box.x2) / 2.0
    cy = (face_box.y1 + face_box.y2) / 2.0
    half = max(fw, fh) * 0.90

    x1 = max(0, round(cx - half))
    y1 = max(0, round(cy - half))
    x2 = min(w, round(cx + half))
    y2 = min(h, round(cy + half))
    if x2 <= x1 + 4 or y2 <= y1 + 4:
        return np.zeros((out_size, out_size, 3), dtype=np.uint8)
    raw_crop = img[y1:y2, x1:x2]
    resized = resize_bilinear(raw_crop, out_size, out_size)
    lo = float(np.percentile(resized, 2))
    hi = float(np.percentile(resized, 99))
    if 15.0 < (hi - lo) < 150.0:
        resized = (resized - lo) * (235.0 / (hi - lo)) + 10.0
    return resized.clip(0, 255).astype(np.uint8)


def evaluate_face_visibility(
    face: FaceDetection,
    frame: np.ndarray,
    *,
    min_det_score: float = 0.24,
    min_box_px: float = 14.0,
    max_yaw_asymmetry: float = 0.48,
    min_sharpness: float = 3.5,
    min_visibility_score: float = 0.16,
) -> FaceVisibilityResult:
    """Score a face detection for frontal visibility, sharpness, and resolution.

    If a camera only sees the back of a person's head or a steep/blurry profile,
    `passes_gate` is False so the camera skips taking the snapshot and delegates
    to the next camera in the stairwell sequence.
    """
    h, w = frame.shape[:2]
    x1 = max(0, int(face.box.x1))
    y1 = max(0, int(face.box.y1))
    x2 = min(w, int(face.box.x2) + 1)
    y2 = min(h, int(face.box.y2) + 1)
    fw = float(face.box.x2 - face.box.x1)
    fh = float(face.box.y2 - face.box.y1)
    box_px = min(fw, fh)

    if x2 <= x1 + 2 or y2 <= y1 + 2:
        return FaceVisibilityResult(0.0, False, face.score, 0.0, 0.0, box_px, 1.0, None, None)

    tight_crop = frame[y1:y2, x1:x2]
    sharpness = _laplacian_sharpness(tight_crop)

    kps = face.landmarks
    left_eye, right_eye, nose, left_mouth, right_mouth = kps[0], kps[1], kps[2], kps[3], kps[4]
    inter_ocular = float(np.linalg.norm(right_eye - left_eye))
    eye_mid_x = float((left_eye[0] + right_eye[0]) / 2.0)
    yaw_asym = (
        float(abs(nose[0] - eye_mid_x) / max(inter_ocular, 1e-3)) if inter_ocular > 0 else 1.0
    )
    eye_tilt = (
        float(abs(right_eye[1] - left_eye[1]) / max(inter_ocular, 1e-3))
        if inter_ocular > 0
        else 1.0
    )

    # Anatomical landmark order: eyes above nose, nose above mouth, left eye left of right
    eyes_top = max(float(left_eye[1]), float(right_eye[1]))
    mouth_top = min(float(left_mouth[1]), float(right_mouth[1]))
    anatomical_ok = (
        inter_ocular >= 6.5
        and float(left_eye[0]) < float(right_eye[0])
        and eyes_top < float(nose[1]) < mouth_top
    )

    if not anatomical_ok:
        angle_score = 0.0
    else:
        yaw_factor = max(0.0, 1.0 - 1.35 * yaw_asym)
        tilt_factor = max(0.0, 1.0 - 0.75 * eye_tilt)
        angle_score = round(float(yaw_factor * tilt_factor), 4)

    sharp_norm = min(1.0, sharpness / 22.0)
    size_norm = min(1.0, box_px / 36.0)
    vis_score = round(
        float(face.score * angle_score * (0.45 + 0.55 * sharp_norm) * (0.45 + 0.55 * size_norm)),
        4,
    )

    passes = bool(
        anatomical_ok
        and face.score >= min_det_score
        and box_px >= min_box_px
        and yaw_asym <= max_yaw_asymmetry
        and sharpness >= min_sharpness
        and vis_score >= min_visibility_score
    )

    zoom_crop = None
    aligned_crop = None
    if passes:
        zoom_crop = extract_zoomed_face(frame, face.box, out_size=160)
        with contextlib.suppress(Exception):
            aligned_crop = align_face(frame, face.landmarks)

    return FaceVisibilityResult(
        score=vis_score,
        passes_gate=passes,
        det_score=face.score,
        angle_score=angle_score,
        sharpness=round(sharpness, 2),
        box_px=round(box_px, 1),
        yaw_asymmetry=round(yaw_asym, 4),
        zoom_crop=zoom_crop,
        aligned_crop=aligned_crop,
    )


def find_best_face_for_person(
    person_box: np.ndarray,
    faces: list[FaceDetection],
    frame: np.ndarray,
    face_detector: Any | None = None,
) -> FaceVisibilityResult:
    """Match detected faces to the upper body of a person box and evaluate visibility."""
    px1, py1, px2, py2 = (float(v) for v in person_box)
    pw = max(1.0, px2 - px1)
    ph = max(1.0, py2 - py1)

    def _is_head_box(fc: FaceDetection) -> bool:
        fw = fc.box.x2 - fc.box.x1
        fh = fc.box.y2 - fc.box.y1
        fcx = (fc.box.x1 + fc.box.x2) / 2.0
        fcy = (fc.box.y1 + fc.box.y2) / 2.0
        return (
            fw <= 0.78 * pw
            and fh <= 0.45 * ph
            and (px1 - 0.12 * pw <= fcx <= px2 + 0.12 * pw)
            and (py1 - 0.12 * ph <= fcy <= py1 + 0.42 * ph)
        )

    candidates: list[FaceDetection] = [fc for fc in faces if _is_head_box(fc)]

    # If full-frame missed a small face on distant stairs, probe an upper-body sub-window
    if not candidates and face_detector is not None and ph >= 90.0 and pw >= 36.0:
        h, w = frame.shape[:2]
        cx = int((px1 + px2) / 2.0)
        cy = int(py1 + ph * 0.25)
        win_half = max(160, int(max(pw, ph * 0.55)))
        wx1, wy1 = max(0, cx - win_half), max(0, cy - win_half)
        wx2, wy2 = min(w, cx + win_half), min(h, cy + win_half)
        if wx2 - wx1 >= 96 and wy2 - wy1 >= 96:
            sub = frame[wy1:wy2, wx1:wx2]
            lo = float(np.percentile(sub, 3))
            hi = float(np.percentile(sub, 98))
            sub_enh = (
                np.clip((sub.astype(np.float32) - lo) * (240.0 / (hi - lo)) + 8.0, 0, 255).astype(
                    np.uint8
                )
                if 15.0 < (hi - lo) < 145.0
                else sub
            )
            for sfc in face_detector.detect_faces(sub_enh, score_threshold=0.22):
                shifted = FaceDetection(
                    box=Box(
                        sfc.box.x1 + wx1,
                        sfc.box.y1 + wy1,
                        sfc.box.x2 + wx1,
                        sfc.box.y2 + wy1,
                    ),
                    score=sfc.score,
                    landmarks=sfc.landmarks + np.array([wx1, wy1], dtype=np.float32),
                )
                if _is_head_box(shifted):
                    candidates.append(shifted)

    best_res = FaceVisibilityResult(0.0, False, 0.0, 0.0, 0.0, 0.0, 1.0, None, None)
    for fc in candidates:
        res = evaluate_face_visibility(fc, frame)
        if (res.passes_gate and not best_res.passes_gate) or (
            res.passes_gate == best_res.passes_gate and res.score > best_res.score
        ):
            best_res = res
    return best_res


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
    initial_box: np.ndarray = field(default_factory=lambda: np.zeros(4))
    velocity: np.ndarray = field(default_factory=lambda: np.zeros(4))
    hits: int = 1
    max_score: float = 0.0
    last_seen: float = 0.0
    last_update: float = 0.0
    feature: np.ndarray | None = None
    best_face_score: float = 0.0
    face_captured_locally: bool = False
    max_raw_face_score: float = 0.0

    def predicted(self, now: float) -> np.ndarray:
        dt = min(now - self.last_update, 0.5)
        return self.box + self.velocity * dt * (1.0 - dt)

    def displacement(self) -> float:
        c_now = (self.box[:2] + self.box[2:]) / 2.0
        c_init = (self.initial_box[:2] + self.initial_box[2:]) / 2.0
        return float(np.hypot(c_now[0] - c_init[0], c_now[1] - c_init[1]))

    def correct(
        self, box: np.ndarray, score: float, now: float, feat: np.ndarray | None = None
    ) -> None:
        dt = max(now - self.last_update, 1e-3)
        prior = self.predicted(now)
        centre = ((box[:2] + box[2:]) - (self.box[:2] + self.box[2:])) / 2 / dt
        self.velocity = 0.7 * self.velocity + 0.3 * np.tile(centre, 2)
        speed_cap = 1.5 * max(box[3] - box[1], 1.0)
        self.velocity = np.clip(self.velocity, -speed_cap, speed_cap)
        self.box = 0.75 * box + 0.25 * prior
        self.hits += 1
        self.max_score = max(self.max_score, score)
        self.last_seen = self.last_update = now
        if feat is not None:
            if self.feature is None:
                self.feature = feat
            else:
                self.feature = 0.8 * self.feature + 0.2 * feat
                self.feature /= float(np.linalg.norm(self.feature)) + 1e-6


class DemoTracker:
    """ByteTrack-style single-camera tracker with static false-positive suppression."""

    def __init__(self, high: float = 0.45, lost_s: float = 1.8, confirm_hits: int = 3) -> None:
        self.high = high
        self.lost_s = lost_s
        self.confirm_hits = confirm_hits
        self.tracks: list[DemoTrack] = []
        self._next_id = 1

    def _match(
        self,
        tracks: list[DemoTrack],
        dets: list[tuple[np.ndarray, float]],
        now: float,
        gate: float,
    ) -> tuple[
        list[tuple[DemoTrack, np.ndarray, float]],
        list[DemoTrack],
        list[tuple[np.ndarray, float]],
    ]:
        pairs = []
        for ti, t in enumerate(tracks):
            p = t.predicted(now)
            ph = max(p[3] - p[1], 1.0)
            for di, (d, _sc) in enumerate(dets):
                iou = _iou(p, d)
                dist = np.hypot(*((p[:2] + p[2:]) / 2 - (d[:2] + d[2:]) / 2)) / ph
                if iou >= gate or dist < 0.65:
                    pairs.append((-iou, dist, ti, di))
        pairs.sort()
        used_t, used_d, matched = set(), set(), []
        for _, _, ti, di in pairs:
            if ti not in used_t and di not in used_d:
                used_t.add(ti)
                used_d.add(di)
                matched.append((tracks[ti], dets[di][0], dets[di][1]))
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
    ) -> list[DemoTrack]:
        """Update tracks and return any confirmed tracks that just expired this step."""
        if img is not None:
            fh, fw = img.shape[:2]
            dets = [d for d in dets if is_valid_person_detection(d, fw, fh)]

        def arr(ds: list[Detection]) -> list[tuple[np.ndarray, float]]:
            return [(np.array([d.box.x1, d.box.y1, d.box.x2, d.box.y2]), d.score) for d in ds]

        high = arr([d for d in dets if d.score >= self.high])
        low = arr([d for d in dets if d.score < self.high])
        matched, rest, new = self._match(self.tracks, high, now, gate=0.2)
        matched2, _, _ = self._match(rest, low, now, gate=0.3)

        for t, box, score in matched + matched2:
            feat = extract_appearance(img, box) if img is not None else None
            t.correct(box, score, now, feat)

        for box, score in new:
            feat = extract_appearance(img, box) if img is not None else None
            t = DemoTrack(
                0,
                box.copy(),
                initial_box=box.copy(),
                max_score=score,
                last_seen=now,
                last_update=now,
                feature=feat,
            )
            self.tracks.append(t)

        # Only tracks matched on THIS frame block their global ID in this camera;
        # a coasting track can be reclaimed if the same person re-appears after a brief occlusion.
        active_ids = {t.track_id for t in self.tracks if t.track_id > 0 and t.last_seen == now}
        reclaimed_ids: set[int] = set()
        for t in self.tracks:
            if t.track_id == 0 and t.hits >= self.confirm_hits:
                # Require either strong confidence or real movement to reject static wall ghosts
                if t.max_score < 0.58 and t.displacement() < 10.0:
                    continue
                if assign_fn is not None and t.feature is not None:
                    gid = assign_fn(t.feature, now, active_ids)
                    t.track_id = gid
                    active_ids.add(gid)
                    reclaimed_ids.add(gid)
                else:
                    t.track_id = self._next_id
                    self._next_id += 1

        if reclaimed_ids:
            self.tracks = [
                t for t in self.tracks if not (t.track_id in reclaimed_ids and t.last_seen < now)
            ]

        alive: list[DemoTrack] = []
        expired_confirmed: list[DemoTrack] = []
        for t in self.tracks:
            timeout = self.lost_s if t.track_id > 0 else 0.35
            if now - t.last_seen <= timeout:
                alive.append(t)
            elif t.track_id > 0:
                expired_confirmed.append(t)
        self.tracks = alive
        return expired_confirmed

    def visible(self, now: float) -> list[tuple[DemoTrack, np.ndarray, bool]]:
        return [
            (t, t.predicted(now), now - t.last_seen > 0.25)
            for t in self.tracks
            if t.track_id > 0 and now - t.last_seen <= 0.85
        ]


# --- multi-camera stairwell global counting & face snapshot coordinator ------


@dataclass
class GlobalPerson:
    global_id: int
    first_seen: float
    last_seen: float
    last_cam_id: int
    last_camera: str
    app_feature: np.ndarray
    face_embedding: np.ndarray | None = None
    history_cameras: list[int] = field(default_factory=list)
    best_face_score: float = 0.0
    best_face_crop: np.ndarray | None = None
    best_face_cam_id: int | None = None
    best_face_cam_name: str | None = None
    snapshot_path: str | None = None
    skipped_cameras: list[int] = field(default_factory=list)


class StairwellGlobalTracker:
    """Unifies person counting and face snapshot handoff across C2..C6 (excluding Cam 12).

    - Cam 12 (cam_id == 1) is excluded and never calls this tracker.
    - Across C2..C6 (Cam 7, Cam 10, Cam 5, Cam 13, Cam 2), the same person is counted as ONE.
    - Tracks the best zoomed-in face snapshot per person via visibility score. If a camera
      never achieves a proper score/angle while the person is in view, it does not take a
      picture and delegates the pending snapshot to the next camera.
    """

    def __init__(
        self,
        sim_threshold: float = 0.50,
        max_gap_s: float = 300.0,
        snapshot_dir: Path | None = None,
        face_embedder: Any | None = None,
    ) -> None:
        self.sim_threshold = sim_threshold
        self.max_gap_s = max_gap_s
        self.snapshot_dir = snapshot_dir or Path("var/snapshots")
        self.face_embedder = face_embedder
        self.persons: list[GlobalPerson] = []
        self._next_id = 1

    def match_or_create(
        self,
        cam_id: int,
        cam_name: str,
        app_feat: np.ndarray,
        now: float,
        active_gids_in_cam: set[int] | None = None,
    ) -> tuple[int, bool]:
        if cam_id == 1:
            raise ValueError("Cam 12 (cam_id=1) is excluded from StairwellGlobalTracker")
        active_set = active_gids_in_cam or set()
        best_score = -1.0
        best_p: GlobalPerson | None = None

        recent_candidates = [
            p
            for p in self.persons
            if p.global_id not in active_set and (now - p.last_seen) <= self.max_gap_s
        ]

        for p in recent_candidates:
            gap = max(0.0, now - p.last_seen)
            app_sim = float(np.dot(app_feat, p.app_feature))
            cam_dist = abs(cam_id - p.last_cam_id)

            # Stairwell topology bonus: A7(2) <-> A10(3) <-> A5(4) <-> A13(5) <-> A2(6)
            topo_bonus = 0.0
            if cam_dist == 0 and gap <= 15.0:
                topo_bonus = 0.16
            elif cam_dist == 1 and gap <= 75.0:
                topo_bonus = 0.15
            elif cam_dist == 2 and gap <= 120.0:
                topo_bonus = 0.08

            # Single-person stairwell continuity boost
            if len(recent_candidates) == 1 and gap <= 60.0 and cam_dist <= 2 and app_sim >= 0.42:
                topo_bonus += 0.12

            score = app_sim + topo_bonus
            if score > best_score:
                best_score = score
                best_p = p

        if best_p is not None and best_score >= self.sim_threshold:
            prev_cam = best_p.last_cam_id
            if (
                prev_cam != cam_id
                and best_p.best_face_cam_id != prev_cam
                and prev_cam not in best_p.skipped_cameras
                and best_p.best_face_crop is None
            ):
                best_p.skipped_cameras.append(prev_cam)
            best_p.last_seen = now
            best_p.last_cam_id = cam_id
            best_p.last_camera = cam_name
            best_p.app_feature = 0.75 * best_p.app_feature + 0.25 * app_feat
            best_p.app_feature /= float(np.linalg.norm(best_p.app_feature)) + 1e-6
            if cam_id not in best_p.history_cameras:
                best_p.history_cameras.append(cam_id)
            return best_p.global_id, False

        gid = self._next_id
        self._next_id += 1
        gp = GlobalPerson(
            global_id=gid,
            first_seen=now,
            last_seen=now,
            last_cam_id=cam_id,
            last_camera=cam_name,
            app_feature=app_feat.copy(),
            history_cameras=[cam_id],
        )
        self.persons.append(gp)
        return gid, True

    def get_person(self, global_id: int) -> GlobalPerson | None:
        for p in self.persons:
            if p.global_id == global_id:
                return p
        return None

    def update_face_candidate(
        self,
        cam_id: int,
        cam_name: str,
        global_id: int,
        vis_score: float,
        zoom_crop: np.ndarray,
        aligned_crop: np.ndarray | None,
        app_feat: np.ndarray | None,
        now: float,
    ) -> tuple[int, bool]:
        """Record a valid face snapshot if its visibility score is the best seen for this person.

        Also uses ArcFace face embeddings (when available) to merge any split track back into
        the original GlobalPerson so the same person is strictly counted as ONE.
        """
        if cam_id == 1:
            return global_id, False

        person = self.get_person(global_id)
        if person is None:
            return global_id, False

        person.last_seen = now
        person.last_cam_id = cam_id
        person.last_camera = cam_name
        if app_feat is not None:
            person.app_feature = 0.8 * person.app_feature + 0.2 * app_feat
            person.app_feature /= float(np.linalg.norm(person.app_feature)) + 1e-6

        emb: np.ndarray | None = None
        if aligned_crop is not None and self.face_embedder is not None:
            with contextlib.suppress(Exception):
                emb = self.face_embedder.embed(aligned_crop)

        # Check if this face embedding matches an earlier GlobalPerson (de-duplicating count!)
        if emb is not None:
            for other in self.persons:
                if other.global_id == person.global_id or other.face_embedding is None:
                    continue
                sim = float(np.dot(emb, other.face_embedding))
                if sim >= 0.50 and other.global_id < person.global_id:
                    # Merge `person` into `other`
                    other.last_seen = max(other.last_seen, person.last_seen)
                    other.last_cam_id = cam_id
                    other.last_camera = cam_name
                    for hc in person.history_cameras:
                        if hc not in other.history_cameras:
                            other.history_cameras.append(hc)
                    for sc in person.skipped_cameras:
                        if sc not in other.skipped_cameras:
                            other.skipped_cameras.append(sc)
                    self.persons = [p for p in self.persons if p.global_id != person.global_id]
                    person = other
                    break

            if person.face_embedding is None:
                person.face_embedding = emb
            else:
                person.face_embedding = 0.7 * person.face_embedding + 0.3 * emb
                person.face_embedding /= float(np.linalg.norm(person.face_embedding)) + 1e-6

        # Remove cam_id from skipped_cameras if it now achieved a valid face score
        if cam_id in person.skipped_cameras:
            person.skipped_cameras.remove(cam_id)

        updated_best = False
        if vis_score > person.best_face_score:
            person.best_face_score = vis_score
            person.best_face_crop = zoom_crop.copy()
            person.best_face_cam_id = cam_id
            person.best_face_cam_name = cam_name
            score_int = round(vis_score * 1000)
            snap_file = (
                self.snapshot_dir
                / f"person_{person.global_id:03d}_cam{cam_id}_score_{score_int:03d}.png"
            )
            best_file = self.snapshot_dir / f"person_{person.global_id:03d}_best.png"
            with contextlib.suppress(Exception):
                write_png(snap_file, zoom_crop)
                write_png(best_file, zoom_crop)
            person.snapshot_path = str(best_file)
            updated_best = True

        return person.global_id, updated_best

    def mark_camera_no_angle_handoff(self, cam_id: int, global_id: int, now: float) -> bool:
        """Record when a track finishes on `cam_id` without getting a proper face score."""
        if cam_id == 1:
            return False
        person = self.get_person(global_id)
        if person is None:
            return False
        person.last_seen = max(person.last_seen, now)
        if person.best_face_cam_id != cam_id and cam_id not in person.skipped_cameras:
            person.skipped_cameras.append(cam_id)
            return True
        return False

    @property
    def headcount(self) -> int:
        return len(self.persons)

    @property
    def captured_count(self) -> int:
        return sum(1 for p in self.persons if p.best_face_crop is not None)


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
    """ffmpeg reads raw RGB on stdin, burns in text, pushes high-bitrate H.264 over RTSP."""
    enc = encoder or get_h264_encoder()
    enc_opts = (
        [
            "-c:v", "h264_nvenc",
            "-preset", "p2",
            "-tune", "ull",
            "-cq", "20",
            "-b:v", "8M",
            "-maxrate", "10M",
            "-bufsize", "10M",
        ]
        if enc == "h264_nvenc"
        else [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-crf", "20",
            "-maxrate", "8M",
            "-bufsize", "8M",
        ]
    )  # fmt: skip
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
            f"drawtext=fontfile={FONT}:textfile={hud}:reload=1:fontsize=19:fontcolor=white"
            ":box=1:boxcolor=black@0.72:boxborderw=8:x=12:y=h-th-16:line_spacing=5"
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
    """Parse a timeline clock as HH:MM[:SS] or seconds from midnight."""
    parts = [float(p) for p in t_str.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 3600 + parts[1] * 60
    return float(parts[0])


def is_green_decode_artifact(img: np.ndarray) -> bool:
    """Reject the near-solid green frames observed from corrupt NVR H.264."""
    sample = img[::16, ::16]
    red, green, blue = (sample[:, :, channel] for channel in range(3))
    return bool(red.max() < 8 and blue.max() < 8 and 50 < green.mean() < 120 and green.std() < 12)


def _downsample_to_tile(img: np.ndarray, tile_w: int = 480, tile_h: int = 544) -> np.ndarray:
    """Downsample camera frame to 480x544 tile using anti-aliased 2x2 box filter when 960x1088."""
    h, w = img.shape[:2]
    if h == tile_h * 2 and w == tile_w * 2:
        return (
            (
                img[0::2, 0::2].astype(np.uint16)
                + img[1::2, 0::2].astype(np.uint16)
                + img[0::2, 1::2].astype(np.uint16)
                + img[1::2, 1::2].astype(np.uint16)
            )
            >> 2
        ).astype(np.uint8)
    if h == tile_h and w == tile_w:
        return img.copy()
    return resize_bilinear(img, tile_w, tile_h).clip(0, 255).astype(np.uint8)


def check_live_streams() -> bool:
    """Verify all six NVR high-res mainstreams before reserving the GPU."""
    av.logging.set_level(av.logging.FATAL)
    host, user, password = get_nvr_credentials()
    all_usable = True
    for cfg in LIVE_CAMERA_CONFIGS:
        channel = cfg["channel"]
        source = f"rtsp://{user}:{password}@{host}:554/Streaming/Channels/{channel}"
        valid, bad = 0, 0
        codec = "unavailable"
        res_str = "0x0"
        try:
            container = av.open(source, options={"rtsp_transport": "tcp"}, timeout=5.0)
            try:
                stream = container.streams.video[0]
                codec = stream.codec_context.name
                res_str = f"{stream.width}x{stream.height}"
                if codec in {"h264", "hevc"}:
                    deadline = time.monotonic() + 4.0
                    for packet in container.demux(stream):
                        if time.monotonic() >= deadline or valid >= 8:
                            break
                        try:
                            decoded = packet.decode()
                        except av.FFmpegError:
                            if valid:
                                bad += 1
                            continue
                        for frame in decoded:
                            if frame.is_corrupt or is_green_decode_artifact(
                                frame.to_ndarray(format="rgb24")
                            ):
                                if valid:
                                    bad += 1
                            else:
                                valid += 1
            finally:
                container.close()
        except Exception:
            bad += 1
        usable = valid >= 5 and bad <= valid
        all_usable &= usable
        reason = "" if usable else " (no clean video; check NVR channel)"
        print(
            f"C{cfg['id']} channel {channel} ({res_str}): codec={codec} valid={valid} bad={bad} "
            f"{'OK' if usable else 'UNUSABLE'}{reason}",
            flush=True,
        )
    return all_usable


# --- multi-camera worker & runner --------------------------------------------


def _camera_worker(
    cfg: dict,
    video_source: Path | str,
    start_s: float,
    backend: str,
    high: float,
    low: float,
    fps: int,
    analysis_fps: float,
    rtsp_base: str,
    publish_individual: bool,
    frame_q: mp.Queue,
    req_q: mp.Queue,
    resp_q: mp.Queue,
    event_q: mp.Queue,
    stop_event: Any,
    is_live: bool = False,
) -> None:
    """Dedicated worker process: decodes, detects, tracks, scores faces, and streams."""
    av.logging.set_level(av.logging.FATAL)
    detector = get_detector(backend)
    cam_id = int(cfg["id"])
    cam_name = str(cfg["name"])
    cam_code = str(cfg.get("code", f"C{cam_id}"))
    is_cam12 = bool(cfg.get("is_cam12", cam_id == 1))
    url = f"{rtsp_base.rstrip('/')}/{cfg['path']}"
    providers = getattr(detector, "providers_active", [])

    # Load SCRFD 5-landmark face detector on all cameras EXCEPT Cam 12
    face_detector = None
    if not is_cam12:
        face_backend = "onnx-cuda" if "cuda" in backend else "onnx-cpu"
        with contextlib.suppress(Exception):
            face_detector = get_face_detector(face_backend)

    print(
        f"Camera {cam_id} ({cam_code}): detector={backend} providers={providers} "
        f"mode={'LOCAL_ONLY (Cam 12 excluded)' if is_cam12 else 'GLOBAL_REID + FACE_SNAPSHOT'}",
        flush=True,
    )
    if backend == "onnx-cuda-yolo" and "CUDAExecutionProvider" not in providers:
        raise RuntimeError(f"Camera {cam_id}: CUDA provider is unavailable")

    proc = None
    tracker = DemoTracker(high=high)
    last_inference_at = float("-inf")
    inference_count = 0

    def assign_global_id(feat: np.ndarray, now: float, active_ids: set[int]) -> int:
        req_q.put((cam_id, cam_name, feat, now, active_ids))
        deadline = time.monotonic() + 1.5
        while not stop_event.is_set() and time.monotonic() < deadline:
            try:
                assigned_cam_id, gid = resp_q.get(timeout=0.05)
                if assigned_cam_id == cam_id:
                    return int(gid)
                resp_q.put((assigned_cam_id, gid))
            except Exception:
                continue
        return 1

    assign_fn = None if is_cam12 else assign_global_id

    def process_frame(img: np.ndarray, now: float) -> None:
        nonlocal last_inference_at, inference_count, proc
        clean_img = img.copy()

        if now - last_inference_at >= 1.0 / analysis_fps:
            fh, fw = clean_img.shape[:2]
            dets = [
                d
                for d in detector.detect(clean_img, score_threshold=low)
                if is_valid_person_detection(d, fw, fh)
            ]
            expired_tracks = tracker.update(dets, now, img=clean_img, assign_fn=assign_fn)
            last_inference_at = now
            inference_count += 1

            # For cameras other than Cam 12: evaluate face visibility & angle on active tracks
            if not is_cam12 and face_detector is not None:
                visible_now = tracker.visible(now)
                if visible_now:
                    faces = face_detector.detect_faces(clean_img, score_threshold=0.20)
                    for track, box, _coasting in visible_now:
                        f_res = find_best_face_for_person(
                            box, faces, clean_img, face_detector=face_detector
                        )
                        track.max_raw_face_score = max(track.max_raw_face_score, f_res.det_score)
                        if (
                            f_res.passes_gate
                            and f_res.zoom_crop is not None
                            and f_res.score > track.best_face_score
                        ):
                            track.best_face_score = f_res.score
                            track.face_captured_locally = True
                            with contextlib.suppress(Exception):
                                event_q.put_nowait(
                                    (
                                        "FACE_SNAPSHOT",
                                        cam_id,
                                        cam_name,
                                        track.track_id,
                                        f_res.score,
                                        f_res.zoom_crop,
                                        f_res.aligned_crop,
                                        track.feature,
                                        now,
                                    )
                                )
                        elif track.feature is not None:
                            with contextlib.suppress(Exception):
                                event_q.put_nowait(
                                    (
                                        "TRACK_HEARTBEAT",
                                        cam_id,
                                        cam_name,
                                        track.track_id,
                                        track.feature,
                                        now,
                                    )
                                )

                # Any track that left this camera without ever getting a proper face angle/score
                # triggers a delegation handoff to the next camera.
                for exp_t in expired_tracks:
                    if not exp_t.face_captured_locally:
                        with contextlib.suppress(Exception):
                            event_q.put_nowait(
                                (
                                    "FACE_HANDOFF",
                                    cam_id,
                                    cam_name,
                                    exp_t.track_id,
                                    exp_t.max_raw_face_score,
                                    now,
                                )
                            )

        drawn: list[tuple[int, np.ndarray]] = []
        for track, box, coasting in tracker.visible(now):
            colour = (180, 180, 180) if is_cam12 else PALETTE[track.track_id % len(PALETTE)]
            draw_box(img, Box(*box), colour, dashed=coasting)
            tag_prefix = "L" if is_cam12 else "P"
            tag = f"{tag_prefix}{track.track_id}"
            if not is_cam12 and track.best_face_score > 0:
                tag = f"{tag} S:{int(track.best_face_score * 100)}"
            draw_label(img, int(box[0]), int(box[1]) - 2, tag, colour, scale=3)
            drawn.append((track.track_id, box))

        thumb = _downsample_to_tile(img, 480, 544)
        title_bg = (70, 70, 70) if is_cam12 else (25, 60, 120)
        draw_label(
            thumb,
            6,
            20,
            f"C{cam_id} {cam_code}" + (" LOCAL" if is_cam12 else ""),
            title_bg,
            scale=2,
            text_colour=255,
        )

        with contextlib.suppress(Exception):
            frame_q.put_nowait((cam_id, thumb, [t[0] for t in drawn], inference_count))

        if proc is None and publish_individual:
            proc = publisher(img.shape[1], img.shape[0], fps, url)

        if proc is not None and proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.write(img.tobytes())

    if is_live:
        while not stop_event.is_set():
            try:
                container = av.open(
                    str(video_source), options={"rtsp_transport": "tcp"}, timeout=10.0
                )
                stream = container.streams.video[0]
                for packet in container.demux(stream):
                    if stop_event.is_set():
                        break
                    try:
                        frames_list = packet.decode()
                    except (av.InvalidDataError, av.FFmpegError):
                        continue
                    for frame in frames_list:
                        if stop_event.is_set():
                            break
                        if frame.is_corrupt:
                            continue
                        img = frame.to_ndarray(format="rgb24")
                        if is_green_decode_artifact(img):
                            continue
                        process_frame(img, time.monotonic())
                container.close()
            except Exception as exc:
                if stop_event.is_set():
                    break
                print(f"Camera {cam_id}: reconnecting after {type(exc).__name__}", flush=True)
                time.sleep(1.0)
    else:
        container = av.open(str(video_source))
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
            process_frame(img, time.monotonic())

        container.close()

    if proc is not None:
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()


def run_multi_camera(args: argparse.Namespace, is_live: bool = False) -> None:
    """Run synchronized multi-camera demo across Cameras 1 to 6 (file replay or live RTSP)."""
    footage_dir = Path(args.footage_dir).expanduser() if not is_live else None
    if not is_live:
        assert footage_dir is not None
        if not footage_dir.exists():
            raise FileNotFoundError(f"Footage directory not found: {footage_dir}")
        time_str = args.time_range or "17:36-17:42"
        start_t_str, end_t_str = time_str.split("-")
        start_sec = parse_time_sec(start_t_str)
        end_sec = parse_time_sec(end_t_str)
        duration_s = max(10.0, end_sec - start_sec)
        delta_s = start_sec - (17 * 3600 + 40 * 60)
        configs = CAMERA_CONFIGS
        print(
            f"=== Starting Argus Vision Multi-Camera Demo ===\n"
            f"  Timeline:  {start_t_str} -> {end_t_str} (duration: {duration_s:.0f}s)\n"
            f"  Footage:   {footage_dir}\n"
            f"  Backend:   {args.backend}\n"
            f"  Counting:  Unified across C2..C6 (Cam 12 excluded)\n"
            f"  Snapshots: var/snapshots/ (Best face visibility score with camera handoff)\n"
            f"  Grid:      http://localhost:8888/{args.path}/\n",
            flush=True,
        )
    else:
        host, user, password = get_nvr_credentials()
        if getattr(args, "nvr_host", None):
            host = args.nvr_host
        configs = LIVE_CAMERA_CONFIGS
        duration_s = float("inf")
        start_sec = 0.0
        delta_s = 0.0
        print(
            f"=== Starting Argus Vision LIVE Multi-Camera Demo ===\n"
            f"  NVR Host:  {host} (960x1088 Mainstreams: 1201, 701, 1001, 501, 1301, 201)\n"
            f"  Sequence:  A7 -> A10 -> A5 -> A13 -> A2 (Cam 12 local only)\n"
            f"  Backend:   {args.backend}\n"
            f"  Snapshots: var/snapshots/ (Best face visibility score with camera handoff)\n"
            f"  Grid:      http://localhost:8888/{args.path}/\n",
            flush=True,
        )

    global_tracker = StairwellGlobalTracker(
        sim_threshold=0.50,
        snapshot_dir=Path("var/snapshots"),
        face_embedder=None,
    )

    hud = Path(tempfile.gettempdir()) / f"argus_demo_{args.path}.txt"
    write_hud(hud, "ARGUS MULTI-CAMERA  initializing...")

    grid_url = f"{args.rtsp.rstrip('/')}/{args.path}"
    rec_path = Path(args.record).expanduser() if args.record else None
    grid_proc = publisher(1440, 1088, args.fps, grid_url, hud=hud, record_file=rec_path)

    frame_q: mp.Queue = mp.Queue(maxsize=36)
    req_q: mp.Queue = mp.Queue()
    resp_q: mp.Queue = mp.Queue()
    event_q: mp.Queue = mp.Queue()
    stop_event = mp.Event()

    workers = []
    for cfg in configs:
        if is_live:
            source_uri = f"rtsp://{user}:{password}@{host}:554/Streaming/Channels/{cfg['channel']}"
            p = mp.Process(
                target=_camera_worker,
                args=(
                    cfg,
                    source_uri,
                    0.0,
                    args.backend,
                    args.high,
                    args.low,
                    args.fps,
                    args.analysis_fps,
                    args.rtsp,
                    not args.no_individual,
                    frame_q,
                    req_q,
                    resp_q,
                    event_q,
                    stop_event,
                    True,
                ),
            )
        else:
            assert footage_dir is not None
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
                    args.analysis_fps,
                    args.rtsp,
                    not args.no_individual,
                    frame_q,
                    req_q,
                    resp_q,
                    event_q,
                    stop_event,
                    False,
                ),
            )
        p.start()
        workers.append(p)

    with contextlib.suppress(Exception):
        global_tracker.face_embedder = get_face_embedder("onnx-cpu")

    canvas = np.zeros((1088, 1440, 3), dtype=np.uint8)
    tile_w, tile_h = 480, 544
    latest_tiles: dict[int, np.ndarray] = {}
    last_frame_at: dict[int, float] = {}
    unavailable_tile = np.full((tile_h, tile_w, 3), 32, dtype=np.uint8)
    unavailable_tile[:, :5] = (255, 180, 0)
    active_by_cam: dict[int, list[int]] = {i: [] for i in range(1, 7)}
    inference_counts = {i: 0 for i in range(1, 7)}
    prior_inference_counts = inference_counts.copy()
    inference_rates = {i: 0.0 for i in range(1, 7)}
    rate_window_start = time.monotonic()

    t0 = time.monotonic()
    shown, rate = 0, 0.0
    start_mono = time.monotonic()

    try:
        while not stop_event.is_set():
            if not is_live and (time.monotonic() - start_mono >= duration_s):
                break
            if any(not worker.is_alive() for worker in workers):
                raise RuntimeError("A camera worker stopped; inspect the demo log")

            # 1. Handle cross-camera global ID requests for C2..C6 (Cam 12 is excluded)
            while not req_q.empty():
                try:
                    cam_id, cam_name, feat, req_time, active_ids = req_q.get_nowait()
                    gid, is_new = global_tracker.match_or_create(
                        cam_id, cam_name, feat, req_time, active_ids
                    )
                    resp_q.put((cam_id, gid))
                    status = "NEW PERSON" if is_new else "RE-ID MATCH (SAME PERSON)"
                    print(
                        f"  [COUNT C{cam_id}] {cam_name}: Person P#{gid} ({status}) | "
                        f"Stairwell Headcount (ex Cam 12): {global_tracker.headcount}",
                        flush=True,
                    )
                except Exception:
                    break

            # 2. Handle face visibility score updates & no-angle camera handoffs
            while not event_q.empty():
                try:
                    ev = event_q.get_nowait()
                    kind = ev[0]
                    if kind == "FACE_SNAPSHOT":
                        _, cid, cname, gid, fscore, zcrop, acrop, afeat, ev_now = ev
                        merged_gid, is_best = global_tracker.update_face_candidate(
                            cid, cname, gid, fscore, zcrop, acrop, afeat, ev_now
                        )
                        if is_best:
                            gp = global_tracker.get_person(merged_gid)
                            spath = gp.snapshot_path if gp else "var/snapshots"
                            print(
                                f"  [FACE SNAPSHOT] Person P#{merged_gid} best face visibility "
                                f"score={fscore:.3f} on {cname} -> saved {spath}",
                                flush=True,
                            )
                    elif kind == "TRACK_HEARTBEAT":
                        _, cid, cname, gid, afeat, ev_now = ev
                        gp = global_tracker.get_person(gid)
                        if gp is not None:
                            gp.last_seen = ev_now
                            gp.last_cam_id = cid
                            gp.last_camera = cname
                            if afeat is not None:
                                gp.app_feature = 0.85 * gp.app_feature + 0.15 * afeat
                                gp.app_feature /= float(np.linalg.norm(gp.app_feature)) + 1e-6
                    elif kind == "FACE_HANDOFF":
                        _, cid, cname, gid, max_raw, ev_now = ev
                        if global_tracker.mark_camera_no_angle_handoff(cid, gid, ev_now):
                            print(
                                f"  [FACE HANDOFF] Person P#{gid}: {cname} never got a proper "
                                f"face score/angle (max_raw={max_raw:.2f}) -> skipping picture & "
                                f"sending to next camera",
                                flush=True,
                            )
                except Exception:
                    break

            # 3. Collect latest frame thumbnails and inference counts from workers
            while not frame_q.empty():
                try:
                    cid, thumb, tids, inference_count = frame_q.get_nowait()
                    latest_tiles[cid] = thumb
                    last_frame_at[cid] = time.monotonic()
                    active_by_cam[cid] = tids
                    inference_counts[cid] = inference_count
                except Exception:
                    break

            now = time.monotonic()
            elapsed = now - rate_window_start
            if elapsed >= 5.0:
                inference_rates = {
                    cid: (inference_counts[cid] - prior_inference_counts[cid]) / elapsed
                    for cid in range(1, 7)
                }
                prior_inference_counts = inference_counts.copy()
                rate_window_start = now

            # 4. Assemble 2x3 grid canvas and overlay zoomed-in face snapshots / handoff badges
            for cid in range(1, 7):
                row = (cid - 1) // 3
                col = (cid - 1) % 3
                y1, y2 = row * tile_h, (row + 1) * tile_h
                x1, x2 = col * tile_w, (col + 1) * tile_w
                if cid in latest_tiles and (
                    not is_live or time.monotonic() - last_frame_at[cid] < 3.5
                ):
                    tile_img = latest_tiles[cid].copy()
                    if cid > 1:
                        # Show zoomed-in face snapshot PiP on this camera tile
                        shown_pip = False
                        for gp_item in reversed(global_tracker.persons):
                            if gp_item.best_face_crop is not None and (
                                gp_item.best_face_cam_id == cid
                                or gp_item.global_id in active_by_cam[cid]
                            ):
                                draw_pip_snapshot(
                                    tile_img,
                                    gp_item.best_face_crop,
                                    f"P{gp_item.global_id} S:{int(gp_item.best_face_score * 100)}",
                                    f"SNAP C{gp_item.best_face_cam_id}",
                                )
                                shown_pip = True
                                break
                        if not shown_pip and active_by_cam[cid]:
                            active_gid = active_by_cam[cid][0]
                            gp = global_tracker.get_person(active_gid)
                            if gp is not None and gp.best_face_crop is None:
                                msg = (
                                    f"P{active_gid} FROM C{gp.skipped_cameras[-1]}"
                                    if gp.skipped_cameras
                                    else f"P{active_gid} WAIT FACE"
                                )
                                draw_label(
                                    tile_img,
                                    tile_w - len(msg) * 8 - 14,
                                    20,
                                    msg,
                                    (255, 190, 0),
                                    scale=2,
                                    text_colour=0,
                                )
                    canvas[y1:y2, x1:x2] = tile_img
                else:
                    canvas[y1:y2, x1:x2] = unavailable_tile
                    active_by_cam[cid] = []

            # 5. Burn in HUD text
            stairwell_active_ids = sorted(
                {gid for cid in range(2, 7) for gid in active_by_cam[cid]}
            )
            stairwell_active_info = (
                ", ".join(
                    f"C{cid}:P#{ids[0]}" for cid, ids in active_by_cam.items() if cid > 1 and ids
                )
                or "none"
            )
            cam12_count = len(active_by_cam[1])

            handoff_notes = []
            for gp_item in global_tracker.persons[-3:]:
                if gp_item.best_face_crop is not None:
                    handoff_notes.append(
                        f"P#{gp_item.global_id}:SNAP@C{gp_item.best_face_cam_id}"
                        f"({gp_item.best_face_score:.2f})"
                    )
                elif gp_item.skipped_cameras:
                    skipped_str = ",".join(f"C{c}" for c in gp_item.skipped_cameras)
                    handoff_notes.append(f"P#{gp_item.global_id}:SKIP({skipped_str})->NEXT")
                else:
                    handoff_notes.append(f"P#{gp_item.global_id}:SCANNING")
            face_status_str = "  ".join(handoff_notes) if handoff_notes else "waiting for person"

            if is_live:
                now_str = time.strftime("%H:%M:%S")
                hud_line1 = (
                    f"ARGUS LIVE HD  |  COUNT (EX CAM12): {global_tracker.headcount}  |  "
                    f"ACTIVE C2-C6: {len(stairwell_active_ids)} ({stairwell_active_info})  |  "
                    f"CAM12 LOCAL: {cam12_count}"
                )
                hud_line2 = (
                    f"FACE SNAPSHOTS: {global_tracker.captured_count}/"
                    f"{global_tracker.headcount}  |  {face_status_str}"
                )
                hud_line3 = f"TIME: {now_str}  |  {rate:.1f} FPS  |  AI FPS: " + " ".join(
                    f"C{cid}:{inference_rates[cid]:.1f}" for cid in range(1, 7)
                )
            else:
                now_sec = start_sec + (time.monotonic() - start_mono)
                hh, mm, ss = int(now_sec // 3600), int((now_sec % 3600) // 60), int(now_sec % 60)
                hud_line1 = (
                    f"ARGUS MULTI-CAM  |  COUNT (EX CAM12): {global_tracker.headcount}  |  "
                    f"ACTIVE C2-C6: {len(stairwell_active_ids)} ({stairwell_active_info})  |  "
                    f"CAM12 LOCAL: {cam12_count}"
                )
                hud_line2 = (
                    f"FACE SNAPSHOTS: {global_tracker.captured_count}/"
                    f"{global_tracker.headcount}  |  {face_status_str}"
                )
                hud_line3 = (
                    f"TIMELINE: {hh:02d}:{mm:02d}:{ss:02d}  |  {rate:.1f} FPS  |  AI FPS: "
                    + " ".join(f"C{cid}:{inference_rates[cid]:.1f}" for cid in range(1, 7))
                )
            write_hud(hud, f"{hud_line1}\n{hud_line2}\n{hud_line3}")

            # 6. Push to MediaMTX
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
            with contextlib.suppress(Exception):
                grid_proc.stdin.close()
        try:
            grid_proc.wait(timeout=2)
        except Exception:
            grid_proc.kill()
        for p in workers:
            if p.is_alive():
                p.terminate()
            p.join(timeout=1)
        print("Demo stopped cleanly.")


# --- main entrypoint ---------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", help="single video file or rtsp:// URL")
    parser.add_argument(
        "--multi", action="store_true", help="run 6-camera synchronized demo from footage"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="run 6-camera live RTSP streams (A12 -> A7 -> A10 -> A5 -> A13 -> A2)",
    )
    parser.add_argument(
        "--nvr-host", default=None, help="NVR host/IP (default: from CAMERA_HOST or 192.168.1.78)"
    )
    parser.add_argument("--time-range", default="17:36-17:42", help="range (e.g. 17:36-17:42)")
    parser.add_argument(
        "--footage-dir",
        default=str(Path.home() / "Documents/Footage/Camera Footage"),
        help="path to Camera Footage directory",
    )
    parser.add_argument("--path", default="demo", help="mediamtx path to publish to")
    parser.add_argument("--rtsp", default="rtsp://localhost:8554")
    parser.add_argument("--backend", default="onnx-cuda-yolo")
    parser.add_argument("--high", type=float, default=0.45, help="score that starts a track")
    parser.add_argument("--low", type=float, default=0.25, help="score that keeps one alive")
    parser.add_argument("--zones", type=Path, help="YAML list of {name, colour, polygon}")
    parser.add_argument("--start", type=float, default=0.0, help="seconds into a single-cam file")
    parser.add_argument("--fps", type=int, default=15, help="output frame rate")
    parser.add_argument(
        "--analysis-fps", type=float, default=3.0, help="per-camera inference rate in live mode"
    )
    parser.add_argument("--no-loop", action="store_true")
    parser.add_argument("--no-individual", action="store_true", help="skip individual streams")
    parser.add_argument("--record", type=Path, help="save output to video file")
    parser.add_argument(
        "--check-streams", action="store_true", help="validate all live NVR streams and exit"
    )
    args = parser.parse_args()
    if args.analysis_fps <= 0:
        parser.error("--analysis-fps must be positive")
    if args.check_streams:
        raise SystemExit(0 if check_live_streams() else 1)

    if args.live:
        run_multi_camera(args, is_live=True)
        return

    if args.multi or not args.source:
        run_multi_camera(args, is_live=False)
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
            fh, fw = img.shape[:2]
            people = [
                d
                for d in detector.detect(img, score_threshold=args.low)
                if is_valid_person_detection(d, fw, fh)
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
