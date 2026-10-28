# Reconciliation & calibration — the evaluator loop

This is the calibration process for the desk. Instead of fitting free parameters to
minimize average error (which hides bugs inside fudge factors), an **LLM evaluator
diagnoses the root cause of each sim-vs-reality gap, documents it, and we fix the logic.**
Fees are NEVER tuned.

## Inputs
- `golden_replay.py` → `findings/deviation_report.json` — engine fills vs **real fills**
  (captured `/trades` prints; fees by formula). Each case: `real_price`, `sim_avg_price`,
  `dev_bps`, `tier`, `degraded`, `stale_warn`.
- Later: our own paper→real orders, same shape, from a small live bootstrap.

## The evaluator's job (per run)
1. Read `deviation_report.json`. For every case with non-trivial `|dev_bps|`, determine
   **why** — classify each as **BUG** (fix in code) or **PHYSICAL residual** (estimate from
   evidence).
2. **Diagnose from evidence, not by fitting.** Each explanation must be grounded in
   observable data *independent of the fill it reproduces*:
   - **book staleness / latency** → compare `case.ts` to the as-of snapshot's
     `capture_ts` and the poll cadence; if the book moved between them, that's the gap.
   - **queue position** (maker) → volume traded *through* the limit after the quote
     posted (from `store.prints_between`).
   - **fee category / date** → check the market's Gamma category vs `fees.CATEGORY_RATES`
     and `FEE_START_TS`.
   - **multi-level walk / partial fill / tick rounding** → inspect the ladder vs `size`.
3. **Write a finding** to `findings/<date>-<slug>.md`: the case(s), the evidence, the
   verdict (BUG vs PHYSICAL), and the fix. For PHYSICAL residuals, propose an
   **evidence-grounded point estimate** per `(market, time)` (e.g. `latency_ms≈1500`,
   `queue_fill_prob≈0.4`) **with rationale**, plus an optimistic/pessimistic sensitivity
   bracket. These feed `fill_model.Assumptions`.
4. Hand findings to the human → the logic is fixed (or the assumptions loaded) →
   re-run `golden_replay` → deviation should drop *because the cause was removed*.

## Guardrail (what keeps this from becoming autotune)
Evidence **predicts** the fill; matching is **confirmation**, never the input. Never pick
"the number that makes this trade match." If a residual can't be explained from
independent evidence, **flag it** (don't invent a value). Fees are exact by formula; a
`fee_dev ≠ 0` is always a BUG.

## The PASS gate (unlocks real money)
Emit **PASS** only when, over a meaningful `n_fills`:
- `median_abs_dev_bps ≤ 15` and `p90_abs_dev_bps ≤ 40` (tighten as data densifies),
- fees exact by formula (no `fee_dev`),
- no open **BUG** findings; all remaining deviation is explained PHYSICAL residual
  carried as documented assumptions + bracket.

The PASS is **scoped**: `findings/PASS.json` names the `market_class` and strategies it
unlocks (shape below). Until a valid scoped PASS exists, `LIVE_TRADING_ENABLED` stays
`false`. Record before/after `deviation_bps` and `n_fills` each round — that trail is the
"backtest your backtester" evidence.

```jsonc
{ "scope": "taker",            // exec scope the PASS covers
  "market_class": "<class>",   // the resolver's market-class label this PASS is scoped to
  "strategies": ["<name>"],    // strategy ids unlocked
  "granted": "YYYY-MM-DD",
  "expiry":  "YYYY-MM-DD" }    // fail-closed on stale/undated
```

## Cadence
Run after each material engine change, and nightly during the live reconciliation window.
Each round: `golden_replay` → evaluator findings → fix → re-run → update the PASS status.
```
golden_replay → deviation_report.json → [evaluator: diagnose+document] → findings/*.md
     ↑                                                                        │
     └──────────────── fix logic / load assumptions ←── human feeds the loop ─┘
```
