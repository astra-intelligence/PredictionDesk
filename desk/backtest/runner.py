"""backtest/runner.py — replay a strategy through the paper-trading engine.

run(orders, cfg) -> BacktestResult. Shares its signature with live/executor.run so
backtest and live are interchangeable. For each order it calls the
SAME engine.execute over a SimFillSource, applies the report to the portfolio, and
records equity. At the end it settles resolved markets and reports a P&L bracket
(optimistic / base / adversarial fill assumptions) — never a single point estimate.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from desk.types import Order, ExecReport, BacktestResult, Resolution, RunMeta
from desk.sim.engine import execute
from desk.sim.fill_source import SimFillSource
from desk.sim.fill_model import Assumptions
from desk.sim.portfolio import Portfolio
from desk.data.store import Store
from desk.tracker.pnl_ledger import PnLLedger

Key = Tuple[str, str]

RUNS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "eval", "runs")
PASS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "eval", "recon", "findings", "PASS")


def _config_hash(config: dict) -> str:
    """sha256 over the canonical config JSON — same shape as signals.registry.config_hash,
    but on the whole config dict (the ablation grid = one hash per run's config-of-record)."""
    return hashlib.sha256(
        json.dumps(config or {}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


from desk.runtime.guard import code_ref as _code_ref, declared_env as _denv   # ONE code-ref/env helper


def _gate_marker() -> Optional[str]:
    """run.json gate stamp = the SCOPED PASS (eval/recon/PASS.json) — the legacy bare findings/PASS marker
    is scope-blind and NOT honored anywhere (the executor refuses it too, so a run.json must
    never show green off a marker the live gate rejects)."""
    try:
        from desk.live import executor as _ex          # lazy: executor imports this module
        pscope = _ex.pass_scope()
        return f"PASS[{','.join(pscope['scope'])}]" if pscope else None
    except Exception:
        return None

# P&L bracket assumption sets (queue/latency are unobservable from recorded data).
# ADVERSARIAL drops the top best level (guards against thin-top-of-book staleness)
# so the bracket bounds the optimistic-fill bias that sparse snapshots otherwise hide.
# cost_band sweeps the unified cost_model: opt = no cost (mid), adv = p90 spread+temporal.
OPTIMISTIC = Assumptions(latency_ms=0, maker_rule="touch", queue_fill_prob=1.0, cost_band="opt")
ADVERSARIAL = Assumptions(latency_ms=5000, maker_rule="through", queue_fill_prob=0.3,
                          skip_best_levels=1, cost_band="adv")


@dataclass
class RunConfig:
    store: Optional[Store] = None
    starting_cash: float = 0.0
    assumptions: Assumptions = field(default_factory=Assumptions)   # base case
    category_of: Optional[Callable[[str], Optional[str]]] = None
    fee_rate_of: Optional[Callable] = None     # order -> exact feeRate (e.g. on-chain-derived)
    resolutions: Dict[Key, Resolution] = field(default_factory=dict)  # override store lookups
    compute_bracket: bool = True
    mark_mode: str = "resolution"   # "resolution" = settle to $1/$0 | "market" = mark open pos at last mid (trade-PnL)
    cost_model: Optional[object] = None  # unified ExecCostModel: spread+depth (T1) AND temporal slippage (any tier)
    # RunMeta / trade-event emission. All OPTIONAL — set run_id to have
    # run() write desk/eval/runs/<run_id>/{run.json, trades.jsonl}. The ablation grid = many run_ids.
    run_id: Optional[str] = None
    source: str = "backtest"        # backtest | paper | live (backtests emit the SAME TradeRow schema)
    strategy: str = ""
    config: dict = field(default_factory=dict)
    signals: Dict[Key, dict] = field(default_factory=dict)  # optional fair/edge/decomp/slug join, keyed (market,token)
    budget: float = 0.0             # display budget for RunMeta (defaults to starting_cash if 0)


def run(orders: List[Order], cfg: Optional[RunConfig] = None) -> BacktestResult:
    ref = _code_ref()                      # once per run — grid runs fork no extra git subprocesses
    cfg = cfg or RunConfig()
    store = cfg.store or Store()
    orders = sorted(orders, key=lambda o: o.submit_ts)

    pf, reports, equity, marks = _simulate(orders, store, cfg, cfg.assumptions)

    bracket = {}
    if cfg.compute_bracket and orders:
        base_pnl = pf.total_pnl(marks)
        # opt/base/adv = cost_band opt/base/adv (baked into the presets). With a cost_model injected the
        # bracket is MEASURED (opt=mid, base=median spread+p75 temporal, adv=p90); without one it's a no-op.
        op = _simulate(orders, store, cfg, OPTIMISTIC)
        ad = _simulate(orders, store, cfg, ADVERSARIAL)
        bracket = {"opt": round(op[0].total_pnl(op[3]), 4), "base": round(base_pnl, 4),
                   "adv": round(ad[0].total_pnl(ad[3]), 4)}

    requested = sum(o.qty for o in orders) or 1.0
    filled = sum(r.filled_qty for r in reports)
    result = BacktestResult(
        equity_curve=equity,
        total_pnl=round(pf.total_pnl(marks), 4),
        exec_pnl=round(pf.exec_pnl, 4),
        resolution_pnl=round(pf.resolution_pnl, 4),
        fill_ratio=round(filled / requested, 4),
        bracket=bracket,
        exec_reports=reports,
        data_quality=_data_quality(reports),
    )
    if cfg.run_id:
        _emit_run_dir(orders, result, cfg, bracket, ref)
    return result


def _emit_run_dir(orders: List[Order], result: BacktestResult, cfg: RunConfig,
                  bracket: dict, ref: str) -> str:
    """Write desk/eval/runs/<run_id>/{run.json, trades.jsonl}. A per-run ledger emits the SAME
    TradeRow rows a paper/live run would (backtests are just source='backtest'); run.json pins the
    run to its code+config+env so a headline number can't silently drift off its config-of-record."""
    run_dir = os.path.join(RUNS_DIR, cfg.run_id)
    os.makedirs(run_dir, exist_ok=True)
    cfg_hash = _config_hash(cfg.config)
    ts_all = [o.submit_ts for o in orders] or [int(time.time())]
    # Per-run trades log (scoped to the run dir; the grid = one dir per run_id).
    led = PnLLedger(path=os.path.join(run_dir, "ledger.json"),
                    trades_path=os.path.join(run_dir, "trades.jsonl"))
    orders_by_id = {o.order_id: o for o in orders}
    run_meta = {"run_id": cfg.run_id, "source": cfg.source, "strategy": cfg.strategy,
                "config_hash": cfg_hash, "orders_by_id": orders_by_id,
                "as_of": max(ts_all), "code_ref": ref,
                "env": _denv()}
    led.log_run(result, as_of=max(ts_all), run_meta=run_meta, signals=cfg.signals or {})
    meta = RunMeta(
        run_id=cfg.run_id, source=cfg.source, strategy=cfg.strategy,
        config=cfg.config, config_hash=cfg_hash,
        starting_cash=cfg.starting_cash, budget=cfg.budget or cfg.starting_cash,
        window={"min_ts": min(ts_all), "max_ts": max(ts_all)},
        cost_band=bracket or {},
        code_ref=ref,
        env=_denv(),
        gate=_gate_marker(),
        settle_source_counts=_settle_counts(result, cfg),
        created_ts=int(time.time()))
    with open(os.path.join(run_dir, "run.json"), "w") as f:
        json.dump(meta.to_dict(), f, indent=2)
    return run_dir


def _settle_counts(result: BacktestResult, cfg: RunConfig) -> dict:
    """Settle-source tally for RunMeta. ExecReports don't carry market/token, so we count the
    Resolution.source of the resolutions overriding this run (GAMMA|CLOB). Empty when the run
    marks-to-market instead of settling (mark_mode='market')."""
    counts: Dict[str, int] = {}
    for res in (cfg.resolutions or {}).values():
        counts[res.source or "GAMMA"] = counts.get(res.source or "GAMMA", 0) + 1
    return counts


def _simulate(orders: List[Order], store: Store, cfg: RunConfig,
              assumptions: Assumptions) -> Tuple[Portfolio, List[ExecReport], List[List[float]], Dict[Key, float]]:
    src = SimFillSource(store, assumptions, cfg.category_of, cfg.fee_rate_of, cost_model=cfg.cost_model)
    pf = Portfolio(cfg.starting_cash)
    reports: List[ExecReport] = []
    equity: List[List[float]] = []
    for o in orders:
        report = execute(o, src)
        pf.apply(o, report)
        reports.append(report)
        marks = _marks(store, pf, o.submit_ts)
        equity.append([o.submit_ts, round(pf.equity(marks), 4)])
    final_marks: Dict[Key, float] = {}
    if cfg.mark_mode == "market":
        # TRADE-PnL: never settle to outcome; mark each open position at its LATEST captured
        # book (not the last order ts — a position still held after the final order would
        # otherwise be marked near its entry). book_at(t=+inf) returns the newest snapshot.
        final_marks = _marks(store, pf, 9_999_999_999)
    else:
        for (m, tk) in list(pf.positions.keys()):           # settle resolved markets at end
            res = cfg.resolutions.get((m, tk)) or store.resolution(m, tk)
            if res is not None:
                pf.settle(res)
    if orders:
        equity.append([orders[-1].submit_ts + 1, round(pf.equity(final_marks), 4)])
    return pf, reports, equity, final_marks


def _marks(store: Store, pf: Portfolio, t: int) -> Dict[Key, float]:
    """HONEST liquidation marks: when the as-of book is
    available, an open position is marked at the price a real CLOSE would walk (long -> SELL into bids,
    short -> BUY from asks; remainder at the last walked level), NOT at mid — mid overstates equity on the
    wide/thin books that high-volatility entries face. Exit taker fee is NOT deducted here (category unknown at this
    layer; the spread/depth term dominates). No usable book -> mid fallback."""
    from desk.sim.fill_model import _walk
    marks: Dict[Key, float] = {}
    for (m, tk), pos in pf.positions.items():
        if pos.qty == 0:
            continue
        mark = None
        try:
            d = store.route(m, tk, t)
            book = d.book if d else None
            if book is not None:
                side = "SELL" if pos.qty > 0 else "BUY"
                levels = book.bids if side == "SELL" else book.asks
                raw, _rem = _walk(levels, side, abs(pos.qty),
                                  0.001 if side == "SELL" else 0.999, t)
                if raw:
                    filled = sum(r.qty for r in raw)
                    mark = sum(r.price * r.qty for r in raw) / filled
            if mark is None and d is not None:
                mark = d.mid
        except Exception:
            mark = None
        if mark is None:
            mark = store.mid_at(m, tk, t)
        if mark is not None:
            marks[(m, tk)] = round(mark, 6)
    return marks


def _data_quality(reports: List[ExecReport]) -> dict:
    by_tier: Dict[str, int] = {}
    degraded = 0
    optimistic = 0
    conf_counts: Dict[str, int] = {}      # how reliable are the cost estimates across the run
    total_cost_c = 0.0                     # total inferred exec cost applied (cents), T1+model fills
    worst = None                           # coarsest backoff level any fill had to use
    oow_fills = 0                          # fills priced by the STRUCTURAL out-of-window proxy (April etc.)
    floor_fills = 0                        # fills priced by the live-reference / backstop FLOOR (tiers 4-5)
    insufficient_fills = 0                 # fills priced FREE (no estimate) — the invariant violation; should be 0
    # rank orders the waterfall worst-last so worst_spread_source surfaces the weakest pricing a run leaned on.
    # `insufficient` (a genuinely free 0¢ fill) is the WORST outcome — ranked above the floors, which DO price friction.
    rank = {"market": 0, "entity": 1, "category": 2, "global": 3,
            "market_oow": 4, "entity_oow": 5, "category_oow": 6, "global_oow": 7,
            "live_ref": 8, "live_adjacent": 9, "global_backstop": 10, "insufficient": 11}
    for r in reports:
        by_tier[r.fidelity_tier] = by_tier.get(r.fidelity_tier, 0) + 1
        if r.degraded:
            degraded += 1
        if any("thin-top" in w for w in r.warnings):
            optimistic += 1
        c = r.est_confidence or "n/a"
        conf_counts[c] = conf_counts.get(c, 0) + 1
        if r.cost_est_c:
            total_cost_c += r.cost_est_c * r.filled_qty
        if r.spread_source:
            if r.spread_source.endswith("_oow"):
                oow_fills += 1
            if r.spread_source in ("live_ref", "live_adjacent", "global_backstop"):
                floor_fills += 1
            if r.spread_source == "insufficient":
                insufficient_fills += 1
            if worst is None or rank.get(r.spread_source, 99) > rank.get(worst, 99):
                worst = r.spread_source
    return {"total_fills": len(reports), "degraded_fills": degraded,
            "optimistic_thin_top_fills": optimistic, "by_tier": by_tier,
            "est_confidence": conf_counts, "total_cost_est_c": round(total_cost_c, 4),
            "worst_spread_source": worst, "oow_fills": oow_fills, "floor_fills": floor_fills,
            "insufficient_fills": insufficient_fills}


if __name__ == "__main__":  # `make backtest` — toy strategy smoke run
    import json
    from desk.signals.example_calendar import generate_orders
    store = Store()
    orders = generate_orders(store)
    result = run(orders, RunConfig(store=store, starting_cash=1000.0))
    print(json.dumps({
        "n_orders": len(orders),
        "total_pnl": result.total_pnl,
        "exec_pnl": result.exec_pnl,
        "resolution_pnl": result.resolution_pnl,
        "fill_ratio": result.fill_ratio,
        "bracket": result.bracket,
        "data_quality": result.data_quality,
        "equity_points": len(result.equity_curve),
    }, indent=2))
