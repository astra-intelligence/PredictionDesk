"""sim/spread_model.py — best-estimate execution-cost model.

THE PROBLEM it solves: a T1 (mid-only) fill has no order book, so the engine used to fill
it FREE at mid — optimistic. This model gives the engine a *best estimate* of what crossing
the spread would really cost, fit from our live T5 captures, so a low-fidelity backtest fill
carries honest (often negative) PnL instead of a fabricated zero cost.

WHAT it measures (per captured snapshot, reusing the engine's own `_walk`): the per-share
cost of a marketable order of N shares vs mid, averaged over both sides — i.e. half-spread
for tiny N, plus depth/impact for larger N. Stored for sizes (1,5,10,25).

HOW it generalises (a market's own live spread prices its uncaptured history; a market on a
date we didn't capture borrows a nearby-date peer): a hierarchical backoff
  market (same token's own history)  ->  entity (peer key, e.g. same event family)
  ->  category  ->  global,
each conditioned on PRICE LEVEL (spread varies strongly with p), softly on time-to-
resolution (lead), and softly on the order's own VOLATILITY REGIME (calm/normal/dislocated;
the entry-state proxy — see below). It returns the median / p75 / p90 of the cost so the
runner's bracket becomes opt=mid · base=median · adv=p90 instead of a flat haircut.

THE WATERFALL — "best available data source, NEVER a free fill" (user principle). Per fill
at (market, token, t) the crossing cost is sourced top-down, *labelling which tier was used*:
  SPREAD + DEPTH
  1. exact       — real book@t (store.route T5 self-capture). Walked, not estimated.
                   (Owned by fill_model; this module is only asked tiers 2-4.)
  2. estimated   — `estimate(as_of_ts=t)`, TIME-HONEST: only obs with capture_ts < t. In-window
                   dates where peers were captured. market->entity->category->global.
  3. <lvl>_oow   — STRUCTURAL out-of-window: order predates our captures entirely -> retry with
                   as_of_ts=None (all-time obs), conditioned on the order's OWN price-level + size +
                   vol regime; only the relationship is borrowed. `low` conf.
  4. live_ref /  — LIVE-REFERENCE FLOOR (rare; only if even global_oow is insufficient): a REAL
     live_adjacent  current spread, never a fabricated/static number. `live_ref` = THIS market's live
                   Gamma bestBid/bestAsk; `live_adjacent` = an LLM-picked comparable LIVE market's
                   spread when this one is closed (else the pool-wide `_pool` floor — any market).
                   Precomputed to findings/live_spread_refs.json + auto-loaded (load(live_refs_path=...))
                   so the API/LLM never runs in the fill loop. Lowest conf.
  5. global_      — CODE BACKSTOP: the all-price-bucket global median from our captures. ENFORCES the
     backstop      never-0 invariant WITHOUT any external file (fires when refs are absent and a price
                   region was never captured) — coarse but a real measured number. Only `insufficient`
                   (no captures AT ALL — impossible in practice) and band='mid' can yield 0.
  TEMPORAL SLIPPAGE (the lag-window drift, estimate_slippage): same tiers 2 (estimated) / 3 (_oow);
  0 only where the bucket is genuinely flat (extreme/illiquid prices legitimately don't move).
A spread fill is therefore NEVER priced at a free 0¢ (the one outcome this model exists to prevent);
structural/floor reliance is surfaced via spread_source/est_confidence/{oow,floor,insufficient}_fills
in runner._data_quality.

VOL-REGIME CONDITIONING: spread is keyed not just on (price, size) but also on the order's recent
mid-move VOLATILITY (range over a trailing window) — the strategy's fills are a biased sample (it
enters on wide/thin books), so the price-bucket average under-prices them. `vol` is a SOFT filter
(calm/normal/dislocated regime, edges in meta) over obs within the bucket — like the `lead` filter,
widening to all-vol if too few match. Observable for any date (via prices-history), so a high-volatility
entry gets the wide-book cost, a calm entry the calm cost. Range (max-min) is the proxy because it is
sampling-density-robust: the dense book fit and the ~1-min prices-history estimate compare.

TIME-HONEST: every stored observation keeps its capture_ts; `estimate(as_of_ts=...)` only uses
observations strictly BEFORE the order — a May fill never borrows a June spread (tier 2); the
structural _oow fallback (tier 3) is a friction estimate, never tradeable signal lookahead.

This module owns no strategy logic and makes no trade decision; it only prices a fill.
"""
from __future__ import annotations

