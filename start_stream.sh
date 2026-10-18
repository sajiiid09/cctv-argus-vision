#!/usr/bin/env bash
# start_stream.sh — Start Argus Vision Live / Demo Stream
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Ensure MediaMTX container is running
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "infra-mediamtx-1"; then
    echo "Starting MediaMTX container..."
    ARGUS_PG_HOST_PORT=5434 docker compose -f infra/compose.dev.yaml up -d mediamtx
fi

# Locate uv binary
if command -v uv >/dev/null 2>&1; then
    UV_CMD="uv"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV_CMD="$HOME/.local/bin/uv"
else
    echo "Error: uv not found in PATH or ~/.local/bin/uv" >&2
    exit 1
fi

echo "=========================================================="
echo " Starting Argus Vision Live 6-Camera Stream (A12->A7->A10->A5->A13->A2)"
echo " Web UI (2x3 Grid): http://localhost:8888/demo/"
echo " RTSP Output:       rtsp://localhost:8554/demo"
echo "=========================================================="

if [ $# -eq 0 ]; then
    exec "$UV_CMD" run --no-sync python rig/bin/demo_overlay.py --live --no-individual --backend onnx-cuda-yolo
else
    exec "$UV_CMD" run --no-sync python rig/bin/demo_overlay.py "$@"
fi
