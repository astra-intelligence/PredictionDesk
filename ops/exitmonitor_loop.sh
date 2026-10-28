#!/bin/bash
# ops/exitmonitor_loop.sh — 5-min exit-monitor cadence under supervisord.
#
# The repo's exit monitor runs ONE pass then exits (its own 180s SIGALRM self-kill bounds a network
# hang); systemd gets the ~5-min cadence from Restart=always + RestartSec=300. supervisord has no
# run-interval primitive, so this wrapper reproduces it: run a pass, stamp lastpass (the /ops page's
# freshness source), sleep 300, repeat. supervisord stops the whole group (stopasgroup/killasgroup),
# so a mid-pass python child dies with this shell.
#
# PAPER NOTE: with live disarmed each pass SWEEPS every on-disk positions_*.json read-only (held
# counts + exit params resolved from the config row, DISABLED rows included — pause is never
# "unmanaged") and places nothing: paper TP/SL exits happen inside the strategy at fire time; the
# loop keeps the live-tight path warm and proven for the day a scoped PASS exists.
set -u
DATA_DIR="${POLYEDGE_DATA_DIR:-$HOME/polyedge-data}"
mkdir -p "$DATA_DIR/logs"
while :; do
  python3 -m desk.driver.exit_monitor
  date +%s > "$DATA_DIR/logs/exitmonitor.lastpass"
  sleep 300
done
