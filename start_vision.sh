#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/argus-vision"
PID_FILE="$STATE_DIR/demo.pid"
LOG_FILE="$STATE_DIR/demo.log"
cd "$ROOT_DIR"
mkdir -p "$STATE_DIR"

if [[ -f "$PID_FILE" ]]; then
    old_pid="$(cat "$PID_FILE")"
    if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
        old_args="$(ps -p "$old_pid" -o args= || true)"
        if [[ "$old_args" == *"rig/bin/demo_overlay.py"* ]]; then
            echo "Vision demo is already running (PID $old_pid)."
            exit 0
        fi
    fi
    rm -f "$PID_FILE"
fi

if command -v uv >/dev/null 2>&1; then
    uv_cmd="$(command -v uv)"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
    uv_cmd="$HOME/.local/bin/uv"
else
    echo "uv is not installed." >&2
    exit 1
fi

echo "Checking all six NVR 960x1088 mainstreams before starting the demo..."
if ! "$uv_cmd" run --no-sync python rig/bin/demo_overlay.py --check-streams; then
    echo "Vision demo not started: one or more NVR streams are unusable." >&2
    exit 1
fi

if systemctl is-active --quiet facefusion.service; then
    if [[ ! -t 0 ]]; then
        echo "FaceFusion is active. Stop it interactively before starting the vision demo." >&2
        exit 1
    fi
    sudo systemctl stop facefusion.service
fi

ARGUS_PG_HOST_PORT=5434 docker compose -f infra/compose.dev.yaml up -d mediamtx

setsid "$uv_cmd" run --no-sync python rig/bin/demo_overlay.py \
    --live --no-individual --backend onnx-cuda-yolo --fps 10 --analysis-fps 3 "$@" \
    > "$LOG_FILE" 2>&1 < /dev/null &
pid=$!
echo "$pid" > "$PID_FILE"
sleep 2
if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Vision demo failed to start. See $LOG_FILE" >&2
    exit 1
fi
printf 'Vision demo started (PID %s).\nWeb: http://localhost:8888/demo/\nLog: %s\n' "$pid" "$LOG_FILE"