import glob
import json
import os
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import pyarrow.parquet as pq

from desk.types import BookSnapshot
from desk.sim.fill_model import _walk

_BOOK_FIELDS = set(BookSnapshot.__dataclass_fields__)  # type: ignore[attr-defined]


def _sample_market_books(capture_dir: str, market: str, max_files: int = 4) -> Dict[str, List[BookSnapshot]]:
    """Light loader for FITTING: read only a few (evenly-spaced) hourly parquet files per market
    instead of every one. Each file holds many write-on-change rows, so a handful spans the
    session and is plenty to sample spreads from — ~30x faster than store._load_market_books()
    which hydrates the whole market (needed for the as-of replay, not for fitting)."""
    files = sorted(glob.glob(os.path.join(capture_dir, "*", market, "book_snapshots", "*.parquet")))
    if not files:
        return {}
    by_tok: Dict[str, List[BookSnapshot]] = {}
    for path in _even_sample(files, max_files):
        try:
            rows = pq.read_table(path).to_pylist()
        except Exception:
            continue
        for r in rows:
            snap = BookSnapshot(**{k: v for k, v in r.items() if k in _BOOK_FIELDS})
            by_tok.setdefault(snap.token_id, []).append(snap)
    for snaps in by_tok.values():
        snaps.sort(key=lambda s: s.capture_ts)
    return by_tok

from desk.runtime.paths import data_path as _data_path

# env-resolved (DESK_DATA_DIR) with the historical in-package location as the default target
JSON_PATH = _data_path("sim", "spread_model.json")
# Canonical live-reference-floor artifact (eval/recon/live_spread_refs.py output). load() auto-loads
# it when present so the tier-4 floor is wired by default (not dead unless every caller remembers).
DEFAULT_LIVE_REFS_PATH = os.path.join(os.path.dirname(__file__), "..", "eval", "recon", "findings",
                                      "live_spread_refs.json")

# Price-level buckets, fine near 0/1 — penny/longshot markets are exactly where spread bites.
_PRICE_EDGES = [0.0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 0.20, 0.35, 0.50,
                0.65, 0.80, 0.88, 0.92, 0.95, 0.97, 0.98, 0.99, 1.0]
_SIZES = (1.0, 5.0, 10.0, 25.0)        # walk-cost is stored per these share sizes
LEAD_BAND_D = 1.5                       # soft lead filter half-width (days); ignored if too few obs
N_MIN = 20                             # min obs (after time/lead/vol filter) for a backoff level to be usable
P90_FLOOR_MULT = 1.5                   # p90 = max(p90, mult*median): a sparse-lucky tail can't understate cost
LAG_WINDOW_S = 10.0                    # central lag the temporal-slippage `wmove` is measured over (>= ~6s system gap)
VOL_WINDOW_S = 600.0                   # trailing window the entry-state vol proxy (mid range) is measured over
# obs row layout: [capture_ts, lead_days(-1 if unknown), wc(size1), wc(size5), wc(size10), wc(size25), wmove(-1 if n/a), vol(-1 if n/a)]
# wmove = |mid(t+LAG_WINDOW_S) - mid(t)| from the token's own series: the typical ADVERSE move over the lag window.
# vol   = max(mid) - min(mid) over the trailing VOL_WINDOW_S: the recent-volatility / entry-state proxy (range = sampling-robust).
_WC0 = 2
_DRIFT_COL = 6
_VOL_COL = 7


def _price_bucket(p: float) -> int:
    for i in range(len(_PRICE_EDGES) - 1):
        if p < _PRICE_EDGES[i + 1]:
            return i
    return len(_PRICE_EDGES) - 2


