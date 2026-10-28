"""sample_weather — the SHIPPED sample tenant and the tenant-authoring tutorial example.

One Open-Meteo model's point forecast → a gaussian with a fixed per-city σ → probability mass per
1°C temperature bucket → paper taker order when the executable edge clears a threshold. Deliberately
simple and HONESTLY uncalibrated (see strategy.py's docstring for every simplification): it exists so
a fresh clone has a strategy that genuinely prices live markets end-to-end — fire → engine → ledger →
dashboard — and so "write your own strategy" has a complete worked example. No edge is claimed.
"""
