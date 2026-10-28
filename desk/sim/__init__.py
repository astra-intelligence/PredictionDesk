"""desk/sim — the SHARED execution engine (used by BACKTEST and LIVE, despite the name).

This is not "simulation-only" code. `engine.execute(Order, FillSource) → ExecReport` is the
single order path for both paper backtests (`SimFillSource`, walk the recorded book) and real
trading (`LiveFillSource`, hit the CLOB) — so backtest ≡ live by construction. fees / accounting
/ report shape are identical; the ONLY sim-vs-reality seam is the `FillSource`.
"""
