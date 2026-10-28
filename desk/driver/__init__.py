"""desk/driver — the strategy scheduler/driver.

Fires enabled strategies on their schedule, feeds Order[] to runner (paper) or executor
(live, gated), logs to the persistent pnl_ledger, and persists per-strategy positions. Mirrors
the capture loop (heartbeat + process-supervisor restart).
"""
