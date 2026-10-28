# Dashboard adapter contracts

Two adapter kinds live here. Nothing else in the dashboard knows any strategy by name.

## 1. The trade adapter — `ledger.py` (generic, exactly one)

Reads the ONE trade-event log (`desk/tracker/trades.jsonl` + `desk/eval/runs/<id>/`) and emits the
normalized SPEC that `render.py`/`template.html` consume (schema in `../SKILL.md`). Every strategy's
trades land there through the same driver → engine → ledger path, so **a new tenant needs zero
trade-adapter work** — rows are grouped by `strategy` name; a tenant that logs `out_signals` (slug,
fair, edge, decomp) gets per-trade pricing breakdowns rendered automatically.

## 2. The opportunities adapter — tenant-specific, pluggable

The "current opportunities" page is a live pricing pass — inherently tenant knowledge. A tenant that
wants the page ships ONE module in this directory implementing:

| Attr | Required | Meaning |
|---|---|---|
| `scan() -> dict` | yes | live rescan: price the tenant's in-window cohort against the latest captured books; writes `OPP_CACHE`; returns the payload |
| `PAGE: str` | yes | self-contained HTML with a `/*__OPP__*/null` placeholder the builder replaces with the JSON payload |
| `OPP_CACHE: str` | yes | repo-root-relative cache path (convention: `dashboard/opportunities.json`) |
| `scan_cached(max_age_s) -> dict` | yes | cached payload when fresh, else `scan()` — the build path never burns a fresh rescan |
| `ensure_enriched(res) -> dict` | yes | upgrade a cached payload offline (no-op if rows are already complete) |
| `QUOTA_PROBE_URL: str` | no | URL the server probes before a live rescan; a body containing `limit exceeded` serves the cached scan stale instead |
| `QUOTA_RESET_HINT: str` | no | the stale-banner text when that probe trips |

**Payload** (consumed by your own `PAGE`, plus these keys the server/builder rely on):
`{"rows": [...], "n_in_window": int, "n_actionable": int, "as_of": unix_s, "decision_date":
"YYYY-MM-DD", "tz": "<IANA name>", "lanes": {...}}` — plus `stale`/`stale_reason`/`from_cache`
provenance flags the server may add. Timestamps render in env `DESK_TZ` (default UTC).

**Discovery.** `desk/dashboard_server.py` and `build_dashboard.py` pick the adapter at run time:
env `DESK_OPP_ADAPTER=<module name>` wins; otherwise the first module here (sorted, `ledger.py` and
`_*` excluded) exposing `PAGE` + `scan`. No adapter installed ⇒ the page is skipped and the API
endpoints answer with an explanatory empty payload — never a crash.

**Reference implementation:** `sample_weather.py` (the shipped sample tenant's screen): captured
universe → slug parse → lead window → one-model gaussian fair → latest book, one row per market,
rows over the shipped row's `entry_threshold` highlighted.

**Imports.** An opportunities adapter may import its tenant package (via
`desk.signals.registry.ensure_importable()`), desk framework APIs, and `adapters` helpers
(`desk_tz`, `desk_tz_name`, `tz_label`). It must fail at CALL time, not import time, when its
tenant is absent — the rest of the dashboard must keep building without it.
