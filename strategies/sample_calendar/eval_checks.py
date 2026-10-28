"""sample_calendar/eval_checks.py — this tenant's eval-gate sections (manifest hook `eval_checks`).

Run by `make eval-fast` after the core framework gate, labeled `sample_calendar:<section>`.
OFFLINE by design: a synthetic two-snapshot book, no store dir, no network — the gate stays green
on a fresh clone and in CI. This section is also the tenant contract's proof-by-example: a tenant
needs NOTHING beyond the capture store and the desk APIs to be fully gate-checked.
"""
from __future__ import annotations

from desk.eval.api import Section
from desk.types import BookSnapshot

from .strategy import describe, generate_orders


def _book(ts: int, bid: float, ask: float) -> BookSnapshot:
    return BookSnapshot(
        market_id="M", token_id="TK", capture_ts=ts, exchange_ts=ts, hash=f"h{ts}",
        bids=[[bid, 200.0]], asks=[[ask, 200.0]], best_bid=bid, best_ask=ask,
        mid=(bid + ask) / 2, spread=ask - bid, tick_size=0.001, min_order_size=1,
        neg_risk=False, last_trade_price=None)


class _FakeStore:
    """Duck-types the two Store methods the strategy reads (universe, book_at): one token,
    two snapshots 300s apart. No filesystem, no network."""

    T0, T1 = 1_781_568_000, 1_781_568_300

    def __init__(self, n_snaps: int = 2):
        self._n = n_snaps
        self._books = {self.T0: _book(self.T0, 0.44, 0.46), self.T1: _book(self.T1, 0.55, 0.57)}

    def universe(self):
        return [("M", "TK", self.T0, self.T1, self._n)]

    def book_at(self, market_id, token_id, t):
        prior = [ts for ts in sorted(self._books) if ts <= t]
        return self._books[prior[-1]] if prior else None


def check_contract() -> Section:
    """HARD guard: the entrypoint's order shapes on a synthetic store — the exact surface the
    driver and the sim engine consume (taker IOC pair, maker rest at touch with GTD validity,
    min_snaps filter, quarter/three-quarter timing)."""
    s = Section("contract", hard=True)
    try:
        st = _FakeStore()
        # taker: one buy/sell IOC pair, wide limits, timed at 1/4 and 3/4 of the window
        orders = generate_orders(st, qty=5.0)
        s.check(len(orders) == 2 and [o.side for o in orders] == ["BUY", "SELL"],
                "taker mode emits a BUY/SELL pair", repr([(o.side, o.tif) for o in orders]))
        if len(orders) == 2:
            b, sl = orders
            s.check(b.tif == "IOC" and sl.tif == "IOC" and b.mode == sl.mode == "taker",
                    "taker orders are IOC/taker", f"{b.tif}/{sl.tif} {b.mode}/{sl.mode}")
            s.check(b.limit_price == 0.99 and sl.limit_price == 0.01,
                    "taker limits cross the spread (wide)", f"{b.limit_price}/{sl.limit_price}")
            span = _FakeStore.T1 - _FakeStore.T0
            s.check(b.submit_ts == _FakeStore.T0 + span // 4
                    and sl.submit_ts == _FakeStore.T0 + 3 * span // 4,
                    "buy at 1/4, sell at 3/4 of the captured window",
                    f"{b.submit_ts}/{sl.submit_ts}")
            s.check(b.strategy_id == "sample_calendar" and b.qty == 5.0,
                    "orders carry this tenant's strategy_id + qty", f"{b.strategy_id} {b.qty}")
        # maker: rests at best bid/ask of the as-of book, GTD with the validity window
        morders = generate_orders(st, mode="maker", maker_validity_s=600)
        s.check(len(morders) == 2 and all(o.mode == "maker" and o.tif == "GTD" for o in morders),
                "maker mode emits GTD/maker pair", repr([(o.tif, o.mode) for o in morders]))
        if len(morders) == 2:
            mb, ms = morders
            s.check(mb.limit_price == 0.44 and ms.limit_price == 0.46,
                    "maker rests at the as-of best bid / best ask", f"{mb.limit_price}/{ms.limit_price}")
            s.check(mb.expiry_ts == mb.submit_ts + 600 and ms.expiry_ts == ms.submit_ts + 600,
                    "maker validity window = maker_validity_s", f"{mb.expiry_ts}/{ms.expiry_ts}")
        # min_snaps: a thin token (fewer snapshots) is skipped entirely
        s.check(generate_orders(_FakeStore(n_snaps=1)) == [],
                "token under min_snaps emits nothing (thin-capture guard)")
        # describe hook renders a non-empty rule line from params
        line = describe({"qty": 5.0, "n_markets": 2, "mode": "maker"})
        s.check(bool(line) and "maker" in line and "2 markets" in line,
                "describe hook renders the params", line)
    except Exception as e:
        s.check(False, "contract guard ran", repr(e))
    return s


def sections():
    return [check_contract()]
