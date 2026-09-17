"""Deterministic synthetic footage for the virtual camera rig (FOOTAGE.md §1).

Schedules are hardcoded — no RNG — so the same command produces the same video
and the same ground truth. People are rendered as disks ("head + shoulders"
from an overhead-ish doorway camera); the scenes carry no appearance realism
and must never be used for accuracy claims (TESTING.md §6).

Writes:
  rig/footage/*.mp4          (git-ignored)
  rig/manifests/*.yaml       (committed: wall-clock mapping + ground truth)
"""

from __future__ import annotations

import sys
from pathlib import Path

import av
import numpy as np
import yaml

RIG_ROOT = Path(__file__).resolve().parents[2]
FOOTAGE = RIG_ROOT / "rig" / "footage"
MANIFESTS = RIG_ROOT / "rig" / "manifests"

FPS = 30
CODEC = "libx264"

DoorMover = dict[str, float | str]


def _canteen_schedule(offset: float) -> list[DoorMover]:
    """Movers cross x=320. `offset` staggers the two doors' schedules."""
    return [
        {"start": 3.0 + offset, "dir": "east", "speed": 60.0, "y": 180, "start_x": 40},
        {"start": 9.0 + offset, "dir": "west", "speed": 55.0, "y": 200, "start_x": 600},
        {"start": 15.0 + offset, "dir": "east", "speed": 70.0, "y": 120, "start_x": 60},
        # two at once, overlapping (the tailgating/occlusion case)
        {"start": 21.0 + offset, "dir": "east", "speed": 62.0, "y": 170, "start_x": 50},
        {"start": 21.4 + offset, "dir": "east", "speed": 58.0, "y": 195, "start_x": 30},
        {"start": 27.0 + offset, "dir": "west", "speed": 66.0, "y": 150, "start_x": 620},
        # slow loiterer (near-dwell, ambiguous shape)
        {"start": 33.0 + offset, "dir": "east", "speed": 18.0, "y": 250, "start_x": 200},
        {"start": 41.0 + offset, "dir": "east", "speed": 64.0, "y": 100, "start_x": 40},
        {"start": 47.0 + offset, "dir": "west", "speed": 52.0, "y": 230, "start_x": 640},
        {"start": 53.0 + offset, "dir": "east", "speed": 68.0, "y": 160, "start_x": 20},
    ]


def _disk(frame: np.ndarray, cx: float, cy: float, r: float, color: tuple[int, int, int]) -> None:
    h, w = frame.shape[:2]
    x0, x1 = max(0, int(cx - r)), min(w, int(cx + r) + 1)
    y0, y1 = max(0, int(cy - r)), min(h, int(cy + r) + 1)
    if x1 <= x0 or y1 <= y0:
        return
    ys = np.arange(y0, y1)[:, None]
    xs = np.arange(x0, x1)[None, :]
    mask = (ys - cy) ** 2 + (xs - cx) ** 2 <= r * r
    region = frame[y0:y1, x0:x1]
    region[mask] = color


def _canteen_frame(t: float, schedule: list[DoorMover]) -> np.ndarray:
    frame = np.full((360, 640, 3), (44, 44, 44), dtype=np.uint8)
    frame[:, 317:323] = (110, 110, 110)  # the door line
    frame[0:20, :] = (70, 70, 90)  # door header
    for m in schedule:
        dt = t - float(m["start"])  # type: ignore[arg-type]
        if dt < 0:
            continue
        x = float(m["start_x"]) + float(m["speed"]) * dt * (1 if m["dir"] == "east" else -1)  # type: ignore[operator]
        if -60 < x < 700:
            shade = (150, 170, 190) if m["dir"] == "east" else (190, 170, 150)  # type: ignore[index]
            _disk(frame, x, float(m["y"]), 16, shade)  # type: ignore[arg-type]
    return frame


def _gate_frame(t: float, _: object = None) -> np.ndarray:
    """One person presents at close range, pauses, leaves. Loops every 20 s."""
    frame = np.full((270, 480, 3), (60, 60, 60), dtype=np.uint8)
    period = 20.0
    lt = t % period
    if lt < 4:
        x = 60 + lt * 70
    elif lt < 14:
        x = 240 + (lt - 4) * 2
    else:
        x = 240 + (lt - 14) * 80
    _disk(frame, x, 135, 40, (170, 180, 160))
    _disk(frame, x, 135, 30, (140, 150, 130))
    return frame


