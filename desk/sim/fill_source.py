"""sim/fill_source.py — the single sim/live hinge.

FillSource.fills(order) -> FillResult is the ONE seam between simulation and live
trading. The engine code path is identical for both; only this object differs:
  SimFillSource  -> walk the recorded book (fill_model) + apply exact fees
  LiveFillSource -> send to the real CLOB and read back real fills      [P4, deferred]

=> any sim-vs-reality divergence lives here, never in fees/accounting/report shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from desk.types import Order, Fill
from desk.sim.fill_model import FillModel, Assumptions
from desk.sim import fees as fees_mod
from desk.data.store import Store


@dataclass
class FillResult:
    fills: List[Fill]               # fee already applied per fill
    tier: str
    mid: Optional[float]
    unfilled_qty: float
    source: str                     # SIM | LIVE
    degraded: bool = False
    warnings: List[str] = field(default_factory=list)
    # Append-only: cost-estimate confidence carried up to ExecReport.
    expected_cost_c: Optional[float] = None
    cost_p90_c: Optional[float] = None
    spread_source: Optional[str] = None
    est_confidence: Optional[str] = None
    slippage_c: Optional[float] = None      # per-market temporal slippage applied, cents
    # Append-only (maker queue-ahead): book depth at-or-better than the resting limit at submit.
    queue_ahead: Optional[float] = None
    # Append-only: the EXCHANGE order id the live CLOB assigned to this order (None in sim). The driver
    # persists it so its reconciliation pass can re-poll a GTC/maker order for fills that landed after
    # the immediate post-response — the one poll a single post ever sees.
    exchange_order_id: Optional[str] = None


class FillSource:
    def fills(self, order: Order) -> FillResult:        # pragma: no cover - interface
        raise NotImplementedError


class SimFillSource(FillSource):
    """Recorded book -> ladder-walk -> exact fees. The backtest/paper engine."""

    def __init__(self, store: Store, assumptions: Optional[Assumptions] = None,
                 category_of: Optional[Callable[[str], Optional[str]]] = None,
                 fee_rate_of: Optional[Callable[[Order], Optional[float]]] = None,
                 cost_model=None):
        self.model = FillModel(store, assumptions, cost_model=cost_model)
        self.category_of = category_of            # market_id -> category (fee fallback); optional
        self.fee_rate_of = fee_rate_of            # order -> exact feeRate (e.g. on-chain-derived); optional

    def fills(self, order: Order) -> FillResult:
        r = self.model.fills(order)
        category = self.category_of(order.market_id) if self.category_of else None
        rate = self.fee_rate_of(order) if self.fee_rate_of else None
        out: List[Fill] = []
        for rf in r.raw_fills:
            fee = fees_mod.fee_for(rf.qty, rf.price, mode=order.mode,
                                   category=category, ts=rf.ts, rate=rate)
            out.append(Fill(ts=rf.ts, price=rf.price, qty=rf.qty, fee=fee))
        return FillResult(fills=out, tier=r.tier, mid=r.mid, unfilled_qty=r.unfilled_qty,
                          source="SIM", degraded=r.degraded, warnings=r.warnings,
                          expected_cost_c=r.expected_cost_c, cost_p90_c=r.cost_p90_c,
                          spread_source=r.spread_source, est_confidence=r.confidence,
                          slippage_c=r.slippage_c, queue_ahead=r.queue_ahead)


class LiveFillSource(FillSource):
    """Real CLOB execution via live/clob_adapter (P4 — gated behind evaluator PASS)."""

    def __init__(self, adapter=None):
        self.adapter = adapter

    def fills(self, order: Order) -> FillResult:
        if self.adapter is None:
            raise RuntimeError("LiveFillSource needs a clob_adapter (P4); not enabled")
        return self.adapter.execute(order)
