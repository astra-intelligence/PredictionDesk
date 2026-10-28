# Signal ideas — a backtest-ready spec for the desk framework

A **signal** isn't a single trade — it's a *repeatable rule* with an edge, to be backtested on historic
data before any money. The scanner's job is to propose good signal hypotheses and emit a structured
`backtest_spec` for each. The user implements the `strategy.py` and runs it through their backtester.

## How signals run in this project (so the spec maps onto it)
A strategy lives at `strategies/<name>/` (a tenant package with a manifest.json) and exposes one function (ref impl: `strategies/sample_weather/strategy.py`):
```python
def generate_orders(store, *, as_of_ts, params, positions, out_signals=None, **kw) -> List[Order]:
    # read markets via store.universe()/book_at()/resolution(); decide; return timestamped Order[]
```
- It's run in **backtest** (`desk/backtest/runner.run`) and live with the same code, swapping the fill source.
- `mark_mode`: `"resolution"` (settle to $1/$0 — for hold-to-resolution signals) or `"market"` (mark to
  latest mid — for rebalancing signals).
- Sizing is typically **fractional-Kelly** off an edge estimate; config lives in `strategy_config.json`.
- Per the project's principle, prefer **LLM judgment** in a "gate" (see `desk/signals/gate.py`)
  over hard formulas where the call is qualitative.

## The `backtest_spec` the scanner emits (one per signal idea)
```json
{
  "hypothesis": "one sentence: the edge / the counterparty's mistake",
  "market_selection": { "tags": ["..."], "yes_price": "<0.08", "dte_days": "<=7", "min_liq": 3000 },
  "entry_rule": "concrete trigger, e.g. 'buy NO at ask when YES price < 0.08 and dte <= 7'",
  "sizing_rule": "fractional-Kelly 0.25 off edge, cap 5% bankroll  (or 'fixed $X')",
  "features": ["the inputs to compute: yes_price, dte_days, tag, realized_yes_rate, spread, ..."],
  "mark_mode": "resolution | market",
  "exit": "hold_to_resolution  (or tp_capture 0.7 / model_stop)",
  "why_it_works": "the persistent reason the edge exists (bias, structure, latency)",
  "failure_modes": ["what kills it: tail events, fees, thin books, regime change"],
  "suggested_params": { "edge_threshold": 0.03, "min_price": 0.02 }
}
```
Map to implementation: `market_selection`+`entry_rule`+`sizing_rule` → the body of `generate_orders`;
`features` → what to read from `store`; `why_it_works`/`failure_modes`/`exit` → the `playbook.md`;
`suggested_params` → the `strategy_config.json` entry. Point the user to `strategies/sample_weather/` as the
working template (`strategy.py`, `playbook.md`, `gate.py`, config row).

## What makes a GOOD signal idea (the bar)
- **Repeatable & selectable:** the entry must be a rule over a *filterable* set of markets, not one event.
- **A named, persistent edge:** favorite-longshot bias, near-resolution exit-pressure, model-vs-market on a
  forecastable quantity, latency around data releases. Say *why it should persist*.
- **Backtestable from data we have:** prefer features computable from captured books + resolutions
  (`store`), so the user's framework can actually measure it. Flag if it needs data we don't capture.
- **Honest failure modes:** every signal has them — list them so the backtest knows what to stress.

## Example specs
1. **Longshot fade** — `{hypothesis:"<0.08-YES outcomes resolving <=7d resolve YES less often than priced",
   market_selection:{yes_price:"<0.08",dte_days:"<=7",min_liq:3000}, entry_rule:"buy NO at ask",
   sizing_rule:"fractional-Kelly 0.25", mark_mode:"resolution", why_it_works:"favorite-longshot bias",
   failure_modes:["tail surprise","fee drag on cheap NO"]}`.
2. **Near-resolution bond capture** — buy near-certain YES at a discount in the last N days (exit-pressure);
   `market_selection:{yes_price:"0.90..0.985",dte_days:"<=3"}`, `mark_mode:"resolution"`, why:"holders
   exit early to free capital", failure_modes:["the residual 1-3% sometimes hits","resolution disputes"].
3. **Weather tail-fade** (already built) — fade temperature buckets the multi-model ensemble says are
   overpriced; see `strategies/sample_weather/` for the full implementation as a pattern.
