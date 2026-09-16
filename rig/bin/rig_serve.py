"""Serve rig footage over RTSP via mediamtx, indistinguishably from a camera.

python rig/bin/rig_serve.py [--host localhost] [--port 8554] [--only name ...]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rig_common import PID_DIR, load_streams, read_pid, spawn


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8554)
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args()

    streams = [
        s for s in load_streams(args.host, args.port) if not args.only or s.name in args.only
    ]
    if not streams:
        raise SystemExit("no streams to serve (generate footage first: rig/synthetic/generate.py)")
    missing = [s.name for s in streams if not s.file.exists()]
    if missing:
        raise SystemExit(f"footage missing for: {missing}; run rig/synthetic/generate.py")

    PID_DIR.mkdir(parents=True, exist_ok=True)
    procs = {}
    for spec in streams:
        if read_pid(spec) is None:
            pid = spawn(spec)
            procs[spec.name] = pid
            print(f"serving {spec.name} -> {spec.rtsp_url} (pid {pid})")
        else:
            print(f"already serving {spec.name}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    print("stopping rig…")
    for spec in streams:
        with contextlib.suppress(ProcessLookupError):
            import os

            pid = read_pid(spec)
            if pid:
                os.kill(pid, signal.SIGTERM)
    for spec in streams:
        with contextlib.suppress(FileNotFoundError):
            spec.pid_file.unlink()


if __name__ == "__main__":
    asyncio.run(main())
