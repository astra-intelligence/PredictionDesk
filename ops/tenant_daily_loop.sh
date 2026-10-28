#!/bin/bash
# ops/tenant_daily_loop.sh — daily tenant substrate jobs under supervisord.
#
# Generic form: once a day, run every discovered tenant's manifest `bootstrap` modules (the same
# thing `make bootstrap` does) so file-based substrate (market metadata, calibration series, …)
# stays topped up. A tree whose tenants declare no bootstrap modules makes this a cheap no-op.
# Bootstrap modules must be incremental / append-only, so re-running is always safe.
#
# SELF-HEALING cadence: on a failed pass (e.g. the venue unreachable from this network) retry
# every 2h; once a pass succeeds, settle to daily.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
while :; do
  if python3 -m desk.signals.registry bootstrap; then
    echo "[tenantdaily] bootstrap pass done — next pass in 24h" && sleep 86400
  else
    echo "[tenantdaily] bootstrap pass FAILED — retry in 2h" && sleep 7200
  fi
done
