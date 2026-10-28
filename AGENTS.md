# PredictionDesk — agent guide

PredictionDesk is an agent trading desk for prediction markets: a multi-strategy trading framework
for Polymarket. One execution
engine runs backtest, paper and live through the same code path, so a backtest result is a claim
about what the live system would have done. **A fresh clone is PAPER-ONLY**: live trading is
double-gated (`LIVE_TRADING_ENABLED` env + an evaluator-earned, scoped `desk/eval/recon/PASS.json`)
and neither gate is satisfied out of the box. Keep it that way until a PASS is genuinely earned.

## Layout

| Path | What lives there |
|---|---|
| `desk/` | The GENERIC system — framework only, zero tenant imports: types, sim engine, data capture + as-of store, registry (plugin discovery), backtest runner, driver + exit rules, live path (locked), eval gates, tracker, reports. Runs, imports and passes its core gate on a desk-only tree (`make eval-core` proves it) |
| `desk/signals/` | Strategy registry (plugin discovery + seals), hash-sealed config, the generic gate, and the builtin sample (`example_calendar.py`) |
| `strategies/` | Strategy plug-ins (tenants), discovered by the registry via each tenant's `manifest.json` — no symlinks, no desk-side imports. Two shipped samples (self-contained packages, relative imports): `strategies/sample_calendar/` (MINIMAL — zero external data, the shape to copy for a new domain) and `strategies/sample_weather/` (full external-data pipeline). Tenant modules run with the strategies dir on `sys.path`: `PYTHONPATH=strategies python3 -m sample_weather.strategy` |
| `scripts/` | run_* launchers, systemd units (`scripts/systemd/`), macOS plists |
| `.claude/skills/` | Agent procedures that are part of the system (arb-scanner, desk-dashboard, recon-evaluator, strategy-reporter) |

## How to run

```bash
pip install -r requirements.txt
make eval-fast                          # offline golden gate — the PR gate (core + tenant sections)
make eval-core                          # desk-only acceptance: strategies/ aside -> core gate + driver + exit monitor
make bootstrap                          # run every tenant's manifest bootstrap modules
python3 -m desk.signals.registry list   # discovered bases/tenants + enabled specs
bash scripts/run_capture.sh             # capture daemon (books + trades)
python3 -m desk.backtest.runner         # sample strategy through the paper engine
python3 -m desk.driver.strategy_driver --once   # one paper tick
python3 -m desk.dashboard_server        # dashboard at :8788
python3 -m desk.mcp_server              # stdio MCP server — Claude at the desk, read+pause only (docs/mcp.md)
python3 -m desk.verify gate             # agent verify CLI: gate | tenants | tick | health | feature-map (--json)
```

## Verification workflow (agents)

- `python3 -m desk.verify <gate|tenants|tick|health|feature-map>` is the agent-facing verification
  CLI: `--json` = exactly one machine-readable object per run; `tick --dry-run` previews what would
  fire; a real `tick` on an empty capture store is REFUSED (exit 3), never fake success. Read-only
  by design — no order placement, no config/seal writes, no control POSTs — enforced by the gate's
  `verify_cli` section.
- Feature map: [`docs/feature-map.md`](docs/feature-map.md) — feature → files → eval section →
  verify command. GENERATED (`python3 -m desk.verify feature-map --write`); never hand-edit, the
  gate fails on drift (`feature-map --check`).
- Pre-merge convention: no merge to main without `desk verify gate` (= `make eval-fast`) green in
  the merging worktree.

Runtime-data paths resolve through `desk/runtime/paths.py`: `__file__`-anchored to the checkout
(never cwd), overridable with `DESK_DATA_DIR=<abs dir>`, which re-roots the desk-relative layout
(`data/capture`, `driver/`, `tracker/`, `reports/out`, …) under one directory. Rendered timestamps
use env `DESK_TZ` (IANA name, default UTC).

## The plugin contract (tenant API)

A tenant = one directory under `strategies/` (env `DESK_STRATEGIES_DIR`) that is a self-contained
Python package plus a `manifest.json`. The registry puts the strategies dir on `sys.path` and imports
the tenant by its own top-level `module` name; the tenant may import desk framework APIs
(`desk.types`, `desk.data.store`, `desk.sim.*`, `desk.driver.exit_rules`, `desk.eval.api`,
`desk.data.central_sources`), the desk NEVER imports the tenant. Anything missing or broken is
LOUDLY skipped and its config rows refused — never a silent fallback.

**manifest.json**

