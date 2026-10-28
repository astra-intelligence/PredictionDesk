---
name: desk-dashboard
description: Build a self-contained interactive HTML dashboard SITE of the desk's strategy runs — three pages (backtest ablation, forward paper test, live) with ONE information hierarchy each (overview scorecard → per-config/run → trades → click-to-expand per-trade pricing decomp: each component model → cross-model central → calibration → fair vs market → edge & side, plus a live Polymarket link). One renderer, ONE adapter (`ledger.py --source backtest|paper|live`) reading the ONE trade-event log (desk/tracker/trades.jsonl) + backtest run dirs (desk/eval/runs/). Use when asked to visualize/dashboard a forward run, a backtest ablation, or the live lane, refresh the dashboard, explain a strategy's trades or pricing, or build the whole site.
---

# Desk dashboard

One generic renderer + **ONE adapter**. The adapter reads the ONE trade-event log and produces a
normalized **SPEC**; `render.py` bakes it into `template.html` → a single self-contained page
(no server, no build step). `build_dashboard.py` orchestrates the three pages + an index.

```
scripts/
  template.html              # data-driven UI (overview + deep dive + expandable per-trade pricing)
  render.py                  # render(spec, out_path) -> self-contained html
  build_dashboard.py         # 3 pages (backtest/forward/live) + index.html, headline numbers
  adapters/
    ledger.py                # THE trade adapter: --source backtest|paper|live  → SPEC → render
    CONTRACT.md              # adapter contracts (trade adapter + pluggable opportunities adapter)
    sample_weather.py        # reference opportunities adapter (the shipped sample tenant's screen)
```

The 4th page ("current opportunities") comes from a pluggable, tenant-specific OPPORTUNITIES adapter
discovered at run time (env `DESK_OPP_ADAPTER`, else the first module in `adapters/` exposing
`PAGE` + `scan`) — contract and payload schema in `adapters/CONTRACT.md`. No adapter ⇒ the page is
skipped gracefully.

## The ONE data flow

Backtest ≡ forward ≡ live run through the SAME engine and land in ONE event log. `tracker/pnl_ledger.py`
writes:
- `tracker/ledger.json` — cross-run aggregates.
- `tracker/trades.jsonl` — **append-only TradeRow events**, one row per order event
  (`open` | `resting` | `closed`), for paper·live. Backtests write the SAME rows to
  `desk/eval/runs/<run_id>/trades.jsonl` (+ a `run.json` RunMeta); the ablation grid = many run dirs.

**decomp + slug are logged AT TRADE TIME** (Gamma drops resolved markets, so a closed trade can't be
repriced later). Every row carries `config_hash` / `code_ref` / env snapshot → a contaminated span (a run
made under a behavior flag) is machine-identifiable. **No renderer or adapter ever touches the network.**

## Build the site

Run from the worktree that owns the data (so relative paths resolve to `desk/tracker` + `desk/eval/runs`):

```
cd /path/to/repo
python3 .claude/skills/desk-dashboard/scripts/build_dashboard.py --out-dir dashboard/
open dashboard/index.html
```

Or one page at a time:

```
python3 .../scripts/adapters/ledger.py --source backtest --grid --out backtest.html
python3 .../scripts/adapters/ledger.py --source backtest --run <RUN_ID> --out one_run.html
python3 .../scripts/adapters/ledger.py --source backtest --strategy calendar --out calendar_bt.html
python3 .../scripts/adapters/ledger.py --source paper --out forward.html
python3 .../scripts/adapters/ledger.py --source live  --out live.html
```

- `--source backtest` reads `desk/eval/runs/<run_id>/` — `--grid` = all run dirs (each a scorecard row),
  `--run RUN_ID` = one, `--strategy NAME` = filter by RunMeta.strategy.
- `--source paper|live` reads `desk/tracker/trades.jsonl` filtered by `source`.
- `--source live` additionally reads `desk/eval/recon/findings/sim_vs_live_report.json` (per-order
  sim-vs-live deviation), `desk/driver/_heartbeat` (staleness), and the scoped `PASS.json` (gate scope) —
  surfaced as a banner. Not live-updating; re-run to refresh.

