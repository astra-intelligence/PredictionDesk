"""eval/recon/golden_replay.py — pin the engine to reality (the calibration keystone).

For each REAL fill (captured `/trades` prints), reconstruct an equivalent taker order, run
the SAME engine over the recorded book@t, and diff engine avg_fill_price vs the real price
-> deviation_bps. Writes a deviation report the evaluator reasons over; it does NOT fit
anything. Emits the data; the evaluator (skills.md) explains gaps -> we fix logic.
"""
from __future__ import annotations

import json
import os
import statistics
from typing import List, Optional

from desk.types import Order, OnchainFill
from desk.sim.engine import execute
from desk.sim.fill_source import SimFillSource
from desk.data.store import Store

FINDINGS_DIR = os.path.join(os.path.dirname(__file__), "findings")
REPORT = os.path.join(FINDINGS_DIR, "deviation_report.json")


def run(max_tokens: int = 120, max_fills_per_token: int = 50, target_fills: int = 80,
        store: Optional[Store] = None) -> dict:
    store = store or Store()
    universe = sorted(store.universe(), key=lambda r: -r[4])[:max_tokens]   # richest first

    def build(fills, m, tk):
        src = SimFillSource(store)
        out = []
        for fl in fills[:max_fills_per_token]:
            if fl.price <= 0 or fl.size <= 0:
                continue
            book = store.book_at(m, tk, fl.ts)
            if book is None:
                continue
            limit = 0.999 if fl.side == "BUY" else 0.001
            order = Order(order_id="gr", strategy_id="gr", market_id=m, token_id=tk,
                          side=fl.side, submit_ts=fl.ts, limit_price=limit, qty=fl.size, mode="taker")
            rep = execute(order, src)
            if rep.filled_qty <= 0:
                continue
            out.append({
                "token": tk[:12], "ts": fl.ts, "side": fl.side, "size": fl.size,
                "real_price": fl.price, "sim_avg_price": rep.avg_fill_price,
                "dev_bps": round((rep.avg_fill_price - fl.price) / fl.price * 1e4, 2),
                "tier": rep.fidelity_tier, "degraded": rep.degraded,
                "low_confidence": any("low-confidence" in w for w in rep.warnings),
                "stale_warn": [w for w in rep.warnings if "stale" in w],
            })
        return out

    # Fill-truth source = captured prints (fee-by-formula; prints carry no fee field).
    cases: List[dict] = []
    for (m, tk, t0, t1, n) in universe:
        if len(cases) >= target_fills:
            break
        prints = store.prints_between(m, tk, t0, t1)
        f = [OnchainFill(market_id=m, token_id=tk, ts=p.ts, price=p.price, size=p.size,
                         side=p.side, maker="", taker="", tx_hash="", fee=0.0) for p in prints]
        if f:
            cases += build(f, m, tk)
    source_used = "prints" if cases else "none"

    devs = [abs(c["dev_bps"]) for c in cases]
    # price accuracy on HIGH-CONFIDENCE fills (the ones a strategy should trust/trade)
    hc = [abs(c["dev_bps"]) for c in cases if not c.get("low_confidence")]
    n_lowconf = sum(1 for c in cases if c.get("low_confidence"))
    summary = {
        "source": source_used,
        "n_fills": len(cases),
        "median_abs_dev_bps": round(statistics.median(devs), 2) if devs else None,
        "max_abs_dev_bps": round(max(devs), 2) if devs else None,
        "high_conf_n": len(hc),
        "high_conf_median_bps": round(statistics.median(hc), 2) if hc else None,
        "high_conf_p90_bps": round(sorted(hc)[int(0.9 * (len(hc) - 1))], 2) if hc else None,
        "high_conf_max_bps": round(max(hc), 2) if hc else None,
        "low_confidence_flagged": n_lowconf,
        "note": ("fee NOT verified from prints (prints lack a fee field); fees by formula. "
                 "GATE is on the HIGH-CONFIDENCE set: penny/coarse-tick/stale/volatile/large-impact "
                 "fills flagged low-confidence (a 5s book can't price them) and excluded from the gate"),
    }
    os.makedirs(FINDINGS_DIR, exist_ok=True)
    with open(REPORT, "w") as f:
        json.dump({"summary": summary, "cases": cases}, f, indent=2)
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