```json
{"name": "<base spec name>", "module": "<top-level package>", "version": "<behavior version>",
 "entrypoint": "pkg.signals.strategy:generate_orders",
 "schedule": {"type": "release_times|interval", "...": "..."}, "enabled": false,
 "params": {"...default params (merged UNDER config-row params, covered by the seal)..."},
 "hooks": {"eval_checks": "pkg.eval_checks:sections",
           "resolvers": "pkg...:fn", "exit_order_builder": "pkg...:fn",
           "event_slug_of": "pkg...:fn", "discover_event_slugs": "pkg...:fn"},
 "bootstrap": ["pkg.weather.build_meta"],
 "playbook": "PLAYBOOK.md"}
```

**Entrypoint.** `generate_orders(store, **ctx) -> List[Order]`. The driver offers the full context and
passes ONLY what the function's signature accepts (a minimal strategy can be `generate_orders(store)`).
Offered kwargs: `as_of_ts` (fire time), `gate_fn` (threshold or LLM gate per `params["gate"]`),
`positions` ({(market_id, token_id): position dict}), `params` (the spec's effective params),
`out_signals` (dict the strategy fills — see below), `resting` (still-live maker-quote rows),
`universe_rows` (light Gamma-derived live select set; backtests omit it and the strategy falls back to
the full historical `store.universe()`), plus every config-row param splatted as its own kwarg.

**out_signals.** `{(market_id, token_id): {slug, fair, mid, edge, gap, engine, lead, city,
target_date, decomp}}` — joined onto TradeRows at log time (Gamma drops resolved markets; a closed
trade can't be repriced later). Magic key `"__n_views__"` (int): candidates PRICED this pass — the
driver pops it for fire observability; it is never a signal.

**Params the framework itself reads.** `live` (per-spec live routing under the PASS),
`exit_tp`/`exit_sl` (exit monitor TP/SL), `budget` + `min_notional` (live short→BUY-NO conversion
sizing), `gate` (`"llm"` → llm_gate), `mode`, `maker_validity_s`. Everything else is tenant-private.

**Hooks the framework consumes.** `eval_checks` — zero-arg callable returning `desk.eval.api.Section`
list; run by `run_eval` after the core gate, labeled `<tenant>:<section>` (absent tenants are skipped
WITH REASON; a present-but-broken hook fails the gate). `resolvers` — zero-arg → `(peer_key,
category_of)`; composed across tenants, feeds the paper/backtest cost model AND the live PASS
market-class scope check on both executor call sites (desk-only: single `"default"` class).
`exit_order_builder` — per-arm override of `desk.driver.exit_rules.tpsl_exit_order` (same signature).
`event_slug_of` — slug → event key for the per-event exposure cap (generic fallback: each market is
its own event). `discover_event_slugs` — `(window_days) -> [event_slug]` for the capture layer's
date-discovery lanes: the TENANT owns the slug grammar; the framework only fetches each slug's
event and applies lane filters. (`date_discovery_cities` — zero-arg → city list — is the
DEPRECATED alias: when a tenant declares only it, the framework wraps the cities through the
legacy daily-temperature template, loudly.)
`bootstrap` — module list run as `__main__` by `make bootstrap` / `registry bootstrap`.
`playbook` — tenant-relative path rendered into run reports. Dashboard opportunities adapters are NOT
a registry hook: they live with the desk-dashboard skill and import the tenant by its plugin name
(contract: `.claude/skills/desk-dashboard/scripts/adapters/CONTRACT.md`).

**Config row.** One row per arm in the strategy config: `{name, strategy: <base>, enabled, schedule,
params, config_hash}`. A row whose `name` is not a base becomes a VARIANT (own name/params, shared
entrypoint). Seal + reseal flow below.

## Strategy config: seal + reseal

Strategy rows live in `desk/signals/strategy_config.json` (override the path with
`DESK_STRATEGY_CONFIG`). Tenants are discovered from `strategies/` (`DESK_STRATEGIES_DIR`) via each
tenant's `manifest.json`. Every row must carry a `config_hash` seal covering the row's EFFECTIVE
behavior — sha256 over {name, strategy, effective schedule, {**base defaults, **row params}, base
version} (`registry.seal_hash`) — so editing a base's defaults or bumping its manifest `version`
breaks every dependent row's seal, never silently shifts sealed behavior. The loader recomputes the
seal and REFUSES mismatched or unsealed rows (fail-closed, loud); a row whose tenant is absent is
refused too (unverifiable ≠ tamper, equally refused). After ANY such change: run
`python3 -m desk.signals.registry reseal` (inspect), then `… reseal --write` to rewrite the seals,
and commit the config together with the change that caused it.

## Secrets

`.env` (chmod 600, gitignored) holds real keys — see `.env.example`. Never commit, print or copy it.
`LIVE_TRADING_ENABLED` stays `false` until a scoped PASS is genuinely earned. Paper/backtest need no
keys at all. Live safety caps: `MAX_ORDER_USD`, `LIVE_MAX_PER_MARKET_USD`, and the enforced
`MAX_DAILY_USD` daily spend ledger (fail-closed).
