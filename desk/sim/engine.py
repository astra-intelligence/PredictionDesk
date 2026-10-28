"""sim/engine.py — execute one order over a FillSource (the heart of the engine).

execute(order, fill_source) -> ExecReport is IDENTICAL for sim and live: it asks the
FillSource for fills, then assembles the report (avg price, fees, slippage, position
and cash deltas) the SAME way regardless of source. Swapping SimFillSource <->
LiveFillSource is the only difference between backtest and live — sim ≡ live by
construction. No data access here; the FillSource owns that.
"""
from __future__ import annotations

from desk.types import Order, ExecReport
from desk.sim.fill_source import FillSource, FillResult


def execute(order: Order, fill_source: FillSource) -> ExecReport:
    r: FillResult = fill_source.fills(order)
    return _assemble(order, r)


def _assemble(order: Order, r: FillResult) -> ExecReport:
    filled_qty = round(sum(f.qty for f in r.fills), 4)
    fees = round(sum(f.fee for f in r.fills), 4)
    notional = sum(f.price * f.qty for f in r.fills)
    avg = round(notional / filled_qty, 6) if filled_qty > 0 else 0.0

    # signed slippage vs mid, bps; positive = worse than mid (paid up / sold down). None (NOT 0.0) when
    # there's no true book mid to measure against — e.g. a live marketable-limit fill whose FillResult.mid
    # the adapter couldn't fetch — so a genuine "unmeasurable" is never reported as a real 0-bps fill.
    slippage_bps = None
    if filled_qty > 0 and r.mid:
        if order.side == "BUY":
            slippage_bps = round((avg - r.mid) / r.mid * 1e4, 2)
        else:
            slippage_bps = round((r.mid - avg) / r.mid * 1e4, 2)

    if order.side == "BUY":
        d_position = filled_qty
        d_cash = -(notional + fees)
    else:
        d_position = -filled_qty
        d_cash = notional - fees

    return ExecReport(
        order_id=order.order_id,
        strategy_id=order.strategy_id,
        fills=r.fills,
        filled_qty=filled_qty,
        avg_fill_price=avg,
        fees=fees,
        slippage_bps_vs_mid=slippage_bps,
        unfilled_qty=round(r.unfilled_qty, 4),
        d_position=round(d_position, 4),
        d_cash=round(d_cash, 4),
        source=r.source,
        fidelity_tier=r.tier,
        degraded=r.degraded,
        warnings=r.warnings,
        # Pass-through cost-estimate reliability. Identical in sim and live, so a live T1 fill
        # self-reports "estimated cost, low confidence". No edge/veto logic.
        cost_est_c=getattr(r, "expected_cost_c", None),
        cost_p90_c=getattr(r, "cost_p90_c", None),
        spread_source=getattr(r, "spread_source", None),
        est_confidence=getattr(r, "est_confidence", None),
        slippage_c=getattr(r, "slippage_c", None),
        # Maker queue-ahead: book depth ahead of our resting limit at submit. The TradeRow wiring
        # reads rep.queue_ahead; None on taker / missing-submit-book fills.
        queue_ahead=getattr(r, "queue_ahead", None),
        # Live exchange order id: the driver persists it for post-fire fill reconciliation.
        exchange_order_id=getattr(r, "exchange_order_id", None),
    )
