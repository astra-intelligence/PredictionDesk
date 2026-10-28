#!/bin/bash
# ops/capture_validate_loop.sh — capture validator + incremental compactor cadence under supervisord.
#
# Split per architecture review B19: validate() OBSERVES only (freshness/coverage/health ->
# _status.json + capture_health.log); desk.data.compactor is the only thing that MUTATES the
# capture store (bounded pass: oldest-first, <=max-dirs leaf dirs, 10-min age floor, atomic
# verify-then-delete merges — see the module docstring). supervisord has no run-interval
# primitive, so this wrapper reproduces the cadence: compactor every 10 min (sized to keep up
# with ~65k part files/h), validator every 3rd cycle (~30 min, its historical cadence).
#
# Both exit nonzero on problems (validator hard-fail / compactor abort); `|| true` keeps the loop
# alive — the failure signal lives in _status.json / capture_health.log / compactor state, which
# the /ops page and notifier read. supervisord stops the loop as a group (stopasgroup).
set -u
DATA_DIR="${POLYEDGE_DATA_DIR:-$HOME/polyedge-data}"
mkdir -p "$DATA_DIR/logs"
i=0
while :; do
  if (( i % 3 == 0 )); then
    python3 -m desk.data.capture_validate || true
  fi
  python3 -m desk.data.compactor || true
  date +%s > "$DATA_DIR/logs/capturevalidate.lastpass"
  i=$((i + 1))
  sleep 600
done
