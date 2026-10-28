# data/sample/: the bundled demo capture

One day (2026-10-23 UTC) of real recorded Polymarket market data for 48 daily-temperature
markets, exactly as `desk/data/capture.py` wrote it, compacted to one parquet per stream:

```
capture/2026-10-23/<market_id>/book_snapshots/day.parquet   # full L2 books, as captured
capture/2026-10-23/<market_id>/trades/day.parquet           # public prints
universe.json                                               # slug/question labels for these markets
```

~1 MB total. `make demo` backtests the built-in toy strategy over it, offline, no keys.

Notes:

- Books are byte-identical in content to the original capture. In the trades files the
  `wallet` and `tx_hash` columns are blanked: they are public on-chain data, but this repo
  does not ship third-party wallet addresses.
- Source: Polymarket's public CLOB/Gamma/Data APIs. This is market data, not advice;
  it ships only so a fresh clone can exercise the whole pipeline honestly.
- Your own capture (see `docs/running.md`) replaces this the moment you start recording.
