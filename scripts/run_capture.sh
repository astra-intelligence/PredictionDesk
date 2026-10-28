#!/bin/bash
# Launch the collector detached (survives terminal/session close). Logs -> capture.out, PID -> capture.pid.
# Usage: scripts/run_capture.sh [config.json]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
CFG="${1:-desk/capture_config.json}"
mkdir -p desk/data/capture
nohup python3 -m desk.data.capture "$CFG" > desk/data/capture/capture.out 2>&1 &
echo $! > desk/data/capture/capture.pid
echo "[run_capture] started pid=$(cat desk/data/capture/capture.pid) cfg=$CFG"
echo "[run_capture] tail -f desk/data/capture/capture.out"
