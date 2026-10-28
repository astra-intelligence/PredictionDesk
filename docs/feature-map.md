# Feature map

<!-- GENERATED — do not hand-edit. Regenerate: `python3 -m desk.verify feature-map --write`.
     Freshness is enforced by the eval gate's `verify_cli` section
     (`python3 -m desk.verify feature-map --check`). Derived from desk/eval/run_eval.py's
     section functions + the tenant registry. -->

Feature → files → eval section → verify command. The eval gate is the feature inventory:
every guarded behavior has a section, and `python3 -m desk.verify gate` runs every row.
`Eval section` reads `name · HARD|soft · fast|full` (fast = in the `--fast` PR gate).

## Core framework

| Feature | Files | Eval section | Verify |
|---|---|---|---|
| fees | `desk/sim/fees.py` | `fees` · HARD · fast | `python3 -m desk.verify gate` |
| engine | `desk/sim/engine.py`, `desk/sim/fill_source.py` | `engine` · HARD · fast | `python3 -m desk.verify gate` |
| HARD smoke: the backtest lane must emit desk/eval/runs/<id>/{run.json,trades.jsonl} | `desk/backtest/runner.py` | `run_dir` · HARD · fast | `python3 -m desk.verify gate` |
| HARD: the shipped example strategy priced+executed on a synthetic 2-snapshot store. | `desk/signals/example_calendar.py`, `desk/sim/engine.py`, `desk/sim/fees.py`, `desk/sim/fill_source.py` | `sample` · HARD · fast | `python3 -m desk.verify gate` |
| backtest | `desk/backtest/runner.py`, `desk/data/store.py`, `desk/signals/example_calendar.py` | `backtest` · soft · full | `python3 -m desk.verify gate` |
| HARD regression: on a synthetic WIDE book, net execution must be much worse than | `desk/sim/engine.py`, `desk/sim/fill_model.py`, `desk/sim/fill_source.py` | `wide_book` · HARD · fast | `python3 -m desk.verify gate` |
| HARD guard: every process-supervisor unit shipped in this repo launches COMMITTED code from THIS | — | `supervisor` · HARD · fast | `python3 -m desk.verify gate` |
| GATING once real overlap exists — the one invariant that catches backtest!=live execution drift. | `desk/eval/recon/golden_replay.py` | `recon` · HARD · full | `python3 -m desk.verify gate` |
| HARD guard: pre-registration integrity. Every strategy_config row whose base is available | `desk/signals/registry.py` | `registry` · HARD · fast | `python3 -m desk.verify tenants` |
| HARD guard (pre-flip-generality P1): date-discovery slug enumeration. Tenant | `desk/data/capture_select.py` | `discovery_hook` · HARD · fast | `python3 -m desk.verify gate` |
| HARD guard: live-fill reconciliation + maker cancel/expiry — the plumbing that must exist before any | `desk/driver/strategy_driver.py`, `desk/live/clob_adapter.py`, `desk/tracker/pnl_ledger.py` | `recon_lifecycle` · HARD · fast | `python3 -m desk.verify gate` |
| HARD guard: store.universe()'s n_snaps must equal the true row count AFTER hour-*.parquet compaction. | `desk/data/capture_validate.py`, `desk/data/store.py` | `compaction_recount` · HARD · fast | `python3 -m desk.verify gate` |
| HARD guard on the incremental compactor (desk/data/compactor.py) — the properties that make | `desk/data/capture_validate.py`, `desk/data/compactor.py` | `compactor_safety` · HARD · fast | `python3 -m desk.verify gate` |
| HARD regression: the T0 settlement chain against REAL Gamma shapes (the class that silently | `desk/data/capture_select.py`, `desk/data/market_feed.py`, `desk/data/sources.py`, `desk/data/store.py` | `settlement_chain` · HARD · fast | `python3 -m desk.verify gate` |
| HARD guard (control plane v2 §1): the driver re-reads the registry at the top of every loop | `desk/driver/strategy_driver.py`, `desk/signals/registry.py` | `driver_reload` · HARD · fast | `python3 -m desk.verify tick --dry-run` |
| HARD guard (control plane v2 §2): the exit monitor sweeps EVERY on-disk positions_*.json | `desk/driver/exit_monitor.py`, `desk/driver/strategy_driver.py`, `desk/signals/registry.py` | `exitmon_sweep` · HARD · fast | `python3 -m desk.verify tick --dry-run` |
| HARD guard (control plane v2 §3-5 / mission-control-v1 stage A): the dashboard control API. | `desk/control_plane.py`, `desk/dashboard_server.py`, `desk/driver/strategy_driver.py`, `desk/signals/registry.py` | `control_api` · HARD · fast | `python3 -m desk.verify health` |
| HARD guard (mission-control-v1 stage C): the red-alert notifier. Pure condition logic on | `desk/glossary.py`, `desk/notifier.py` | `notifier` · HARD · fast | `python3 -m desk.verify health` |
| HARD guard (framework): the generic TP/SL exit builder's sign/side matrix — long TP/SL -> SELL, | `desk/driver/exit_rules.py` | `exit_rules` · HARD · fast | `python3 -m desk.verify gate` |
| HARD guard (framework): MAX_DAILY_USD is a REAL enforced ledger, not an .env promise. | `desk/live/executor.py` | `daily_cap` · HARD · fast | `python3 -m desk.verify gate` |
| The /ops health page generator (.claude/skills/desk-dashboard/scripts/build_ops.py) must import | `desk/control_plane.py`, `desk/glossary.py` | `ops_page` · HARD · fast | `python3 -m desk.verify health` |
| HARD guard (MCP v1): the desk's MCP server — the AI-native surface must stay EXACTLY the | `desk/mcp_server.py` | `mcp_server` · HARD · fast | `python3 -m desk.verify health` |
| HARD guard (harness Phase 0): the agent-facing verification CLI (desk/verify.py) — the one | `desk/verify.py` | `verify_cli` · HARD · fast | `python3 -m desk.verify feature-map --check` |

<!-- tenants:begin -->

## Tenants (discovered at generation time)

A tenant's gate sections run inside `python3 -m desk.verify gate`, labeled
`<tenant>:<section>`; `python3 -m desk.verify tenants` shows discovery + seal status.

| Feature | Files | Eval section(s) | Verify |
|---|---|---|---|
| `sample_calendar` v2 | `strategies/sample_calendar/` | `sample_calendar:*` via `sample_calendar.eval_checks:sections` | `python3 -m desk.verify gate` |
| `sample_weather` v2 | `strategies/sample_weather/` | `sample_weather:*` via `sample_weather.eval_checks:sections` | `python3 -m desk.verify gate` |

<!-- tenants:end -->
