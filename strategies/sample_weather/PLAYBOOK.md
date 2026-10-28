# sample_weather — playbook

**What it trades.** Polymarket's daily temperature ladders ("highest/lowest temperature in \<city\>
on \<date\>"), for the ~12 cities in `cities.py`, 1–5 days ahead.

**The model, honestly.** One Open-Meteo model (`gfs_global` by default) gives a point forecast for
the city's daily max/min. A gaussian with a FIXED hand-set per-city σ turns that into probability
mass per 1°C bucket. Fair(token) = that mass (or its complement for the No side). Entry = IOC taker
BUY when fair − ask ≥ `entry_threshold` (0.2 by default — deliberately strict, so it fires rarely).

**Why those numbers claim nothing.** σ is not calibrated, the coordinates are city centers rather
than the settlement station, the gaussian is assumed, and one model is taken at face value. Each of
those gaps is exactly where a real weather strategy does its work. Expect the fills this strategy
does take to be adversely selected.

**What it demonstrates.** The full tenant contract: manifest discovery, a sealed config row, driver
context (`positions`, `universe_rows`, `out_signals` with per-trade decomp), the per-event exposure
hook (`event_slug_of`), tenant eval sections in the gate, and dashboard rendering with a real
pricing breakdown per trade.

**Runbook.** It needs the capture daemon's `_universe.json` + captured books (run
`bash scripts/run_capture.sh` for a few minutes first), and network access to Open-Meteo + Gamma at
fire time. `python3 -m desk.driver.strategy_driver --once` fires it in paper mode.
