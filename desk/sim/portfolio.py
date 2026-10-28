"""sim/portfolio.py — per-run position & PnL incl. resolution .

Average-cost accounting, fee-inclusive so PnL is exact:
  - BUY adds (notional + fees) to cost basis.
  - SELL realizes (proceeds - fees) - cost_basis on the closed shares.
  - resolution marks a position to its payout (1.0 won / 0.0 lost) and realizes it.

total_pnl = exec_pnl (realized from trading) + resolution_pnl + unrealized (open).
ExecReport carries no market/token id, so apply() takes the originating Order too.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from desk.types import Order, ExecReport, Resolution, Position

Key = Tuple[str, str]


class Portfolio:
    def __init__(self, starting_cash: float = 0.0):
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions: Dict[Key, Position] = {}
        self.fees_paid = 0.0
        self.exec_pnl = 0.0          # realized from trading (reductions)
        self.resolution_pnl = 0.0    # realized from settlement

    def _pos(self, m: str, tk: str) -> Position:
        return self.positions.setdefault((m, tk), Position(market_id=m, token_id=tk))

    def apply(self, order: Order, report: ExecReport) -> None:
        self.cash += report.d_cash
        self.fees_paid += report.fees
        f = report.filled_qty
        if f <= 0:
            return
        pos = self._pos(order.market_id, order.token_id)
        signed = f if order.side == "BUY" else -f
        notional = report.avg_fill_price * f
        fees = report.fees
        old = pos.qty

        if old == 0 or (old > 0) == (signed > 0):
            # increasing magnitude -> weighted fee-inclusive basis
            add_basis = (notional + fees) if signed > 0 else (notional - fees)
            new_qty = old + signed
            pos.avg_cost = (abs(old) * pos.avg_cost + abs(add_basis)) / abs(new_qty)
            pos.qty = round(new_qty, 6)
        else:
            # reducing / closing -> realize PnL on the closed shares
            close = min(f, abs(old))
            if old > 0:                          # closing long via sell
                proceeds_per = (notional - fees) / f
                pos.realized_pnl += close * (proceeds_per - pos.avg_cost)
            else:                                # closing short via buy
                cost_per = (notional + fees) / f
                pos.realized_pnl += close * (pos.avg_cost - cost_per)
            pos.qty = round(old + signed, 6)
            if pos.qty == 0:
                pos.avg_cost = 0.0
            elif (old > 0) != (pos.qty > 0):     # flipped past zero
                pos.avg_cost = report.avg_fill_price
        self._recompute_exec_pnl()

    def settle(self, res: Resolution) -> None:
        """Mark the position for (market,token) to its payout and realize it."""
        pos = self.positions.get((res.market_id, res.token_id))
        if pos is None or pos.qty == 0:
            return
        gain = pos.qty * (res.payout - pos.avg_cost)
        self.resolution_pnl += gain
        self.cash += pos.qty * res.payout
        pos.realized_pnl += gain
        pos.qty = 0.0
        pos.avg_cost = 0.0

    def _recompute_exec_pnl(self) -> None:
        self.exec_pnl = sum(p.realized_pnl for p in self.positions.values()) - self.resolution_pnl

    def unrealized(self, marks: Optional[Dict[Key, float]] = None) -> float:
        marks = marks or {}
        u = 0.0
        for key, pos in self.positions.items():
            if pos.qty == 0:
                continue
            mark = marks.get(key, pos.avg_cost)
            pos.unrealized_pnl = pos.qty * (mark - pos.avg_cost)
            u += pos.unrealized_pnl
        return u

    def equity(self, marks: Optional[Dict[Key, float]] = None) -> float:
        return self.cash + self.unrealized(marks) + self._position_cost()

    def _position_cost(self) -> float:
        # cost already left the cash account on entry; add basis back so equity = cash + MV.
        return sum(p.qty * p.avg_cost for p in self.positions.values() if p.qty != 0)

    def total_pnl(self, marks: Optional[Dict[Key, float]] = None) -> float:
        return self.exec_pnl + self.resolution_pnl + self.unrealized(marks)

    def snapshot(self) -> dict:
        return {
            "cash": round(self.cash, 4),
            "fees_paid": round(self.fees_paid, 4),
            "exec_pnl": round(self.exec_pnl, 4),
            "resolution_pnl": round(self.resolution_pnl, 4),
            "positions": {f"{m}:{tk[:10]}": round(p.qty, 4)
                          for (m, tk), p in self.positions.items() if p.qty != 0},
        }
