"""desk/reports — the output/presentation layer (read-only over results).

Turns a run's `ExecReport[]` / `BacktestResult` into human artifacts: `run_report` (per-run
scaffold + anomaly flags → md + json; PDF via the strategy-reporter skill from the md).
HTML is the dashboard's job (desk-dashboard skill, `ledger` adapter). Reads the persisted
ledger in `desk/tracker`; never executes orders.
"""
