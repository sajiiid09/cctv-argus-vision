#!/usr/bin/env bash
# start_stream.sh — Start Argus Vision Live / Demo Stream
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Locate uv binary
if command -v uv >/dev/null 2>&1; then
    UV_CMD="uv"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV_CMD="$HOME/.local/bin/uv"
else
    echo "Error: uv not found in PATH or ~/.local/bin/uv" >&2
    exit 1
fi

if [ $# -eq 0 ]; then
    exec "$ROOT_DIR/start_vision.sh"
fi

for arg in "$@"; do
    if [[ "$arg" == "--live" ]]; then
        echo "Use ./start_vision.sh for live cameras; it checks all six feeds first." >&2
        exit 1
    fi
done

ARGUS_PG_HOST_PORT=5434 docker compose -f infra/compose.dev.yaml up -d mediamtx
exec "$UV_CMD" run --no-sync python rig/bin/demo_overlay.py "$@"