## Footer / banner (surfaced via `meta`, no template rewrite)

- **footnote** (all pages) ← RunMeta `code_ref` · `config_hash` · env snapshot · settle-source counts (GAMMA vs CLOB).
- **banner** (live page) ← heartbeat/staleness pill · gate PASS scope stamp · mean sim-vs-live `|deviation|` bps
  (green ≤15 / amber ≤40 / red — the recon gate). The template renders `meta.banner` (HTML) into `#banner`;
  empty/absent on the other pages.

## Per-trade pricing (how it's sourced)

If a strategy logs a pricing **decomposition** into the TradeRow at trade time, the dashboard renders it:
per-component model outputs, the authoritative central, calibration (bias offset, residual σ, rolling
error-sample count) and the fair(s), plus the outcome. The dashboard reads this straight from the row — so
it works even for **closed** (resolved, Gamma-dropped) trades. No backfill, no network. The shipped sample
logs no decomp; the template shows "pricing not captured" and everything else renders.

## Human legibility (levers · decomp · assumptions · sanity-check link)

The deep dive is built so a human can read each strategy and understand exactly what it's doing and where it
could be wrong, then sanity-check it against the live market:

- **Per-strategy LEVERS panel** — the adapter expands each strategy's config into a labeled glossary:
  the **ensemble** (engine → its component models + `central` statistic + error `dist`, read from an optional
  `desk.signals.pricing.ENGINES` registry if the strategy defines one), entry threshold, lead window, regime,
  edge mode/cap, metric, exec mode, calibration frame, qty/budget — plus run-of-record provenance (config
  hash, code ref, env, **cost-band** opt/base/adv, queue model, settle source). Backtest reads `run.json`'s
  `config`; paper/live reads the matching `strategy_config.json` row's `params` (matched by strategy name).
  See `_config_levers`.
- **Per-trade DECOMP** — component models → cross-model central (+statistic) → calibration (offset, σ,
  n_errors) → fair vs market mid/entry → edge & side. Rendered by `pricingDetail`; shows "not captured" when
  `decomp` is null.
- **Per-trade EXECUTION ASSUMPTIONS** (`assumptionsCard`) — plain-language fill basis:
  **T3–T5 = "filled by walking the REAL captured order book"** (exact) vs **T1 = "mid price + MODELED
  spread/slippage"** (cost model). Plus `cost_est_c` (¢/sh vs mid), `slippage_bps_vs_mid`, fees,
  `est_confidence`, and a `degraded` flag. These come from TradeRow `fidelity_tier`/`degraded` +
  `extras.{cost_est_c,slippage_bps_vs_mid,est_confidence}` (emitted by `pnl_ledger._emit_open_rows`).
- **Sanity-check link** — each trade row shows slug + subject + target date + a real
  `polymarket.com/market/<slug>` URL. A strategy SHOULD emit the real slug (not the hex condition id) in its
  signals; a run may also write a run-dir `signals.json` sidecar (order_id → slug/subject/target_date/lead)
  the adapter merges onto rows, since `pnl_ledger` forwards `lead/engine/gap` into extras but not
  subject/date.

## Normalized SPEC schema (the ONE contract render.py/template.html consume)

