"""desk/tracker — persistent cross-run P&L ledger (the durable record).

`pnl_ledger` accumulates `BacktestResult`s across runs/executions into a `LedgerSnapshot`
(realized/unrealized/fees, attribution by strategy). The persistence layer that `reports/` reads;
distinct from the per-run `sim/portfolio` (which lives only for one run).
"""