def _pct(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    return sorted_vals[int(q * (len(sorted_vals) - 1))]


def _vol_edges(vols: List[float]) -> Optional[List[float]]:
    """calm/normal/dislocated regime edges = (p50, p85) of the NONZERO vol distribution.
    None if there isn't a meaningful spread of vols (then the vol soft-filter is a no-op)."""
    nz = sorted(v for v in vols if v is not None and v > 0)
    if len(nz) < 100:
        return None
    lo, hi = _pct(nz, 0.50), _pct(nz, 0.85)
    return [round(lo, 6), round(hi, 6)] if hi > lo > 0 else None


def _regime_of(v: Optional[float], edges) -> Optional[int]:
    """Vol regime 0 calm / 1 normal / 2 dislocated by the meta vol_edges; None disables the filter
    (no edges fit, or the order's vol is unknown). The SAME edges classify fit-obs and the query."""
    if not edges or v is None or v < 0:
        return None
    return 0 if v < edges[0] else (2 if v >= edges[1] else 1)


def _walk_cost(bs, size: float) -> Optional[float]:
    """Per-share cost of a marketable `size`-share order vs mid, averaged over both sides.
    Reuses the engine's exact ladder-walk so the fit matches how fills are actually priced."""
    mid = bs.mid
    if not mid or mid <= 0:
        return None
    out: List[float] = []
    for side, levels in (("BUY", bs.asks), ("SELL", bs.bids)):
        raw, rem = _walk(levels, side, size, 0.999 if side == "BUY" else 0.001, 0)
        filled = size - rem
        if filled <= 1e-9:
            continue
        vwap = sum(r.price * r.qty for r in raw) / filled
        out.append(abs(vwap - mid))
    return (sum(out) / len(out)) if out else None


@dataclass
class SpreadEst:
    cost: float       # price units, at the requested band (the cost actually applied to the fill)
    p90: float        # price units — the adverse end of the band
    median: float     # price units — the base
    source: str       # market | entity | category | global | insufficient | mid
    n: int            # obs the estimate was computed from


@dataclass
class CostBreakdown:
    """The unified per-fill execution-cost estimate the engine asks for ONCE (price units).
    walk_c = spread + depth (size-aware crossing cost — used only when there's no real book);
    temporal_c = adverse drift over the lag window. Distinct owners, never double-counted."""
    walk_c: float = 0.0
    walk_p90_c: float = 0.0
    temporal_c: float = 0.0
    source: Optional[str] = None


# cost_band -> (spread_band, temporal_band). One knob the bracket sweeps: opt(0) / base / adv(p90).
_COST_BANDS = {"opt": ("mid", "none"), "base": ("median", "p75"), "adv": ("p90", "p90")}


def cost_bands(cost_band: str) -> tuple:
    return _COST_BANDS.get(cost_band, _COST_BANDS["base"])


class ExecCostModel:
    """The Desk's one execution-cost model: spread+depth (estimate) AND temporal slippage
    (estimate_slippage) from the SAME fitted artifact, asked together via cost(). See module docstring.

    peer_key(market_id) -> entity key (e.g. same event family) and category_of(market_id) -> category
    are INJECTED so this stays strategy-agnostic; both optional (level skipped if absent)."""

    def __init__(self, levels: Dict, peer_key: Optional[Callable] = None,
                 category_of: Optional[Callable] = None, meta: Optional[dict] = None,
                 calibration: Optional[dict] = None, live_refs: Optional[dict] = None):
        self.levels = levels                  # {level: {key: {price_bucket_str: [obs,...]}}}
        self.peer_key = peer_key
        self.category_of = category_of
        self.meta = meta or {}
        # Optional reality-calibration from eval/recon/slippage_calibration.py: per-(price_bucket,
        # size) realized/model multiplier earned from real fills. None/{} -> identity (book-fit only).
        self.calibration = calibration or {}
        # Optional live-reference floor (waterfall tier 4) from eval/recon/live_spread_refs.py:
        # {market_id: {half_spread, source}, "_by_category": {cat: {half_spread, source}}}. A REAL
        # current spread (or LLM-adjacent live market's), used ONLY when even global_oow is insufficient.
        self.live_refs = live_refs or {}

    # ── lookup ────────────────────────────────────────────────────────────────
    @classmethod
    def load(cls, path: str = JSON_PATH, peer_key=None, category_of=None,
             calibration_path: Optional[str] = None,
             live_refs_path: Optional[str] = None) -> Optional["SpreadModel"]:
        """Load a fitted model from JSON. Returns None if the artifact is absent (the cost model
        ships un-fit — regenerate via slippage_calibration on own captures; until then the engine
        runs with cost_model=None and the never-0 waterfall backstop is simply not engaged)."""
        if not os.path.exists(path):
            return None
        with open(path) as f:
            blob = json.load(f)
        calib = None
        if calibration_path and os.path.exists(calibration_path):
            with open(calibration_path) as f:
                calib = (json.load(f) or {}).get("calibration") or {}
        # Auto-wire the tier-4 floor from the canonical artifact when no explicit path is given
        # (pass live_refs_path="" to opt out). golden never injects a cost_model, so it's unaffected.
        if live_refs_path is None and os.path.exists(DEFAULT_LIVE_REFS_PATH):
            live_refs_path = DEFAULT_LIVE_REFS_PATH
        live_refs = None
        if live_refs_path and os.path.exists(live_refs_path):
            with open(live_refs_path) as f:
                live_refs = json.load(f) or {}
        return cls(blob["levels"], peer_key, category_of, blob.get("meta"), calib, live_refs)

    def estimate(self, market_id: str, token_id: str, price: float, size: float = 1.0,
                 lead: Optional[float] = None, as_of_ts: Optional[int] = None,
                 band: str = "median", vol: Optional[float] = None) -> Optional[SpreadEst]:
        """Best-estimate per-share crossing cost (price units) — the waterfall (see module docstring).
        Tier 2 `estimated`: time-honest vs `as_of_ts` FIRST (no lookahead). Tier 3 `<lvl>_oow`: if that
        yields NO data — e.g. orders predating our captures entirely — retry with all-time obs
        (STRUCTURAL), tagged `_oow` so it's never confused with a contemporaneous fit.
        Tier 4 `live_ref`/`live_adjacent`: if even global_oow is insufficient, a REAL live-reference
        spread (never 0¢/static). Tier 5 `global_backstop`: the code-guaranteed never-0 floor (all-bucket
        global median) when no refs are loaded — so the invariant holds without any external file.
        `vol` (the order's recent mid-range) soft-conditions tiers 2-3 on the entry's volatility regime
        so a high-volatility entry is charged the wide-book cost, not the average."""
        r = self._estimate_impl(market_id, token_id, price, size, lead, as_of_ts, band, vol)
        if as_of_ts is not None and r.source == "insufficient":
            r = self._estimate_impl(market_id, token_id, price, size, lead, None, band, vol)
            if r.source not in ("insufficient", "mid"):
                r.source += "_oow"
        if r.source == "insufficient" and band != "mid":
            lr = self._live_ref(market_id)                  # tier 4: live-reference floor (if refs loaded)
            if lr is not None:
                return lr
            bs = self._backstop(size, band, vol)            # tier 5: code-guaranteed never-0 backstop
            if bs is not None:
                return bs
        return r

    def _live_ref(self, market_id: str) -> Optional[SpreadEst]:
        """Waterfall tier 4: the precomputed live-reference half-spread for this market (or its
        category, or the pool-wide floor), tagged `live_ref` (this market's own live book) /
        `live_adjacent` (an LLM-picked comparable live market when this one is closed). Lowest
        confidence; never 0 / static."""
        if not self.live_refs:
            return None
        e = self.live_refs.get(market_id)
        by_cat = self.live_refs.get("_by_category") or {}
        if e is None and self.category_of:
            e = by_cat.get(self.category_of(market_id))
        if e is None:                                       # last: pool-wide floor (any market)
            e = by_cat.get("_pool")
        if not isinstance(e, dict):
            return None
        hs = float(e.get("half_spread", 0.0) or 0.0)
        if hs <= 0:
            return None
        src = e.get("source", "live_ref")
        return SpreadEst(cost=round(hs, 6), p90=round(hs * P90_FLOOR_MULT, 6),
                         median=round(hs, 6), source=src, n=1)

    def _backstop(self, size: float, band: str, vol: Optional[float] = None) -> Optional[SpreadEst]:
        """Waterfall tier 5 — the code-guaranteed never-0 floor that ENFORCES the invariant without
        any external file: the all-PRICE-BUCKET global median crossing cost from our own captures.
        It drops only the price-level conditioning (the coarsest real estimate), so it returns a
        non-zero measured number whenever ANY captures exist — never a fabricated/static value. Fires
        only when every bucketed backoff AND the live-ref floor were insufficient (a sparse rebuild /
        a never-captured price region). vol soft-filters if it still leaves >=N_MIN obs."""
        si = min(range(len(_SIZES)), key=lambda i: abs(_SIZES[i] - size))
        col = _WC0 + si
        g = self.levels.get("global", {}).get("ALL", {})
        rows = [r for bucket in g.values() for r in bucket]
        if len(rows) < N_MIN:
            return None
        qreg = _regime_of(vol, self.meta.get("vol_edges"))
        vals = [r[col] for r in rows
                if not (qreg is not None and len(r) > _VOL_COL and r[_VOL_COL] >= 0
                        and _regime_of(r[_VOL_COL], self.meta.get("vol_edges")) != qreg)]
        if len(vals) < N_MIN:                               # vol filter too tight -> use all-vol
            vals = [r[col] for r in rows]
        vals.sort()
        med, p90 = _pct(vals, 0.5), _pct(vals, 0.90)
        p90 = max(p90, P90_FLOOR_MULT * med)
        cost = {"median": med, "p75": _pct(vals, 0.75), "p90": p90}.get(band, med)
        return SpreadEst(cost=round(cost, 6), p90=round(p90, 6), median=round(med, 6),
                         source="global_backstop", n=len(vals))

    def _estimate_impl(self, market_id: str, token_id: str, price: float, size: float = 1.0,
                       lead: Optional[float] = None, as_of_ts: Optional[int] = None,
                       band: str = "median", vol: Optional[float] = None) -> Optional[SpreadEst]:
        if band == "mid":
            return SpreadEst(0.0, 0.0, 0.0, "mid", 0)
        bi = str(_price_bucket(price))
        si = min(range(len(_SIZES)), key=lambda i: abs(_SIZES[i] - size))  # nearest size column
        col = _WC0 + si
        qreg = _regime_of(vol, self.meta.get("vol_edges"))   # the order's vol regime (None -> no vol filter)
        cands = [
            ("market", token_id),
            ("entity", self.peer_key(market_id) if self.peer_key else None),
            ("category", self.category_of(market_id) if self.category_of else None),
            ("global", "ALL"),
        ]
        for level, key in cands:
            if key is None:
                continue
            rows = self.levels.get(level, {}).get(key, {}).get(bi)
            if not rows:
                continue
            def _gather(use_lead: bool, use_vol: bool) -> List[float]:
                vals = []
                for r in rows:
                    if as_of_ts is not None and r[0] >= as_of_ts:
                        continue
                    if use_lead and lead is not None and r[1] >= 0 and abs(r[1] - lead) > LEAD_BAND_D:
                        continue
                    if (use_vol and qreg is not None and len(r) > _VOL_COL and r[_VOL_COL] >= 0
                            and _regime_of(r[_VOL_COL], self.meta.get("vol_edges")) != qreg):
                        continue
                    vals.append(r[col])
                return vals
            # Prefer the most-conditioned obs set with >=N_MIN; vol (entry-state) is kept longer than lead.
            wcs: List[float] = []
            for ul, uv in ((True, True), (False, True), (True, False), (False, False)):
                wcs = _gather(ul, uv)
                if len(wcs) >= N_MIN:
                    break
            if len(wcs) < N_MIN:
                continue
            wcs.sort()
            med, p75, p90 = _pct(wcs, 0.5), _pct(wcs, 0.75), _pct(wcs, 0.90)
            p90 = max(p90, P90_FLOOR_MULT * med)
            cost = {"median": med, "p75": p75, "p90": p90}[band]
            # Reality calibration (optional): scale the book-fit cost to match REAL taker cost
            # measured by slippage_calibration.py for this (price,size) cell. 1.0 if uncalibrated.
            # CLAMPED to [0.25, 4.0]: tiny-sample fits produced implausible multipliers (e.g. a
            # near-empty cell yielding a 200x cost reduction) — cost-tuning by the back door.
            # Outside the band => treat as uncalibrated, never scale.
            mult = self.calibration.get(bi, {}).get(str(int(_SIZES[si])), 1.0)
            if not (0.25 <= mult <= 4.0):
                mult = 1.0
            if mult != 1.0:
                cost, p90, med = cost * mult, p90 * mult, med * mult
            return SpreadEst(cost=round(cost, 6), p90=round(p90, 6), median=round(med, 6),
                             source=level, n=len(wcs))
        return SpreadEst(0.0, 0.0, 0.0, "insufficient", 0)

    def estimate_slippage(self, market_id: str, token_id: str, price: float, size: float = 1.0,
                          as_of_ts: Optional[int] = None, band: str = "p75",
                          vol: Optional[float] = None) -> SpreadEst:
        """Per-market TEMPORAL slippage (price units, >=0): the typical ADVERSE mid move over the
        central lag window (LAG_WINDOW_S), backed off market->entity->category->global. Time-honest
        vs as_of FIRST; if that yields no data (order predates captures) fall back to the all-time
        structural estimate, tagged `_oow` — not 0. `vol` soft-conditions on the entry's volatility
        regime (a high-volatility entry drifts more). DISTINCT from estimate() (which owns spread+depth)."""
        r = self._estimate_slip_impl(market_id, token_id, price, size, as_of_ts, band, vol)
        if as_of_ts is not None and r.source == "insufficient":
            r = self._estimate_slip_impl(market_id, token_id, price, size, None, band, vol)
            if r.source not in ("insufficient", "none"):
                r.source += "_oow"
        return r

    def _estimate_slip_impl(self, market_id: str, token_id: str, price: float, size: float = 1.0,
                            as_of_ts: Optional[int] = None, band: str = "p75",
                            vol: Optional[float] = None) -> SpreadEst:
        if band in ("none", "mid"):
            return SpreadEst(0.0, 0.0, 0.0, "none", 0)
        bi = str(_price_bucket(price))
        qreg = _regime_of(vol, self.meta.get("vol_edges"))
        cands = [("market", token_id),
                 ("entity", self.peer_key(market_id) if self.peer_key else None),
                 ("category", self.category_of(market_id) if self.category_of else None),
                 ("global", "ALL")]
        for level, key in cands:
            if key is None:
                continue
            rows = self.levels.get(level, {}).get(key, {}).get(bi)
            if not rows:
                continue
            def _gather(use_vol: bool) -> List[float]:
                return [r[_DRIFT_COL] for r in rows
                        if len(r) > _DRIFT_COL and r[_DRIFT_COL] >= 0
                        and (as_of_ts is None or r[0] < as_of_ts)
                        and not (use_vol and qreg is not None and len(r) > _VOL_COL and r[_VOL_COL] >= 0
                                 and _regime_of(r[_VOL_COL], self.meta.get("vol_edges")) != qreg)]
            vals = _gather(use_vol=True)
            if len(vals) < N_MIN:                # vol filter too tight -> drop it
                vals = _gather(use_vol=False)
            if len(vals) < N_MIN:
                continue
            vals.sort()
            med, p75, p90 = _pct(vals, 0.5), _pct(vals, 0.75), _pct(vals, 0.90)
            slip = {"median": med, "p75": p75, "p90": p90}[band]
            return SpreadEst(cost=round(slip, 6), p90=round(p90, 6), median=round(med, 6),
                             source=level, n=len(vals))
        return SpreadEst(0.0, 0.0, 0.0, "insufficient", 0)

    def cost(self, market_id: str, token_id: str, price: float, size: float = 1.0,
             as_of_ts: Optional[int] = None, spread_band: str = "median",
             temporal_band: str = "p75", vol: Optional[float] = None) -> CostBreakdown:
        """The ONE call the engine makes per fill: spread+depth walk-cost AND temporal slippage,
        labeled, from the same fitted model. Pass spread_band='mid' on the book-walk path (the real
        ladder already crossed the spread) so only the temporal term is added — no double-count.
        `vol` (the order's recent mid-range, supplied by fill_model) soft-conditions both on the
        entry's volatility regime; None falls back to the price/size-only estimate (no vol filter)."""
        sp = (self.estimate(market_id, token_id, price, size=size, as_of_ts=as_of_ts, band=spread_band, vol=vol)
              if spread_band not in ("mid", "none") else None)
        tp = (self.estimate_slippage(market_id, token_id, price, size=size, as_of_ts=as_of_ts, band=temporal_band, vol=vol)
              if temporal_band not in ("none", "mid") else None)
        return CostBreakdown(
            walk_c=round(sp.cost, 6) if sp else 0.0,
            walk_p90_c=round(sp.p90, 6) if sp else 0.0,
            temporal_c=round(tp.cost, 6) if (tp and tp.cost > 0) else 0.0,
            source=(sp.source if sp else None) or (tp.source if tp else None))

    # ── validation (R5): predicted cost vs the snapshot's own actual ladder walk ──
    def validate(self, store, dates: List[str], sizes=(1.0, 10.0), max_markets: int = 60,
                 per_token: int = 40) -> dict:
        import statistics
        errs: Dict[float, List[float]] = {s: [] for s in sizes}
        mkts = sorted({p.split("/")[-2] for d in dates
                       for p in glob.glob(os.path.join(store.capture_dir, d, "*", "book_snapshots"))})
        for m in mkts[:max_markets]:
            for tk, snaps in _sample_market_books(store.capture_dir, m, max_files=4).items():
                snaps = [s for s in snaps if s.bids and s.asks and s.best_ask >= s.best_bid]
                for bs in _even_sample(snaps, per_token):
                    for size in sizes:
                        actual = _walk_cost(bs, size)
                        if actual is None:
                            continue
                        est = self.estimate(m, tk, bs.mid, size=size, as_of_ts=bs.capture_ts + 1, band="median")
                        if est and est.source not in ("insufficient", "mid"):
                            errs[size].append(abs(est.cost - actual))
        out = {}
        for s, e in errs.items():
            e.sort()
            out[f"size{int(s)}"] = {"n": len(e),
                                    "median_abs_err": round(statistics.median(e), 5) if e else None,
                                    "p90_abs_err": round(_pct(e, 0.9), 5) if e else None}
        return out


SpreadModel = ExecCostModel   # back-compat alias (recon harnesses import SpreadModel)


def _even_sample(seq: List, cap: int) -> List:
    if cap <= 0 or len(seq) <= cap:
        return list(seq)
    step = len(seq) / cap
    return [seq[int(i * step)] for i in range(cap)]


# ── build from the live T5 captures ──────────────────────────────────────────
def build(store=None, capture_dir: Optional[str] = None, peer_key=None, category_of=None,
          per_token: int = 40, max_files: int = 4, max_markets: Optional[int] = None,
          priority_markets=None, bucket_caps: Optional[dict] = None, log: bool = True) -> SpreadModel:
    """Fit the model from capture/*/*/book_snapshots/*.parquet. Per token, evenly sample
    `per_token` snapshots, compute walk-cost for each size, and append (time-stamped) to the
    market/entity/category/global buckets. Bucket lists are capped (even-sampled) to bound size.
    priority_markets are processed FIRST (so market-level data for anchors is guaranteed even
    under a max_markets cap)."""
    from desk.data.store import Store
    store = store or Store(capture_dir=capture_dir)
    caps = bucket_caps or {"market": 40, "entity": 150, "category": 2000, "global": 4000}
    levels: Dict[str, Dict[str, Dict[str, List]]] = {"market": {}, "entity": {}, "category": {}, "global": {}}

    def _add(level, key, bi, obs):
        if key is None:
            return
        levels[level].setdefault(key, {}).setdefault(bi, []).append(obs)

    universe = store.universe()
    all_markets = sorted({m for (m, _tk, *_r) in universe})
    pri = [m for m in (priority_markets or []) if m in set(all_markets)]
    markets = pri + [m for m in all_markets if m not in set(pri)]
    if max_markets:
        markets = markets[:max_markets]
    n_obs = 0
    all_vols: List[float] = []          # nonzero vols -> regime edges (meta["vol_edges"])
    for n, m in enumerate(markets):
        try:
            by_tok = _sample_market_books(store.capture_dir, m, max_files)
        except Exception:
            continue
        ent = peer_key(m) if peer_key else None
        cat = category_of(m) if category_of else None
        for tk, snaps in by_tok.items():
            snaps = [s for s in snaps if s.bids and s.asks and s.best_ask >= s.best_bid]
            if not snaps:
                continue
            res = None
            try:
                res = store.resolution(m, tk)
            except Exception:
                res = None
            res_ts = res.resolved_ts if res else None
            ts_arr = [s.capture_ts for s in snaps]      # for the as-of(t+lag) drift / trailing-vol lookups
            mid_arr = [s.mid for s in snaps]
            for bs in _even_sample(snaps, per_token):
                bi = str(_price_bucket(bs.mid))
                wcs = [_walk_cost(bs, s) for s in _SIZES]
                if any(w is None for w in wcs):
                    continue
                lead_d = round((res_ts - bs.capture_ts) / 86400.0, 3) if res_ts else -1.0
                # temporal slippage: adverse mid move over the lag window, from this token's OWN series
                j = bisect_left(ts_arr, bs.capture_ts + LAG_WINDOW_S)
                wmove = round(abs(snaps[j].mid - bs.mid), 6) if j < len(snaps) else -1.0
                # vol: recent mid RANGE over the trailing window (recent-volatility / entry-state proxy).
                # (Fit reads only `max_files` sampled hours, so a snapshot near a sampled file's left
                # edge sees a truncated window -> mild low-bias; range + coarse 3-regime cuts absorb it.)
                lo = bisect_left(ts_arr, bs.capture_ts - VOL_WINDOW_S)
                hi = bisect_right(ts_arr, bs.capture_ts)
                win = mid_arr[lo:hi]
                vol = round(max(win) - min(win), 6) if len(win) >= 2 else -1.0
                if vol > 0:
                    all_vols.append(vol)
                obs = [int(bs.capture_ts), lead_d] + [round(w, 6) for w in wcs] + [wmove, vol]
                _add("market", tk, bi, obs)
                _add("entity", ent, bi, obs)
                _add("category", cat, bi, obs)
                _add("global", "ALL", bi, obs)
                n_obs += 1
        if log and (n + 1) % 100 == 0:
            print(f"  ...{n + 1}/{len(markets)} markets, {n_obs} obs")
    # cap each (level,key,bucket) list by even sampling
    for level, keys in levels.items():
        cap = caps.get(level, 4000)
        for key, buckets in keys.items():
            for bi, rows in buckets.items():
                if len(rows) > cap:
                    rows.sort(key=lambda r: r[0])
                    buckets[bi] = _even_sample(rows, cap)
    vol_edges = _vol_edges(all_vols)    # calm/normal/dislocated regime cuts (None -> vol filter no-op)
    meta = {"n_markets": len(markets), "n_obs": n_obs, "sizes": list(_SIZES),
            "price_edges": _PRICE_EDGES, "per_token": per_token, "caps": caps,
            "vol_window_s": VOL_WINDOW_S, "vol_edges": vol_edges, "n_vol_obs": len(all_vols)}
    return SpreadModel(levels, peer_key, category_of, meta)


def save(model: SpreadModel, path: str = JSON_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)   # DESK_DATA_DIR target may not exist yet
    with open(path, "w") as f:
        json.dump({"meta": model.meta, "levels": model.levels}, f)


