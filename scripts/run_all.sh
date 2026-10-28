#!/usr/bin/env bash
# Dev / foreground quick-start: capture + strategy driver + dashboard + exit monitor, all as
# background children of this script. Ctrl-C (or any exit) kills every child. Plain bash + nohup,
# so it works the same on macOS and Linux — no systemd, no launchctl.
#
# Paper by default. Live needs LIVE_TRADING_ENABLED=true + a scoped PASS + wallet keys (in .env).
# For a durable server deploy use the systemd units in scripts/systemd/ instead.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
mkdir -p desk/data/capture desk/driver

pids=()
cleanup() {
  echo
  echo "[run_all] stopping ${#pids[@]} child process(es)…"
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null || true; done   # ${…+…} = safe under
  # set -u on bash 3.2 (macOS) when the trap fires before any child started (empty array)
}
trap cleanup EXIT INT TERM

start() {   # start <name> <logfile> <cmd…>
  local name="$1" log="$2"; shift 2
  nohup "$PY" "$@" >"$log" 2>&1 &
  local pid=$!
  pids+=("$pid")
  printf '[run_all] %-16s pid=%-7s log=%s\n' "$name" "$pid" "$log"
}

start capture         desk/data/capture/capture.out  -m desk.data.capture desk/capture_config.json
start strategydriver  desk/driver/driver.out         -m desk.driver.strategy_driver
start dashboard       desk/driver/dashboard.out      -m desk.dashboard_server

# exit_monitor's entrypoint is a single check pass, so wrap it in a ~5-min poll loop (mirrors how a
# process supervisor reruns it). Kept as one child pid so cleanup tears the whole loop down.
( while true; do "$PY" -m desk.driver.exit_monitor; sleep 300; done ) \
  >desk/driver/exitmonitor.out 2>&1 &
pids+=("$!")
printf '[run_all] %-16s pid=%-7s log=%s\n' "exitmonitor" "$!" "desk/driver/exitmonitor.out"

echo "[run_all] all started — dashboard on http://localhost:8788 — Ctrl-C to stop."
wait
