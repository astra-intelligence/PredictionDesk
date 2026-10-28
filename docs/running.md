# Running PredictionDesk

The operating manual. The README covers the 2-minute demo; this covers the real thing.
The agent-readable system contract (layout, plugin API, refusal semantics) is [`AGENTS.md`](../AGENTS.md).

## Bring the desk up (dev, foreground)

```bash
pip install -r requirements.txt
make eval-fast                                  # the offline golden gate: run it first, always
bash scripts/run_capture.sh                     # start recording live order books + trades
python3 -m desk.driver.strategy_driver --once   # fire enabled strategies once (paper)
python3 -m desk.dashboard_server                # dashboard at http://localhost:8788
```

`make run` starts capture + driver + dashboard + exit monitor together in the foreground
(Ctrl-C stops everything). `make bootstrap` runs every tenant's manifest bootstrap modules
on a fresh machine.

## Durable deploy

systemd user units live in `scripts/systemd/` (Linux); macOS launchd plists are bundled in
`scripts/` too. `make status` shows unit state, driver heartbeat, job locks and capture
freshness; `make daemons-stop` is the ops emergency stop.

## Collect your own data

No market data ships in this repo beyond the 1 MB demo sample. `bash scripts/run_capture.sh`
records order books and trades continuously; the default `desk/capture_config.json` captures a
liquid-markets lane, which is all the default-enabled `sample_calendar` strategy needs. The
`sample_weather` strategy ships disabled because the default lane does not record the daily
temperature ladders it trades: copy the `_example_weather_lane` block in
`desk/capture_config.json` into `lanes`, replace the placeholder `tag_ids` with the real weather
tag id (browse https://gamma-api.polymarket.com/tags), restart capture, then set the
`sample_weather` row to `enabled: true` in `desk/signals/strategy_config.json`. Every
day you do not record is a day you can never backtest honestly; capture runs as its own
process precisely so nothing else can interrupt it.

Everything downstream (backtests, reconciliations, dashboards) builds from your own capture.

## The eval suite

| Command | What it is |
|---|---|
| `make eval-fast` | every HARD guard, offline, seconds: the PR gate; CI runs exactly this |
| `make eval` | the full suite: golden replay + paper backtest + reconciliation + supervisor/registry guards |
| `make eval-core` | desk-only acceptance: `strategies/` moved aside, the framework must still pass its gate and drive paper trades |

Tenants extend the gate through their manifest's `eval_checks` hook: a present-but-broken
hook fails the gate; an absent tenant is skipped *with the reason printed*.

## Live trading (locked by default)

`desk/live/executor.py` refuses every order unless **both** hold:

1. `LIVE_TRADING_ENABLED=true` in the environment (`.env.example` documents every knob), and
2. a reconciliation certificate at `desk/eval/recon/PASS.json`, written only by the
   reconciliation evaluator after replaying real fills through the simulator. It names the
   strategy, execution mode and market class it certifies, and it expires.

Neither exists in a fresh clone. On top of the gate: a geo guard, a per-market concentration
cap (`LIVE_MAX_PER_MARKET_USD`), a per-event cap, and an enforced daily spend ledger
(`MAX_DAILY_USD`). Everything fails closed.

## Runtime data layout

Runtime paths resolve through `desk/runtime/paths.py`: anchored to the checkout by default,
relocatable with `DESK_DATA_DIR=<abs dir>` (re-roots `data/capture`, `driver/`, `tracker/`,
`reports/out` under one directory). Rendered timestamps use `DESK_TZ` (IANA name, default UTC).

## AI agent skills (`.claude/skills/`)

LLM agent procedures that are part of the system's design, each a folder with a `SKILL.md`
plus plain-Python scripts, readable and runnable without any AI tooling.

| Skill | What it's for |
|---|---|
| `arb-scanner` | scans the whole market for opportunity families (complement pairs, negRisk baskets, bond-like carry, MM rewards, directional screens) and adversarially fact-checks every candidate |
| `desk-dashboard` | builds the 4-page HTML dashboard from the run stores; `desk/dashboard_server.py` serves and refreshes it; its validator fails the build on unexplained data gaps |
| `recon-evaluator` | the only thing allowed to write a live-trading unlock: replays real fills through the sim, diagnoses each deviation as bug vs physical residual, grants a scoped, expiring pass |
| `strategy-reporter` | per-run narrative reports under `desk/reports/` from a run's trades + config |
