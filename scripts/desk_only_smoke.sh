#!/usr/bin/env bash
# desk-only smoke — prove plug-and-play: the framework imports, gates, paper-trades and exit-monitors
# with NO tenant present. `make eval-core` runs this. Steps:
#   1. strategies/ moved aside (RESTORED on any exit — trap);
#   2. `import desk` + every formerly tenant-coupled framework module imports clean;
#   3. core eval gate (--fast) = RESULT: PASS, tenant sections skipped WITH REASON (not failed);
#   4. one paper driver tick on a freshly SEALED calendar sample row (DESK_STRATEGY_CONFIG);
#   5. one exit-monitor pass (clean skip: live not enabled).
# Runtime data is re-rooted into a temp DESK_DATA_DIR so the smoke never touches real state.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
ASIDE="${DESK_SMOKE_ASIDE:-/tmp/strategies-aside}"
SMOKE_DATA="$(mktemp -d /tmp/desk-smoke-data.XXXXXX)"
MOVED=0

restore() {
  if [ "$MOVED" = 1 ] && [ -d "$ASIDE" ] && [ ! -e "$ROOT/strategies" ]; then
    mv "$ASIDE" "$ROOT/strategies"
    echo "[smoke] strategies/ restored"
  fi
  rm -rf "$SMOKE_DATA"
}
trap restore EXIT INT TERM

if [ -e "$ROOT/strategies" ]; then
  if [ -e "$ASIDE" ]; then
    echo "[smoke] $ASIDE already exists — refusing to clobber it; remove/restore it first" >&2
    exit 2
  fi
  mv "$ROOT/strategies" "$ASIDE"
  MOVED=1
  echo "[smoke] strategies/ -> $ASIDE (desk-only tree)"
else
  echo "[smoke] no strategies/ present — already a desk-only tree"
fi

export DESK_DATA_DIR="$SMOKE_DATA"

echo "[smoke] 1/4 import desk (framework modules import with no tenant)"
"$PY" - <<'PYEOF'
import desk  # noqa: F401
import desk.signals.registry as R
import desk.driver.strategy_driver, desk.driver.exit_monitor, desk.eval.run_eval      # noqa: F401,E401
import desk.data.capture_select, desk.data.central_sources                            # noqa: F401,E401
import desk.live.executor, desk.reports.run_report, desk.signals.gate                 # noqa: F401,E401
specs = R.load_registry()   # tenant rows must be LOUDLY refused, never crash or silently load
builtins = set(R.BUILTIN_VERSIONS)  # with strategies/ aside, ONLY builtin-named specs may survive
assert all(s.name in builtins for s in specs), [s.name for s in specs]
print("    import desk: OK; registry load clean (tenant rows refused loudly above)")
PYEOF

echo "[smoke] 2/4 core eval gate (--fast) on the desk-only tree"
out="$("$PY" -m desk.eval.run_eval --fast 2>&1)" || { echo "$out" | tail -40; exit 1; }
echo "$out" | grep -q "RESULT: PASS" || { echo "$out" | tail -40; exit 1; }
echo "$out" | grep -q "tenant sections skipped (desk-only tree)" \
  || { echo "[smoke] FAIL: tenant-skip reason missing from gate output"; exit 1; }
echo "$out" | grep -E '^\[(PASS|FAIL|WARN)\]|RESULT:' | sed 's/^/    /'

echo "[smoke] 3/4 paper driver --once on a sealed calendar sample row"
SMOKE_CFG="$SMOKE_DATA/strategy_config.json"
"$PY" - "$SMOKE_CFG" <<'PYEOF'
import json, sys
from desk.signals import registry as R
bases = R.base_specs()
row = {"name": "calendar", "strategy": "calendar", "enabled": True,
       "schedule": {"type": "interval", "secs": 21600},
       "params": {"qty": 5.0, "n_markets": 1, "mode": "taker"}}
row["config_hash"] = R.seal_hash(row, bases["calendar"])     # seal exactly like a real registration
json.dump({"strategies": [row]}, open(sys.argv[1], "w"))
print("    sealed calendar row ->", sys.argv[1])
PYEOF
drv="$(DESK_STRATEGY_CONFIG="$SMOKE_CFG" "$PY" -m desk.driver.strategy_driver --once 2>&1)"
echo "$drv" | sed 's/^/    /'
echo "$drv" | grep -q '"strategy": "calendar"' || { echo "[smoke] FAIL: calendar did not fire"; exit 1; }
echo "$drv" | grep -q '"error"' && { echo "[smoke] FAIL: a fire errored (see above)"; exit 1; }

echo "[smoke] 4/4 exit monitor single pass"
em="$(DESK_STRATEGY_CONFIG="$SMOKE_CFG" "$PY" -m desk.driver.exit_monitor 2>&1)"
echo "$em" | sed 's/^/    /'
echo "$em" | grep -q '"exit_monitor_run"' || { echo "[smoke] FAIL: exit monitor pass did not complete"; exit 1; }

echo "[smoke] desk-only smoke PASS"
