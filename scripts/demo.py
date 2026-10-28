"""scripts/demo.py — the offline first-run demo (`make demo`).

Backtests the built-in toy calendar strategy (desk/signals/example_calendar.py) over the
bundled capture sample (data/sample/: one day of Polymarket daily-temperature markets,
real recorded order books + trades), once as a taker and once as a maker, through the
SAME engine live trading uses — real-ladder fills, exact fees, honest P&L bracket —
then builds the dashboard. No network, no keys, no setup beyond `pip install`.

The toy strategy claims NO edge (buy early / sell late); the point is what the machine
does with it: ladder-walk fills on thin books, fee math, cost decomposition per trade,
and an optimistic/base/adversarial bracket instead of a single flattering number.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from desk.data.store import Store                      # noqa: E402
from desk.signals.example_calendar import generate_orders  # noqa: E402
from desk.backtest.runner import run, RunConfig        # noqa: E402

SAMPLE = os.path.join(REPO, "data", "sample")


def _labels() -> dict:
    """(market_id, token_id) -> {slug} so dashboard rows read as market names, not hex ids."""
    try:
        rows = json.load(open(os.path.join(SAMPLE, "universe.json")))["tokens"]
        return {(r["market_id"], r["token_id"]): {"slug": r.get("slug")} for r in rows}
    except Exception:
        return {}


def main() -> None:
    capture = os.path.join(SAMPLE, "capture")
    if not os.path.isdir(capture):
        sys.exit("data/sample/capture missing — run from a full checkout")
    store = Store(capture_dir=capture)
    labels = _labels()

    for mode in ("taker", "maker"):
        orders = generate_orders(store, qty=25.0, n_markets=8, mode=mode)
        sig = {k: dict(v) for k, v in labels.items() if k in {(o.market_id, o.token_id) for o in orders}}
        res = run(orders, RunConfig(
            store=store, starting_cash=1000.0, run_id=f"sample-{mode}",
            source="backtest", strategy="calendar", mark_mode="market",
            config={"strategy": "calendar", "mode": mode, "qty": 25.0, "n_markets": 8,
                    "sample": "data/sample (2026-10-23 temperature ladders)"},
            signals=sig))
        print(json.dumps({"run_id": f"sample-{mode}", "orders": len(orders),
                          "fill_ratio": res.fill_ratio, "total_pnl": res.total_pnl,
                          "bracket": res.bracket,
                          "worst_spread_source": res.data_quality.get("worst_spread_source")}))

    build = os.path.join(REPO, ".claude", "skills", "desk-dashboard", "scripts", "build_dashboard.py")
    subprocess.run([sys.executable, build], cwd=REPO, check=True)
    print("\ndemo runs written to desk/eval/runs/sample-{taker,maker}/ — dashboard/ rebuilt.")
    print("serving next (make demo) or: python3 -m desk.dashboard_server  →  http://localhost:8788")


if __name__ == "__main__":
    main()
