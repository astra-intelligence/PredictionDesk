"""sample_calendar/strategy.py — buy early / sell late on captured books. No edge, no data.

Taker mode crosses the spread now (wide limit, guaranteed fill, pays fee); maker mode rests
passively at best bid/ask (0 fee, GTD validity, fills only if the market trades through). The
point is the CONTRACT, not the signal: `generate_orders(store, **params)` is everything a tenant
entrypoint must be — the driver passes only the kwargs the signature accepts.
"""
from __future__ import annotations

from typing import List, Optional

from desk.data.store import Store
from desk.types import Order


def generate_orders(store: Optional[Store] = None, qty: float = 50.0, n_markets: int = 3,
                    min_snaps: int = 2, mode: str = "taker",
                    maker_validity_s: int = 3600, out_signals: Optional[dict] = None) -> List[Order]:
    """mode='taker' (cross spread) | 'maker' (rest at best bid/ask, GTD validity)."""
    store = store or Store()
    orders: List[Order] = []
    n_views = 0                                                # candidates priced this pass
    universe = sorted(store.universe(), key=lambda r: -r[4])   # richest tokens first
    for i, (m, tk, t0, t1, n) in enumerate(universe):
        if n < min_snaps or t1 <= t0:
            continue
        n_views += 1
        buy_ts = t0 + (t1 - t0) // 4
        sell_ts = t0 + 3 * (t1 - t0) // 4
        if mode == "maker":
            bb, bs = store.book_at(m, tk, buy_ts), store.book_at(m, tk, sell_ts)
            if not bb or not bs or not bb.bids or not bs.asks:
                continue
            orders.append(Order(f"scal-buy-{i}", "sample_calendar", m, tk, "BUY", buy_ts,
                                bb.best_bid, qty, "GTD", buy_ts + maker_validity_s, "maker"))
            orders.append(Order(f"scal-sell-{i}", "sample_calendar", m, tk, "SELL", sell_ts,
                                bs.best_ask, qty, "GTD", sell_ts + maker_validity_s, "maker"))
        else:
            orders.append(Order(f"scal-buy-{i}", "sample_calendar", m, tk, "BUY", buy_ts,
                                0.99, qty, "IOC", None, "taker"))
            orders.append(Order(f"scal-sell-{i}", "sample_calendar", m, tk, "SELL", sell_ts,
                                0.01, qty, "IOC", None, "taker"))
        if len({o.market_id for o in orders}) >= n_markets:
            break
    if out_signals is not None:
        out_signals["__n_views__"] = n_views   # fire observability: the driver logs this as n_views
    return orders


def describe(params: dict) -> str:
    """Rule-line describe hook: this tenant's plain-English params line (schedule text is the
    framework's half — never restated here)."""
    qty = float(params.get("qty", 50.0))
    n = int(params.get("n_markets", 3))
    mode = params.get("mode", "taker")
    return (f"minimal sample tenant (no edge model) · {qty:g} sh/clip · "
            f"{n} market{'s' if n != 1 else ''} ({mode}) · holds to settlement")
