#!/usr/bin/env bash
# Run the strategy driver in the background without launchd (quick manual start).
# Paper by default; live requires LIVE_TRADING_ENABLED=true and an evaluator PASS file.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
mkdir -p desk/driver
nohup "$PY" -m desk.driver.strategy_driver >> desk/driver/driver.out 2>> desk/driver/driver.err &
echo "strategy_driver started pid $! (paper unless LIVE_TRADING_ENABLED+PASS). logs: desk/driver/driver.{out,err}"
