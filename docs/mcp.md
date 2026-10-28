# MCP: drive the desk from Claude

PredictionDesk ships a stdio MCP server (`desk/mcp_server.py`, official python `mcp` SDK), so
Claude — or any MCP client — can sit at the desk: read health, activity and weekly P&L, look up
any term in the glossary, see what the notifier is alerting on, and pause/resume arms through the
same audited control path the dashboard UI uses.

## Connect

The dashboard must be running (`make ops-start`, or `python3 -m desk.dashboard_server`); the MCP
server is a thin client of its localhost HTTP API.

Claude Code (run from the repo root, with the project venv's python on PATH):

```bash
claude mcp add predictiondesk -- python3 -m desk.mcp_server
```

Any other MCP client (generic stdio config — set `cwd` to this repo so `-m desk.mcp_server`
resolves, and `command` to a python that has `requirements.txt` installed):

```json
{
  "mcpServers": {
    "predictiondesk": {
      "command": "python3",
      "args": ["-m", "desk.mcp_server"],
      "cwd": "/path/to/PredictionDesk"
    }
  }
}
```

Environment knobs: `DESK_DASH_URL` (dashboard base URL; default `http://127.0.0.1:$DASH_PORT`,
`DASH_PORT` default 8788) and `DESK_DATA_DIR` (data home holding the notifier state file; default
`~/polyedge-data`).

## Tools

| Tool | Arguments | What it does |
|---|---|---|
| `desk_health` | — | `GET /api/health` passthrough: processes, capture/driver/exits freshness, forecast memory, quota, paper P&L, live gates, per-arm rows, THRESHOLDS |
| `desk_events` | `limit` (default 50, max 60) | Recent activity newest-first: fires, control flips, process starts/exits |
| `desk_week` | — | Last 7 UTC days of realized P&L with per-arm attribution |
| `desk_strategies` | — | The per-arm rows: 4-state mode, mode-since, open positions, last fire, today counts, rule line |
| `desk_explain` | `term` | Plain-language glossary lookup, fuzzy on name/label/aka; threshold numbers come from the live health payload |
| `desk_alerts` | — | The notifier's currently-active conditions + when each transitioned, from its state file |
| `desk_pause` | `name` | Pause one arm via `POST /api/strategy/<name>/pause` (audited) |
| `desk_resume` | `name`, `confirm_name` (optional) | Resume one arm via `POST /api/strategy/<name>/resume` (audited); live-routed arms demand the typed confirm |
| `desk_pause_all` | — | Pause every enabled arm via `POST /api/stack/pause-all` (audited kill-switch pause tier) |

Control semantics the client sees verbatim (`{status, body}` passthrough):

- **409** — resuming a live-routed arm needs typed confirmation; the body names the exact
  `confirm_name` to send back. Ask the human first.
- **429** — control debounce; retry after `retry_after_s`.
- **403** — control gate (missing `X-Desk-Control` header or non-allowlisted Host).
- **404** — unknown arm (the body lists known names).
- **500** — refused/rolled-back flip (structural guard, loader verify or audit failure).

Every flip the control tools make goes through the dashboard's HTTP control API with the
`X-Desk-Control` header, so it lands in the append-only audit JSONL
(`$DESK_DATA_DIR/audit/control_audit.jsonl`) exactly like a UI action — when it happened, prior →
new, when it takes effect, from where.

## What MCP access deliberately excludes

By design, not omission — the eval gate's `mcp_server` section enforces the inventory and the
import ban statically:

- **No live-arming.** Neither `LIVE_TRADING_ENABLED` nor any PASS can be set from here; the
  escalation ritual stays manual and out of band.
- **No order placement.** There is no tool that submits, amends or cancels an order.
- **No config or seal mutation.** The server never writes the strategy config; it never imports
  the config-writing internals (`desk.control_plane`, `desk.signals`). Pause/resume happen only
  through the audited HTTP control path, which can flip `enabled` and nothing else.

Your assistant can watch the desk and stop it; it cannot bet your money.
