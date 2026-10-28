# desk/data — capture + store

**Capture** (`capture.py`) continuously records live Polymarket order books and trades to dated parquet under `capture/` (gitignored — you collect your own). Start with `bash scripts/run_capture.sh` or the launchd plist; `capture_monitor.py` health-checks it; `capture_validate.py` verifies freshness/coverage.

**Store** (`store.py`) is the one accessor the rest of the system uses: as-of(t) books/mids/resolutions with a fidelity router (own captured book > mid-price series > resolution-only). Stale or fallback data is flagged `degraded`, never served silently.

Why forward capture exists: the public APIs expose price history but no order-book depth history — without the book, realistic fills, spread, and slippage are unknowable, and backtests would be fiction.
