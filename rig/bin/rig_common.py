"""Shared rig plumbing: resolve manifests, spawn ffmpeg loopers, pidfiles."""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

RIG_ROOT = Path(__file__).resolve().parents[2]
FOOTAGE = RIG_ROOT / "rig" / "footage"
MANIFESTS = RIG_ROOT / "rig" / "manifests"
PID_DIR = RIG_ROOT / "var" / "rig"


@dataclass(slots=True)
class StreamSpec:
    name: str
    file: Path
    rtsp_url: str

    @property
    def pid_file(self) -> Path:
        return PID_DIR / f"{self.name}.pid"


def load_streams(
    host: str = "localhost", port: int = 8554, manifests_dir: Path = MANIFESTS
) -> list[StreamSpec]:
    streams = []
    for mf in sorted(manifests_dir.glob("*.yaml")):
        data = yaml.safe_load(mf.read_text())
        streams.append(
            StreamSpec(
                name=data["name"],
                file=FOOTAGE / data["file"],
                rtsp_url=f"rtsp://{host}:{port}/{data['name']}",
            )
        )
    return streams


def ffmpeg_cmd(spec: StreamSpec) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-re",
        "-stream_loop",
        "-1",
        "-i",
        str(spec.file),
        "-c",
        "copy",
        "-rtsp_transport",
        "tcp",
        "-f",
        "rtsp",
        spec.rtsp_url,
    ]


def spawn(spec: StreamSpec) -> int:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ffmpeg_cmd(spec),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    spec.pid_file.write_text(str(proc.pid))
    return proc.pid


def read_pid(spec: StreamSpec) -> int | None:
    try:
        pid = int(spec.pid_file.read_text().strip())
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None
    # a killed-but-unreaped child answers kill(pid, 0) as a zombie; it is dead
    try:
        with open(f"/proc/{pid}/stat") as fh:
            state = fh.read().split(") ")[-1].split()[0]
        if state == "Z":
            return None
    except OSError:
        pass  # non-Linux or gone: signal check above was the verdict
    return pid


def send(spec: StreamSpec, sig: signal.Signals) -> bool:
    pid = read_pid(spec)
    if pid is None:
        return False
    os.kill(pid, sig)
    return True
