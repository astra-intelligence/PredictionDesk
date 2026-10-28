"""signals/example_calendar.py — EXTERNAL toy strategy emitting Order[].

Strategy is a SEPARATE component; the engine is strategy-agnostic. This toy buys a
token early in its captured window and sells late ("buy Mon / sell Fri"). Configurable
as taker or maker:
  - taker: cross the spread now (wide limit), guaranteed fill, pays fee.
  - maker: rest passively at best bid/ask (provide liquidity), 0 fee, fills only if the
    market trades through your price within the validity window.
No edge — a smoke test exercising real ladder-walk / maker fills + fees end-to-end.
"""
from __future__ import annotations

from typing import List, Optional

from desk.types import Order
from desk.data.store import Store


def generate_orders(store: Optional[Store] = None, qty: float = 50.0, n_markets: int = 3,
                    min_snaps: int = 2, mode: str = "taker",
                    maker_validity_s: int = 3600) -> List[Order]:
    """mode='taker' (cross spread) | 'maker' (rest at best bid/ask, GTD validity)."""
    store = store or Store()
    orders: List[Order] = []
    universe = sorted(store.universe(), key=lambda r: -r[4])   # richest tokens first
    for i, (m, tk, t0, t1, n) in enumerate(universe):
        if n < min_snaps or t1 <= t0:
            continue
        buy_ts = t0 + (t1 - t0) // 4
        sell_ts = t0 + 3 * (t1 - t0) // 4
        if mode == "maker":
            bb, bs = store.book_at(m, tk, buy_ts), store.book_at(m, tk, sell_ts)
            if not bb or not bs or not bb.bids or not bs.asks:
                continue
            orders.append(Order(f"cal-buy-{i}", "calendar", m, tk, "BUY", buy_ts,
                                bb.best_bid, qty, "GTD", buy_ts + maker_validity_s, "maker"))
            orders.append(Order(f"cal-sell-{i}", "calendar", m, tk, "SELL", sell_ts,
                                bs.best_ask, qty, "GTD", sell_ts + maker_validity_s, "maker"))
        else:
            orders.append(Order(f"cal-buy-{i}", "calendar", m, tk, "BUY", buy_ts, 0.99, qty, "IOC", None, "taker"))
            orders.append(Order(f"cal-sell-{i}", "calendar", m, tk, "SELL", sell_ts, 0.01, qty, "IOC", None, "taker"))
        if len({o.market_id for o in orders}) >= n_markets:
            break
    return orders


def describe(params: dict) -> str:
    """Rule-line describe hook (ui-redesign v2): the builtin's own plain-English params line,
    discovered beside the entrypoint (desk.control_plane.rule_line). Schedule text is the
    framework's half — never restated here."""
    qty = float(params.get("qty", 50.0))
    n = int(params.get("n_markets", 3))
    mode = params.get("mode", "taker")
    return (f"toy sample (no edge model) · {qty:g} sh/clip · "
            f"{n} market{'s' if n != 1 else ''} ({mode}) · holds to settlement")
