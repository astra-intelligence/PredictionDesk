# desk/ — the framework

One order-driven engine runs backtest and live identically: `execute(Order, FillSource) → ExecReport`. Swap the sim fill source (walk the recorded book) for the live one (real CLOB) — sim ≡ live by construction. A reconciliation evaluator pins the engine to reality before any real money.

Design + component map: `../docs/architecture.md`. Quickstart + data regeneration: `../README.md`.

```bash
make eval-fast   # golden gate (offline, seconds)
make backtest    # sample strategy through the paper engine
```
