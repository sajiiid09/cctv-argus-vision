#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/argus-vision"
PID_FILE="$STATE_DIR/demo.pid"
cd "$ROOT_DIR"

if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        args="$(ps -p "$pid" -o args= || true)"
        if [[ "$args" != *"rig/bin/demo_overlay.py"* ]]; then
            echo "PID $pid is no longer the vision demo; leaving it alone." >&2
            exit 1
        fi
        kill -INT -- "-$pid"
        for _ in {1..10}; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM -- "-$pid"
        fi
        echo "Vision demo stopped."
    else
        echo "Vision demo is not running."
    fi
    rm -f "$PID_FILE"
else
    echo "Vision demo is not running."
fi

ARGUS_PG_HOST_PORT=5434 docker compose -f infra/compose.dev.yaml stop mediamtx
