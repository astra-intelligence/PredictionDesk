"""eval/recon/reconcile_live_fills.py — PASSIVE sim-vs-live reconciliation of the DRIVER's REAL fills.

No probe orders are placed: this replays each ALREADY-EXECUTED
live fill (from tracker/trades.jsonl, source=live) through the SIM engine at the fill's as-of ts — the
backtest read path — and records the price deviation (live avg-fill vs what the sim/backtest engine fills at
the captured book of that instant). NO orders are placed. Writes findings/sim_vs_live_report.json, which the
dashboard reads for the 'sim↔live mean |dev| bps' badge (the twin-invariant credibility line: live ≡ sim).

IMPORTANT: run with the FULL book read (DESK_BOOK_MAX_FILES=0) — the driver's live newest-K cap is for
current-book reads only; a past fill's book lives in OLDER part-files, so the recon needs the whole set.

    DESK_BOOK_MAX_FILES=0 python3 -m desk.eval.recon.reconcile_live_fills
"""
from __future__ import annotations

import json
import os
import time

from desk.types import Order
from desk.sim.engine import execute
from desk.sim.fill_source import SimFillSource
from desk.data.store import Store

_DESK = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # …/desk
TRADES = os.path.join(_DESK, "tracker", "trades.jsonl")   # anchored to the package, not the launch dir
REPORT = os.path.join(os.path.dirname(__file__), "findings", "sim_vs_live_report.json")


def _live_fills() -> list:
    """Latest live TradeRow per order_id (dedupe — reconcile is idempotent/regenerating)."""
    out: dict = {}
    if not os.path.exists(TRADES):
        return []
    for line in open(TRADES):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("source") != "live":
            continue
        oid = d.get("order_id")
        if oid:
            out[oid] = d
    return list(out.values())


def reconcile(store: Store = None) -> dict:
    """Replay every live fill through the sim engine at its as-of ts; write the deviation report."""
    store = store or Store(book_max_files=0)   # FULL book read — a past fill's book is in OLDER part-files,
    # so the driver's live newest-K cap must not apply here (else book_at(past-ts) misses it).
    runs = []
    for tr in _live_fills():
        m, tk = tr.get("market_id"), tr.get("token_id")
        side, qty = tr.get("side"), float(tr.get("qty") or 0.0)
        live_px = tr.get("price")
        ts = int(tr.get("ts") or 0)
        if not (m and tk and side and qty > 0 and live_px and ts):
            continue
        limit = float(tr.get("limit_price") or (0.999 if side == "BUY" else 0.001))
        order = Order("recon", tr.get("strategy", "live"), m, tk, side, ts, limit, qty,
                      tif="IOC", mode="taker")   # IOC = the declared immediate-taker tif (FOK is the only
        # tif the fill model special-cases; an undeclared value behaved the same but read as a fourth kind)
        try:                                     # sim = the backtest engine at the captured book of that ts
            sim = execute(order, SimFillSource(store))
        except Exception:
            sim = None
        sim_px = getattr(sim, "avg_fill_price", None)
        dev_bps = (round((float(live_px) - sim_px) / sim_px * 1e4, 2)
                   if (sim_px and float(live_px)) else None)
        runs.append({
            "ts": ts, "order_id": tr.get("order_id"), "strategy": tr.get("strategy"),
            "token": str(tk)[:14], "side": side, "size": qty,
            "sim": (None if sim is None else {
                "avg_fill_price": round(sim_px, 5) if sim_px else None,
                "filled_qty": round(getattr(sim, "filled_qty", 0.0) or 0.0, 4),
                "fees": getattr(sim, "fees", None), "tier": getattr(sim, "fidelity_tier", None)}),
            "live": {"avg_fill_price": round(float(live_px), 5), "filled_qty": qty, "fees": tr.get("fees")},
            "deviation": (None if sim_px is None else {
                "price_bps": dev_bps,
                "fill_qty_diff": round(qty - (getattr(sim, "filled_qty", 0.0) or 0.0), 4),
            }),
        })
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w") as fh:                # REGENERATE (deduped by order_id), not append
        json.dump({"runs": runs, "generated_ts": int(time.time()), "mode": "passive-live-fill-replay"}, fh, indent=2)
    measured = [r for r in runs if (r.get("deviation") or {}).get("price_bps") is not None]
    return {"n_fills": len(runs), "n_measured": len(measured),
            "mean_abs_dev_bps": (round(sum(abs(r["deviation"]["price_bps"]) for r in measured) / len(measured), 1)
                                 if measured else None),
            "unmeasured_reason": "no captured book at fill ts (market not in local capture window)"}


if __name__ == "__main__":
    print(json.dumps(reconcile(), indent=2))
