# Prediction-market trading system — design overview

## What we're building

A system for trading prediction markets on Polymarket: find pricing models that are genuinely profitable — starting with an ensemble of forecasting models whose fair value we compare against market prices — and trade the gaps.

We start **manual**: the system finds and shows opportunities, proves them out in backtests and paper trading, and a human places the trades. A model that demonstrates real profit graduates to automated trading, behind explicit safety checks.

The design is shaped by one hostile fact and one discipline:

- **The hostile fact:** Polymarket's APIs expose price *history* but no order-book *depth* history. Prediction markets are thin — the cost of actually getting filled (spread, depth, slippage) is often bigger than the edge. Without the book, that cost is unknowable, and any backtest is fiction.
- **The discipline:** a backtest must be a claim about what the live system would have done — not about a friendlier imaginary market. Every component below exists to defend that claim against a specific way backtests lie.

## The system

```
                     Polymarket (live prices, order books, resolutions)
                          │                                ▲
                          │ record                         │ real orders
                          v                                │ (locked until proven)
              ┌───────────────────────┐          ┌─────────┴───────────┐
              │ DATA CAPTURE          │          │ EXECUTION ENGINE     │
              │ forward-capture of    │          │ one engine, two      │
              │ live order books +    │          │ modes:               │
              │ trades, continuously  │          │  · simulated fills   │
              └──────────┬────────────┘          │    (backtest/paper)  │
                         v                       │  · real orders       │
              ┌───────────────────────┐   read   │ same rules, fees,    │
              │ DATA ACCESS           │←──────┐  │ and assumptions      │
              │ "the market as of     │       │  └─────────▲───────────┘
              │ time T" + estimated   │       │            │ buy/sell requests
              │ spread when no        │       │            │
              │ capture exists        │  ┌────┴────────────┴─────────┐
              └───────────────────────┘  │ STRATEGIES                │
                                         │ pure business logic:      │
              ┌───────────────────────┐  │ "given the data, what is  │
              │ SCHEDULER             │─>│ mispriced, and what would │
              │ runs each strategy    │  │ I buy or sell?"           │
              │ on its own clock      │  └────────────┬──────────────┘
              └───────────────────────┘               │ every decision + trade
                                                      v
              ┌─────────────────────────────────────────────────────────┐
              │ LEDGER — permanent record: price, fees, fair value, why │
              └────────────────────────────┬────────────────────────────┘
                                           v
              ┌─────────────────────────────────────────────────────────┐
              │ DASHBOARD — live · backtest · paper trade · opportunities│
              └────────────────────────────┬────────────────────────────┘
                                           v
                              human reviews & trades manually (phase 1)
```

## Component design

### Data capture
Continuously records live order books and trades, starting day one. **Design driver:** depth history cannot be bought later — every uncaptured day is a day we can never backtest honestly. So capture is its own always-on process, independent of everything else (a strategy bug must never stop the recording), with health monitoring, and its output is the ground truth every other component builds on.

### Data access
One interface the whole system uses to ask for "the market as of time T." Three design decisions carry the honesty burden:

- **As-of semantics everywhere.** The single worst backtest bug is peeking at the future — a calibration window that includes the traded day, a forecast used minutes before publication. It reliably manufactures fake profit. Making "you can only see what existed at T" a property of the *data layer* — rather than a convention each strategy must remember — kills the whole bug class.
- **A cost estimate is never free.** Where no captured book exists, we fall back to price history plus a **spread estimate** learned from books we *do* observe in comparable markets (similar market type, similar price level). The estimate is never zero: assuming an unrecorded trade was cheap, hundreds of times over, invents an edge. If there's no evidence to estimate from at all, the fill is skipped — fail closed, never guess cheap.
- **Degraded data arrives labeled.** Stale books, estimated spreads, and fallback sources are flagged on every response and rolled up per run, so "this backtest leaned on estimates" is visible in the result, not buried.

### Strategies
Pure business logic: read data, compute fair value, compare to price, emit buy/sell requests. Two design rules:

- **Strategies never touch the exchange or simulate fills.** All market mechanics live in the engine — so a strategy tested in backtest is *identical* to the one that trades, and a new idea (weather, rates, sports) is a plug-in module touching nothing else.
- **Configurations are frozen and fingerprinted before evaluation.** Test 50 variants and the best looks great by luck; the cure is pre-registration — a config is hashed when registered, the system refuses to run a tampered one, and there's no picking winners after the fact.

### Execution engine
One engine turns a buy/sell request into a fill, in either mode: simulated against recorded data, or real orders to Polymarket — same code, same rules. The simulation rules each answer a specific way backtests lie:

- **Fills walk the real book, level by level.** Filling at the midpoint is the classic lie: in reality 100 shares cost 45¢, the next 50 cost 47¢, and a wide book has nothing near mid. On markets where the edge is a few cents, this difference is the whole result.
- **Fees are the exchange's exact formula** — category- and price-dependent, big enough to erase thin edges — never approximated, never tuned to make results fit.
- **Stale and thin books are treated pessimistically.** A recorded book can be seconds old by decision time; a thin top level is assumed already gone.
- **Passive (maker) orders don't magically fill.** We count everything resting ahead of us at submission and fill only if enough real volume trades through the price.
- **Results are a bracket** — optimistic / base / adverse cost assumptions reported side by side. A strategy that only works in the optimistic column is visibly not working.

### Scheduler
Runs each strategy on its own clock (e.g. when new forecast data lands), routes orders to paper or — when unlocked — live, and settles positions **only on official market resolution**, never by assuming a last price was final. Runs unattended so paper trading accumulates a hands-off track record — that, not the backtest, is the evidence a strategy is real.

### Ledger
Every trade, simulated or real, is permanently recorded with price, fees, the model's fair value, and the decision context, written safely even when several processes trade at once. **Design driver:** auditability — any surprising number must be traceable to the exact data, config, and reasoning that produced it.

### Dashboard
Four pages: **live**, **backtest**, **paper trade**, **opportunities**. The first three share one layout on purpose — backtest, paper, and live results are only trustworthy if you can compare them side by side in the same shape. **Opportunities** is phase 1's product: what the models think is mispriced right now, for a human to trade.

### Safety checks
Real-money trading is off by default, and unlocking is evidence-based, not a switch: simulated fills must first be verified against small real test trades, and the unlock is scoped (which strategy, which market type, expiring). Statistical guards live here too — correlated same-day trades are clustered into one bet, and a placebo test (shuffle the model's signal) must destroy the profit; if it survives shuffling, it was never skill. Even then, the backtest only nominates — forward reality decides.

## How the pieces talk

One small, fixed contract end to end: strategies emit buy/sell requests → the engine returns fill reports → everything appends to the one ledger. That single path is what makes backtest = live true, strategies plug-in, and all three tracks (backtest / paper / live) directly comparable.

## Phasing

1. **Now — manual:** capture data, backtest candidate models, paper-trade the promising ones, trade the opportunities page by hand.
2. **Next — automated:** a strategy with a proven paper track record and verified execution accuracy places small real orders itself, behind the safety checks.