```jsonc
{
  "meta": { "title": "...", "subtitle": "...", "source_kind": "forward|backtest|live",
            "as_of": "...", "built": "...", "n_cycles": 0, "footnote": "...",
            "banner": "..." },              // banner = live-page heartbeat/gate/deviation HTML (optional)
  "_edge_cents": { "<strategy_name>": 10 },     // edge in cents per strategy (for the "would trade" note)
  "strategies": [{
    "name": "calendar", "exec": "taker|maker|marketable", "fair": "<engine>|...",
    "lead": [0,5], "edge": 0.10, "cities": "all", "is_maker": false, "desc": "...",
    "start": "<YYYY-MM-DD HH:MM> <tz label>",   // rendered in env DESK_TZ (default UTC)
    // headline metrics (overview row + deep-dive tiles):
    "realized": 0.0, "mtm": -40.89, "overall": -40.89, "return_pct": -41.0,
    "trades_n": 87, "win_pct": null, "avg_cap": 99.7, "peak_cap": 99.8, "budget": 100,
    "n_settled": 0, "n_open": 87, "n_resting": 0,
    "maker_placed": 0, "maker_filled": 0, "maker_fill_pct": null, "maker_fill_val_pct": null,
    "cap_trail": [/* per-cycle capital, optional */],
    "levers": [["Execution","taker","..."], ["Config hash","abc123…","..."], ...],   // [label, value, description]
    "trades": [{
      "status": "open|resting|closed", "side": "BUY|SELL", "qty": 10, "entry_px": 0.95,
      "fair": 0.03, "mid_entry": 0.96, "live_mid": 0.97,
      "unrealized": -0.25, "realized": null,            // closed -> realized; open -> unrealized
      "queue_ahead": 0, "lead": 1, "city": "<subject label>", "target_date": "<YYYY-MM-DD>",
      "entry_ts": 1782800000, "url": "https://polymarket.com/market/<slug>",  // REAL slug, not hex cond
      "fair_model": "<engine>",
      // execution ASSUMPTIONS (what data entered the trade + assumed cost/slippage) — rendered by assumptionsCard:
      "fidelity_tier": "T1",         // T3–T5 = real captured-ladder walk (exact); T1 = mid + MODELED spread/slippage
      "degraded": false, "cost_est_c": 19.5, "slippage_bps_vs_mid": 42.0,
      "est_confidence": "estimated",  // exact | estimated | low | none
      "fees": 0.0078,
      "decomp": {                                       // null if pricing wasn't captured (the sample logs none)
        "models": [{"model":"<component>","value_c":31.6}, ...],
        "central_c": 31.87, "central_method": "mean", "metric": "<variable>",
        "bucket_lo": 33.5, "bucket_hi": 34.5,
        "gauge": {"dist":"empirical","lead":1,"offset":-1.86,"sigma":1.05,"n_errors":26,"fair":0.4231},
        "gbt":   {"central_c": 31.87, "fair": 0.0301}
      }
    }]
  }]
}
```

Field notes: anything optional renders gracefully when absent (no `cap_trail` → no sparkline; `is_maker:false`
→ taker tiles; `decomp:null` → "pricing not captured"; `meta.banner` absent → no banner). Coloring:
positive=green, negative=red. Timestamps are unix seconds, displayed in env `DESK_TZ` (IANA name, default UTC; `meta.tz` carries it to the page). `runs` (per-config drill) and
`extra_cols` (overview) are also honored — see the template.

## Adding a new strategy = zero dashboard work

Because every strategy fires through the same driver → runner/executor → ledger, its trades land in
`trades.jsonl` (paper/live) or a run dir (backtest) automatically. `ledger.py` picks them up by `strategy`
name — no new adapter, no template change.

## TradeRow / RunMeta schema (desk/types.py)

- **TradeRow** (`tracker/trades.jsonl`): `run_id, source, strategy, config_hash, order_id, market_id,
  token_id, slug, url, ts, side, qty, price, limit_price, mode, status, fees, edge, fair, mid_entry,
  live_mid, pnl_realized, pnl_mtm, queue_ahead, fidelity_tier, degraded, decomp, extras`.
  `PnLLedger.log_run(result, as_of, run_meta, signals)` emits one open/resting row per ExecReport;
  `log_settle(..., market_id, token_id, extras)` emits the closed row.
- **RunMeta** (`desk/eval/runs/<run_id>/run.json`): `run_id, source, strategy, config, config_hash,
  starting_cash, budget, window, cost_band, code_ref, env, gate, settle_source_counts, created_ts`.
  Written by `backtest/runner.run` when `RunConfig.run_id` is set.
