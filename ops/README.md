# ops/ — the supervised paper stack

One supervisord config runs the whole desk: capture, strategy driver, exit monitor (5-min loop),
dashboard, the tenant's daily substrate jobs (build_meta + calibration extender — data-dependent
arms fail closed on a >7d-stale substrate), and (macOS) caffeinate. Paper only — no `.env` is loaded, `LIVE_TRADING_ENABLED` is never
set, and the private strategy config rides in via `DESK_STRATEGY_CONFIG` pointing OUTSIDE any git tree
(`~/polyedge-data/config/strategy_config.private.json`).

```bash
make ops-start     # supervisord -c ops/supervisord.conf  (+ status print)
make ops-status    # one-glance process table
make ops-tail      # last lines of every program log
make ops-stop      # grouped shutdown, supervisord included
```

- Web UI: http://127.0.0.1:9100 (localhost only; also the XML-RPC the /ops page queries)
- Ops health page: http://127.0.0.1:8788/ops (rendered by the dashboard server; file-based, still
  works when supervisord is down)
- Logs: `~/polyedge-data/logs/*.log` (10MB × 3 rotation per program)

## Pause semantics (config `enabled` flips — no restart needed)

The driver re-reads the strategy config (JSON + seal verify) at the top of EVERY 30s loop
iteration, so flipping a row's `enabled` takes effect at the next tick without touching the stack:

- **Pause** (`enabled: false`) = **no new entries** from the next fire window — NEVER "no risk
  management": the settle pass unions every on-disk `positions_*.json`, and the exit monitor sweeps
  them all too, resolving TP/SL from the config row even while the row is disabled. A positions file
  with no config row at all is managed with the framework default TP/SL=none (loudly logged;
  settle-on-resolution remains its only exit).
- **Paper TP/SL nuance:** paper TP/SL runs INSIDE the strategy at fire time (the exit monitor
  places orders only on the live path), so pausing a PAPER arm leaves its open positions riding to
  settlement with no TP/SL management. Pause a paper arm only when that is acceptable, or flatten
  first.
- **Resume** (`enabled: true`) schedules the row at its NEXT window — never an immediate off-window
  fire. (A driver RESTART still fires everything immediately; that behavior is unchanged.)
- **Stopping the stack:** `stopwaitsecs=1260` on the driver lets a mid-flight fire (legally up to
  1200s) finish between SIGTERM and SIGKILL — prefer `make ops-stop`/`supervisorctl` over `kill -9`,
  and avoid restarts during the 00:30/06:30/12:30/18:30 UTC fire windows (+20 min).

## Why supervisord (and the honest systemd comparison)

**For it:** one config file works on BOTH macOS and a Linux VPS (the repo's launchd plists and
systemd units are two parallel dialects of the same five programs); `supervisorctl status` is a
one-glance table launchctl simply doesn't have; the localhost web UI + XML-RPC feeds the /ops page a
live process table for free; `supervisorctl stop desk:*` is a grouped stop; per-program log files with
rotation, no journald/log-show spelunking.

**Honestly for systemd (Linux):** systemd survives host reboots out of the box (`enable`), restarts
supervisord itself if IT dies (here supervisord is the root of the tree and nothing watches it),
has native per-unit resource limits/cgroups, socket activation, timers (the repo's daily
build-meta/settlement timers are systemd units), and journald with structured metadata. On a
long-lived VPS the right end-state is systemd units for everything, `scripts/systemd/` — this
supervisord layer is the Mac-friendly, OS-portable middle ground; on the VPS you can either keep it
under a single tiny systemd unit (`ExecStart=... supervisord -n -c ops/supervisord.conf`) or drop it
for the native units.

## Same conf on a Linux VPS — the one-liner differences

Paths are env-derived (`%(ENV_HOME)s`, `%(here)s`), so with the same conventions
(checkout anywhere, venv at `~/.venvs/polyedge`, data at `~/polyedge-data`) the file runs unchanged,
except:

1. **caffeinate**: macOS-only — delete the `[program:caffeinate]` block (or let it go FATAL after 3
   retries; the group is unaffected).
2. **Reboot survival**: add the one systemd user unit above (or a `@reboot` cron:
   `@reboot $HOME/.venvs/polyedge/bin/supervisord -c $HOME/desk-repo/ops/supervisord.conf`).
3. **bash path**: `/bin/bash` exists on both; nothing to change.

## Mac sleep caveat

`caffeinate -dims` prevents **idle** sleep (display/disk/system) while the stack runs. It does NOT
prevent lid-close sleep on a MacBook running on battery with no external display. For a true 24/7
Mac run: keep it on power + `caffeinate` (this conf), or closed-lid with external display/keyboard,
or `sudo pmset -a sleep 0`. Expect gaps in capture if the lid closes.

## The /ops page

`GET /ops` on the dashboard server regenerates (≤1/min) and serves `dashboard/ops.html` via
`.claude/skills/desk-dashboard/scripts/build_ops.py` — process table (supervisord XML-RPC), capture
freshness, driver heartbeat + per-arm last fire, exit-monitor lastpass, calibration substrate age
(the 7-day fail-closed gate), Open-Meteo quota meter, paper P&L, disk usage; red/amber/green chips
per row; auto-refreshes every 60s. When supervisord is down the process table degrades to a note and
every file-based row still renders. It can also be built one-off:
`python3 .claude/skills/desk-dashboard/scripts/build_ops.py`.
