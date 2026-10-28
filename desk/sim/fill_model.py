"""sim/fill_model.py — Order + data@t -> raw fills.

Routes to the highest fidelity tier store offers and fills accordingly:
  T5 book  -> DEPTH ladder-walk (deterministic, exact)         [primary]
  T1 mid   -> MID estimate (degraded; flagged)                 [fallback]

Maker orders rest at the limit; whether they fill is judged from real captured
prints (volume traded THROUGH the limit during the validity window) under an
assumption set (latency, queue fill-prob) that the evaluator estimates per market.
No fees here — engine applies fees on top.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from desk.types import Order
from desk.data.store import Store, DataAtT


@dataclass
class Assumptions:
    """Evidence-grounded, per-(market,time) — the evaluator proposes these with rationale.
    Defaults are the neutral 'no extra friction' case for first-pass paper trading."""
    latency_ms: int = 0             # submit -> eligible at the matching engine
    maker_rule: str = "through"     # "touch" (optimistic) | "through" (pessimistic, volume-gated)
    queue_fill_prob: float = 0.5    # maker: our share of through-volume (queue position proxy). BASE=0.5;
                                    # 1.0 exists ONLY in the labeled OPTIMISTIC bracket — a verdict-producing
                                    # backtest must never reach queue_fill_prob=1.0
    # Adversarial bound for thin/stale top-of-book: assume the top N best price levels may
    # already be consumed by match time (parameter-free worst case).
    skip_best_levels: int = 0
    # Base-case FLAG: warn when a consumed best level is thin (size <= mult * min_order_size),
    # since such fills are optimistically biased (the level may be stale). Flag, don't drop.
    thin_top_mult: float = 10.0
    drift_lookback: int = 6         # snapshots for recent mid-drift — the volatile-fill CONFIDENCE flag only
                                    # (temporal COST is now owned by the per-market cost_model, not this)
    # Low-confidence flags (forward backtest must never trust these as a point estimate):
    penny_price: float = 0.05       # below this, one tick = huge % -> price unreliable
    tick_pct_thresh: float = 0.03   # one tick > this fraction of price -> coarse-tick, unreliable
    fresh_secs: float = 8.0         # book older than this -> price may have drifted
    rel_vol_thresh: float = 0.10    # recent drift > this fraction of price -> volatile, unreliable
    impact_thresh: float = 0.005    # fill avg worse than best by > this fraction -> large-order impact
    # ── fidelity-cap + best-estimate cost ────────────────────────────────────────
    # Cap the data tier the fill is allowed to see, so the SAME orders can be replayed
    # on cheaper data to measure backtest drift. None = use the best tier store offers
    # (today's behavior). "T5" | "T1".
    max_tier: Optional[str] = None
    # ── unified execution-cost model band ────────────────────────────────────────
    # When an ExecCostModel is injected, the engine asks it once per fill for {walk_c (spread+depth,
    # used only when there's no real book), temporal_c (adverse drift over the lag)}. cost_band is the
    # ONE knob the bracket sweeps. Without a model injected this is a no-op (golden stays bit-identical).
    cost_band: str = "base"         # "opt" (mid, 0) | "base" (median spread, p75 temporal) | "adv" (p90, p90)
    lag_s: float = 10.0             # central conservative lag (= spread_model.LAG_WINDOW_S); for the warning text


@dataclass
class RawFill:
    ts: int
    price: float
    qty: float


@dataclass
class FillModelResult:
    raw_fills: List[RawFill]
    tier: str
    mid: Optional[float]
    unfilled_qty: float
    degraded: bool = False
    warnings: List[str] = field(default_factory=list)
    # Append-only. Reliability label + inferred-cost band for THIS fill.
    expected_cost_c: Optional[float] = None   # inferred exec cost vs mid actually applied, in cents (T1+model only)
    cost_p90_c: Optional[float] = None        # p90 of that cost, cents — the uncertainty band
    spread_source: Optional[str] = None       # market|peer|category|global|insufficient (which backoff priced it)
    confidence: Optional[str] = None          # exact | estimated | low | none — how much to trust this fill's price
    slippage_c: Optional[float] = None        # per-market temporal slippage applied vs the spread price, cents
    # Append-only (maker queue-ahead). Book depth at-or-better than the resting limit at SUBMIT — the
    # volume that must trade through before our order fills. Maker only; None on taker or missing submit-book.
    queue_ahead: Optional[float] = None


class FillModel:
    def __init__(self, store: Store, assumptions: Optional[Assumptions] = None, cost_model=None):
        self.store = store
        self.a = assumptions or Assumptions()
        self.cost_model = cost_model   # unified ExecCostModel (spread+depth + temporal); None = off

    def _cost(self, order: Order, mid: Optional[float], eff_t: int, include_spread: bool):
        """The ONE call to the unified cost model -> CostBreakdown (or None). include_spread=False on
        the book-walk path (the real walk already crossed the spread — only temporal is added); True on
        the T1 path (no book — add both). cost_band picks the percentile; no model => None (no-op).
        Passes the order's recent VOL (entry-state proxy) so the model conditions on the volatility
        regime — keeping the cost model store-agnostic (fill_model owns the store, computes vol here)."""
        if self.cost_model is None or mid is None:
            return None
        from desk.sim.spread_model import cost_bands
        sb, tb = cost_bands(self.a.cost_band)
        # Prefer the strategy's own entry-state hint (Order.entry_vol, e.g. gap-from-fair) when
        # set: it's a direct recent-volatility magnitude that works on a historical path where
        # _recent_vol can't measure a 600s range from coarse prices-history. Else infer from the series.
        vol = order.entry_vol if order.entry_vol is not None else \
            self._recent_vol(order.market_id, order.token_id, eff_t)
        return self.cost_model.cost(order.market_id, order.token_id, price=mid, size=order.qty,
                                    as_of_ts=eff_t, spread_band=(sb if include_spread else "mid"),
                                    temporal_band=tb, vol=vol)

    def _recent_vol(self, market_id: str, token_id: str, t: int) -> Optional[float]:
        """The order's entry-state volatility = RANGE of mid over the trailing VOL window, matched to
        how the cost model fit its `vol` obs (range = sampling-density robust). Captured book series
        first (T5); else CLOB /prices-history via store.sources (the T1 / pre-capture path).
        None if neither has >=2 points -> the model skips the vol filter (price/size-only estimate)."""
        from desk.sim.spread_model import VOL_WINDOW_S
        t0 = t - VOL_WINDOW_S
        mids: List[float] = []
        try:
            snaps = self.store._load_market_books(market_id).get(token_id, [])
            mids = [s.mid for s in snaps if s.mid and t0 <= s.capture_ts <= t]
        except Exception:
            mids = []
        if len(mids) < 2 and getattr(self.store, "sources", None) is not None:
            try:
                pts = self.store.sources.series(token_id)
                mids = [p[1] for p in pts if t0 <= p[0] <= t]
            except Exception:
                pass
        return (max(mids) - min(mids)) if len(mids) >= 2 else None

    def _recent_mid_drift(self, market_id: str, token_id: str, t: int) -> float:
        """Measured adverse-move size = the largest SINGLE-interval mid move over the last
        `drift_lookback` snapshots up to t. One-interval (not full range) ties it to the
        staleness window and is robust to a single outlier snapshot. 0 if unavailable."""
        try:
            snaps = self.store._load_market_books(market_id).get(token_id, [])
        except Exception:
            return 0.0
        recent = [s.mid for s in snaps if s.capture_ts <= t][-self.a.drift_lookback:]
        if len(recent) < 2:
            return 0.0
        return max(abs(recent[i] - recent[i - 1]) for i in range(1, len(recent)))

    def fills(self, order: Order) -> FillModelResult:
        eff_t = order.submit_ts + int(self.a.latency_ms / 1000)
        d = (_route_capped(self.store, order.market_id, order.token_id, eff_t, self.a.max_tier)
             if self.a.max_tier else self.store.route(order.market_id, order.token_id, eff_t))
        warnings = list(d.warnings)
        if order.mode == "maker":
            return self._maker(order, d, warnings)
        return self._taker(order, d, eff_t, warnings)

    # ── taker: cross the spread, walk the ladder ──────────────────────────────
    def _taker(self, order: Order, d: DataAtT, eff_t: int, warnings: List[str]) -> FillModelResult:
        if d.book is not None:   # T5 self-capture — walk the real book
            full = d.book.asks if order.side == "BUY" else d.book.bids
            skip = self.a.skip_best_levels
            levels = full[skip:] if skip else full
            if skip and full:
                warnings.append(f"adversarial: assumed top {skip} best level(s) consumed by match time")
            raw, remaining = _walk(levels, order.side, order.qty, order.limit_price, eff_t)
            # FOK = fill-or-kill: all-or-nothing. If we can't fully fill, fill nothing.
            if order.tif == "FOK" and remaining > 1e-9:
                warnings.append(f"FOK unfilled: only {order.qty - remaining:.2f}/{order.qty:.2f} available at limit")
                return FillModelResult([], d.tier, d.book.mid, order.qty, d.degraded, warnings)
            # FLAG: optimistic fill against a thin best level that may be stale.
            if not skip and raw and full:
                best_px, best_sz = float(full[0][0]), float(full[0][1])
                thin_thr = self.a.thin_top_mult * (d.book.min_order_size or 0)
                if raw[0].price == best_px and best_sz <= thin_thr:
                    warnings.append(
                        f"thin-top: filled best level {best_px}x{best_sz} "
                        f"(<= {thin_thr:g}); optimistically biased — may be sub-snapshot stale")
            # LOW-CONFIDENCE: where a 5s book can't pin the fill price — penny price, stale book,
            # or high recent volatility (drift large vs price). Forward strategy must not trust these.
            avg = (sum(r.price * r.qty for r in raw) / sum(r.qty for r in raw)) if raw else 0.0
            drift0 = self._recent_mid_drift(order.market_id, order.token_id, eff_t) if raw else 0.0
            if raw:
                why = None
                tick = d.book.tick_size or 0
                if avg < self.a.penny_price:
                    why = "penny price"
                elif tick > 0 and avg > 0 and tick / avg > self.a.tick_pct_thresh:
                    why = f"coarse tick ({tick} = {100*tick/avg:.0f}% of price {avg:.3f}): 1 tick move = large %"
                elif (d.stale_secs or 0) > self.a.fresh_secs:
                    why = f"book {d.stale_secs:.0f}s stale"
                elif avg > 0 and drift0 / avg > self.a.rel_vol_thresh:
                    why = f"volatile (recent drift {drift0:.3f} = {100*drift0/avg:.0f}% of price)"
                else:
                    best_px = float(full[0][0])
                    # any fill that walked PAST the best level has impact uncertainty: our
                    # snapshot's best-level depth may differ from reality at match time.
                    multi_level = len({round(r.price, 6) for r in raw}) > 1
                    impact = best_px > 0 and abs(avg - best_px) / best_px > self.a.impact_thresh
                    if multi_level or impact:
                        why = (f"impact: walked past top of book (avg {avg:.4f} vs best {best_px:.4f}, "
                               f"{len({round(r.price,6) for r in raw})} levels)")
                if why:
                    warnings.append(f"low-confidence fill ({why}): price unreliable at 5s book resolution; size down / don't trust point")
            # TEMPORAL slippage over the lag window (adverse). The unified cost_model owns it; the
            # real walk already crossed the spread (include_spread=False) so there is no double-count.
            slip = 0.0
            cb = self._cost(order, d.book.mid, eff_t, include_spread=False)
            if cb and cb.temporal_c > 0 and raw:
                slip = cb.temporal_c
                sgn = 1.0 if order.side == "BUY" else -1.0
                raw = [RawFill(r.ts, min(0.999, max(0.001, r.price + sgn * slip)), r.qty) for r in raw]
                warnings.append(f"temporal slippage {slip:.4f} over {self.a.lag_s:.0f}s lag (band={self.a.cost_band})")
            # Reliability label: a real ladder-walk is EXACT unless it tripped a low-conf / stale flag.
            conf = "low" if (d.degraded or any(("low-confidence" in w or "thin-top" in w) for w in warnings)) else "exact"
            return FillModelResult(raw, d.tier, d.book.mid, remaining, d.degraded, warnings, confidence=conf,
                                   slippage_c=round(slip * 100.0, 4) if slip else None)
        if d.tier == "T1" and d.mid is not None:
            return self._taker_t1(order, d, eff_t, warnings)
        return FillModelResult([], d.tier, d.mid, order.qty, True, warnings, confidence="none")

    # ── T1 (mid only): no ladder. Fill at mid ± (inferred spread + temporal) from the cost_model. ──
    def _taker_t1(self, order: Order, d: DataAtT, eff_t: int, warnings: List[str]) -> FillModelResult:
        mid = d.mid
        # COST FAIL-CLOSED: a T1 estimate with NO vol/regime signal must not silently fall back to the
        # un-conditioned tier (~5c) on books that can be far wider. When a cost model is engaged, a market
        # whose regime is unknowable (no Order.entry_vol hint AND the series too coarse for _recent_vol)
        # is SKIPPED, never priced on the cheap tier.
        if self.cost_model is not None:
            _vol = order.entry_vol if order.entry_vol is not None else \
                self._recent_vol(order.market_id, order.token_id, eff_t)
            if _vol is None:
                warnings.append("cost fail-closed: no vol/regime signal for the cost model "
                                "(set Order.entry_vol) — market SKIPPED, not priced on the calm tier")
                return FillModelResult([], "T1", mid, order.qty, True, warnings, confidence="none")
        cb = self._cost(order, mid, eff_t, include_spread=True)   # no real book -> add BOTH components
        cost = cb.walk_c if cb else 0.0          # inferred spread+depth (0 = today's naive-mid behavior)
        p90 = cb.walk_p90_c if cb else 0.0
        slip = cb.temporal_c if cb else 0.0      # inferred temporal drift over the lag
        src = cb.source if cb else None
        conf = "estimated" if src in ("market", "entity", "category") else "low"
        # BUY pays mid + (spread + temporal); SELL receives mid - (spread + temporal). Both flow into PnL.
        sign = 1.0 if order.side == "BUY" else -1.0
        eff_price = min(0.999, max(0.001, mid + sign * (cost + slip)))
        ok = (order.side == "BUY" and eff_price <= order.limit_price) or \
             (order.side == "SELL" and eff_price >= order.limit_price)
        raw = [RawFill(eff_t, round(eff_price, 6), order.qty)] if ok else []
        unfilled = 0.0 if ok else order.qty
        if cost > 0 or slip > 0:
            warnings.append(f"T1 fill priced by cost_model: mid {mid:.4f} {'+' if sign > 0 else '-'} "
                            f"(spread {cost:.4f} + temporal {slip:.4f}) [band={self.a.cost_band}, src={src}] -> {eff_price:.4f}")
        else:
            warnings.append("T1 mid fill (no cost_model): execution cost NOT priced — optimistic estimate")
        return FillModelResult(
            raw, "T1", mid, unfilled, True, warnings,
            expected_cost_c=round(cost * 100.0, 4),
            slippage_c=round(slip * 100.0, 4) if slip else None,
            cost_p90_c=(round(p90 * 100.0, 4) if p90 else None),
            spread_source=src, confidence=conf)

    # ── maker: rest at limit; fill judged from real through-volume + queue-ahead ─
    def _maker(self, order: Order, d: DataAtT, warnings: List[str]) -> FillModelResult:
        """Queue-ahead semantics. We rest at the limit BEHIND everything already resting at-or-better
        than us; only after that queue clears do our shares fill, ALL-OR-NOTHING:

          queue_ahead = book depth at-or-better than the limit AT SUBMIT
                        (SELL: ask levels px <= limit; BUY: bid levels px >= limit)
          through_vol = counterparty prints at/through the limit in [submit, min(expiry, now)]

          FILL RULE (all-or-nothing, at the limit):
            maker_rule=="touch":   fill iff through_vol                     >= queue_ahead + qty
            maker_rule=="through": fill iff through_vol * queue_fill_prob   >= queue_ahead + qty
          (queue_fill_prob is a CONSERVATIVE share-of-flow haircut in the pessimistic 'through' path only;
           it is NOT added to the threshold — through_vol/queue_fill_prob would be wrong math.)

        FAIL-CLOSED: no submit-book ⇒ queue_ahead is UNKNOWN ⇒ leave the order unfilled with a warning
        (never assume front-of-queue, which would be the optimistic all-flow-fills-us case)."""
        submit_d = self.store.route(order.market_id, order.token_id, order.submit_ts)
        if submit_d.book is None:
            warnings.append("maker fail-closed: no book at submit — queue_ahead unknown; "
                            "unfilled (never assume front-of-queue)")
            return FillModelResult([], d.tier, d.mid, order.qty,
                                   True, warnings, queue_ahead=None)
        b = submit_d.book
        levels = b.asks if order.side == "SELL" else b.bids
        queue_ahead = _depth_at_or_better(levels, order.limit_price, order.side)

        t1 = order.expiry_ts or order.submit_ts
        prints = self.store.prints_between(order.market_id, order.token_id, order.submit_ts, t1)
        # A counterparty trading at/through our limit could fill our resting order.
        relevant = [p for p in prints
                    if (order.side == "BUY" and p.price <= order.limit_price)
                    or (order.side == "SELL" and p.price >= order.limit_price)]
        through_vol = sum(p.size for p in relevant)
        # "through" applies the queue_fill_prob haircut as a conservative share of the flow (NOT a threshold
        # inflation). "touch" is the optimistic no-haircut bracket.
        effective = through_vol if self.a.maker_rule == "touch" else through_vol * self.a.queue_fill_prob
        filled = effective >= queue_ahead + order.qty
        if not filled:
            warnings.append(
                f"maker unfilled: through_vol {through_vol:.1f}"
                f"{'' if self.a.maker_rule == 'touch' else f'*{self.a.queue_fill_prob:g}={effective:.1f}'} "
                f"< queue_ahead {queue_ahead:.1f} + qty {order.qty:.1f}")
            return FillModelResult([], d.tier, d.mid, order.qty,
                                   d.degraded, warnings, queue_ahead=round(queue_ahead, 4))
        fill_ts = relevant[-1].ts if relevant else t1   # the print that cleared the queue+our qty
        raw = [RawFill(fill_ts, order.limit_price, round(order.qty, 4))]
        if d.tier != "T5":
            warnings.append("maker fill judged on prints without recorded book — approximate")
        return FillModelResult(raw, d.tier, d.mid, 0.0,
                               d.degraded or d.tier != "T5", warnings,
                               queue_ahead=round(queue_ahead, 4))


def _route_capped(store: Store, market_id: str, token_id: str, t: int, max_tier: str) -> DataAtT:
    """Fidelity cap — return data as if the best tier available were `max_tier`, ignoring
    anything richer, so the SAME orders can be replayed on cheaper data to measure backtest
    drift. Reuses store's public accessors (mid_at) and mirrors route()'s degraded/staleness
    stamping; it does NOT modify store.route().
      "T5"/None -> normal route()        "T1" -> mid only (no ladder)
    """
    if max_tier in (None, "T5"):
        return store.route(market_id, token_id, t)
    d = DataAtT(market_id=market_id, token_id=token_id, t=t, tier="NONE")
    mid = store.mid_at(market_id, token_id, t)
    if mid is not None:
        d.tier, d.mid, d.degraded = "T1", mid, True
        d.warnings.append(f"fidelity-cap {max_tier}: mid-only (no ladder) — fill is an ESTIMATE, not a ladder-walk")
        return d
    d.degraded = True
    d.warnings.append("fidelity-cap: no data at t for (market,token) — cannot fill")
    return d


def _depth_at_or_better(levels, limit: float, side: str) -> float:
    """Total resting size that fills BEFORE our resting order at `limit` (= queue_ahead).
      SELL joins the ask: ALL ask size priced at level <= limit is ahead of us.
      BUY  joins the bid: ALL bid size priced at level >= limit is ahead of us.
    Level = [price, size]; also tolerates {price,size} dicts (vendor books)."""
    tot = 0.0
    for lv in levels or []:
        px = lv.get("price") if isinstance(lv, dict) else lv[0]
        sz = lv.get("size") if isinstance(lv, dict) else lv[1]
        if px is None or sz is None:
            continue
        if side == "SELL" and float(px) <= limit + 1e-9:
            tot += float(sz)
        elif side == "BUY" and float(px) >= limit - 1e-9:
            tot += float(sz)
    return tot


def _walk(levels, side: str, qty: float, limit: float, ts: int) -> tuple[List[RawFill], float]:
    """Consume ladder levels until qty filled or the limit is crossed."""
    out: List[RawFill] = []
    remaining = qty
    for lvl in levels or []:
        price, size = float(lvl[0]), float(lvl[1])
        if side == "BUY" and price > limit:
            break
        if side == "SELL" and price < limit:
            break
        take = min(remaining, size)
        if take <= 0:
            continue
        out.append(RawFill(ts, price, round(take, 4)))
        remaining -= take
        if remaining <= 1e-9:
            remaining = 0.0
            break
    return out, round(remaining, 4)