def _floor_frame(t: float, _: object = None) -> np.ndarray:
    """4x8 seated stations; occupancy toggles on a fixed schedule."""
    frame = np.full((360, 640, 3), (50, 50, 46), dtype=np.uint8)
    for row in range(4):
        for col in range(8):
            x0, y0 = 40 + col * 74, 40 + row * 80
            seat_on = ((row * 8 + col + int(t // 10)) % 5) != 0 or t < 10
            color = (150, 150, 120) if seat_on else (70, 70, 60)
            frame[y0 + 10 : y0 + 50, x0 : x0 + 50] = color
    return frame


SCENES: dict[str, dict] = {
    "canteen_door_01": {
        "frame_fn": lambda t: _canteen_frame(t, _canteen_schedule(0.0)),
        "schedule": _canteen_schedule(0.0),
        "duration": 60,
        "role": "canteen_door",
    },
    "canteen_door_02": {
        "frame_fn": lambda t: _canteen_frame(t, _canteen_schedule(7.0)),
        "schedule": _canteen_schedule(7.0),
        "duration": 60,
        "role": "canteen_door",
    },
    "gate_door": {"frame_fn": _gate_frame, "schedule": None, "duration": 60, "role": "gate"},
    "floor_view": {"frame_fn": _floor_frame, "schedule": None, "duration": 60, "role": "floor"},
}

PERSON_LABELS = {"person"}


def crossings_for(schedule: list[DoorMover], door_x: float = 320.0) -> list[dict]:
    out = []
    for m in schedule:
        start = float(m["start"])  # type: ignore[arg-type]
        speed = abs(float(m["speed"]))  # type: ignore[arg-type]
        start_x = float(m["start_x"])  # type: ignore[arg-type]
        t = start + (door_x - start_x) / (speed * (1 if m["dir"] == "east" else -1))
        if start <= t <= start + 60:
            direction = "enter" if m["dir"] == "east" else "exit"
            out.append({"t": round(t, 3), "direction": direction})
    return sorted(out, key=lambda c: c["t"])


def encode(name: str, scene: dict, out_dir: Path, fps: int = FPS) -> Path:
    out = out_dir / f"{name}.mp4"
    height, width = 360, 640
    if name == "gate_door":
        height, width = 270, 480
    container = av.open(str(out), mode="w", format="mp4")
    try:
        stream = container.add_stream(CODEC, rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        # One keyframe per second, matching the I-frame interval we set on the
        # real cameras (ADR-0029/ADR-0031). libx264's default GOP of 250 leaves
        # a 15-second packet ring holding one keyframe or none, which makes
        # clip extraction snap back several seconds or fail outright -- measured,
        # not assumed: a 5-second capture off the rig contained zero keyframes.
        stream.gop_size = fps
        stream.options = {"crf": "20", "preset": "veryfast", "g": str(fps)}
        n_frames = scene["duration"] * fps
        for i in range(n_frames):
            t = i / fps
            img = scene["frame_fn"](t)
            vframe = av.VideoFrame.from_ndarray(img, format="rgb24")
            vframe.pts = i
            for packet in stream.encode(vframe):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    return out


def generate(names: list[str] | None = None) -> list[Path]:
    FOOTAGE.mkdir(parents=True, exist_ok=True)
    MANIFESTS.mkdir(parents=True, exist_ok=True)
    written = []
    for name, scene in SCENES.items():
        if names and name not in names:
            continue
        out = encode(name, scene, FOOTAGE)
        height, width = (270, 480) if name == "gate_door" else (360, 640)
        manifest = {
            "name": name,
            "file": out.name,
            "resolution": [width, height],
            "fps": FPS,
            "duration_s": scene["duration"],
            "codec": "h264",
            "licence": "synthetic-generated (this repo)",
            "consent": "not-applicable (no real people)",
            "local_start": "13:58:00",
            "timezone": "Asia/Dhaka",
            "role": scene["role"],
        }
        if scene["schedule"]:
            manifest["ground_truth"] = {
                "door_line_x": 320,
                "crossings": crossings_for(scene["schedule"]),
            }
        (MANIFESTS / f"{name}.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
        written.append(out)
    return written


if __name__ == "__main__":
    wanted = sys.argv[1:] or None
    for p in generate(wanted):
        print(f"wrote {p} (+ manifest)")
