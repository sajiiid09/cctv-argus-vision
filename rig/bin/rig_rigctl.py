"""Rig fault injection (ARCHITECTURE.md §6 — the rig's most valuable feature).

python rig/bin/rig_rigctl.py status
python rig/bin/rig_rigctl.py stop canteen_door_01      # camera unplugged (SIGKILL)
python rig/bin/rig_rigctl.py pause canteen_door_01     # WiFi stall: TCP up, no frames
python rig/bin/rig_rigctl.py resume canteen_door_01    # SIGCONT
python rig/bin/rig_rigctl.py start canteen_door_01     # restart the loop for one stream
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rig_common import load_streams, read_pid, send, spawn


def find(name: str, host: str = "localhost", port: int = 8554):
    for spec in load_streams(host, port):
        if spec.name == name:
            return spec
    raise SystemExit(f"unknown stream {name!r} (manifests in rig/manifests/)")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    if cmd == "status":
        for spec in load_streams():
            pid = read_pid(spec)
            state = f"up pid={pid}" if pid else "down"
            print(f"{spec.name:20s} {state}  {spec.rtsp_url}")
        return
    name = sys.argv[2]
    spec = find(name)
    if cmd == "stop":
        ok = send(spec, signal.SIGKILL)
        print(f"{name}: killed" if ok else f"{name}: not running")
    elif cmd == "pause":
        ok = send(spec, signal.SIGSTOP)
        print(f"{name}: paused (stall)" if ok else f"{name}: not running")
    elif cmd == "resume":
        ok = send(spec, signal.SIGCONT)
        print(f"{name}: resumed" if ok else f"{name}: not running")
    elif cmd == "start":
        if read_pid(spec) is None:
            print(f"{name}: serving pid={spawn(spec)}")
        else:
            print(f"{name}: already running")
    else:
        raise SystemExit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    main()
