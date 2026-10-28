"""driver/exit_rules.py — GENERIC exit-order construction (framework code, no tenant imports).

tpsl_exit_order builds the TP/SL exit for any held prediction-market position from the live touch; it
is strategy-agnostic math (return on committed capital vs the realizable exit price), so it lives in
the framework: the exit monitor and any tenant can use it, and a desk-only tree runs the exit monitor
without a tenant. A tenant may override the builder per arm via its manifest hook
`"exit_order_builder": "pkg.mod:fn"` — same signature, returning an Order or None.
"""
from __future__ import annotations

from typing import Optional

from desk.types import Order


def tpsl_exit_order(pos, route_d, m, tk, sid, as_of_ts, exit_tp, exit_sl) -> Optional[Order]:
    """TP/SL exit on return-on-committed-capital (BUY commits entry, SELL commits 1−entry).
    Return is measured on the REALIZABLE exit price — the touch we'd actually HIT taking liquidity: sell
    INTO the best bid (long) / buy AT the best ask (short) — NOT the mid (mid overstates the achievable
    return, so TP could fire on a price you can't get and SL could miss a bid that already dropped). The
    decision price and the exit limit are the same touch. Fair-free (entry vs touch); final-day safe. None = no exit."""
    q = float((pos or {}).get("qty") or 0.0)
    if not q or (exit_tp is None and exit_sl is None):
        return None
    book = getattr(route_d, "book", None)
    ep = float(pos.get("entry_price") or 0.0)
    cap_ps = ep if q > 0 else (1.0 - ep)
    if cap_ps <= 0 or book is None:
        return None
    px = book.best_bid if q > 0 else book.best_ask     # the side we'd HIT to close the full position
    if px is None or not (0.0 < px < 1.0):
        return None
    ret = ((px - ep) if q > 0 else (ep - px)) / cap_ps  # realizable return on committed capital
    if not ((exit_tp is not None and ret >= float(exit_tp))
            or (exit_sl is not None and ret <= -float(exit_sl))):
        return None
    limit = max(0.01, min(0.99, round(px * 100) / 100))
    return Order(order_id=f"{sid}-exit-{m[:8]}-{tk[:6]}-{as_of_ts}", strategy_id=sid,
                 market_id=m, token_id=tk, side=("SELL" if q > 0 else "BUY"),
                 submit_ts=as_of_ts, limit_price=limit, qty=abs(q), tif="IOC",
                 mode="taker", entry_vol=round(abs(px - ep), 4))
