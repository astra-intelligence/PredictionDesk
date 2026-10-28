"""eval/recon/slippage_calibration.py — calibrate the cost model against REAL fills.

The spread/walk-cost model (desk/sim/spread_model.py) is fit from — and validated against —
the order BOOK. This module checks it against REALITY: real executed fills (captured `/trades`
prints — the same source golden_replay uses). For each real fill it computes:

  realized_cost = |real_price - mid|         # what a real taker actually paid over mid (REAL slippage)
  model_cost    = SpreadModel.estimate(...)  # the model prediction, built from OTHER markets/times
  walk_cost     = engine ladder-walk - mid   # what our T5 walk predicts (≈circular for prints; sanity only)

The PRIMARY, non-circular signal is realized_cost vs model_cost — it's the only one where the
prediction never saw this fill. A latency sweep (replay over book@(ts-Δ)) measures the fill-price
DISPERSION a decision-to-fill latency adds. Output: a per-(price,size) calibration table + a plain
recommendation for what (if anything) to add to the model.

NOTE this is a market-wide calibration of the cost LEVEL and latency VARIANCE — directional
adverse-selection (we fill exactly when the book turns against us) needs our OWN fills, i.e. a
small live bootstrap. Fees are not touched (golden_replay owns fee reconciliation).
"""
from __future__ import annotations

import json
import os
import statistics
from typing import List, Optional, Tuple

from desk.types import Order, OnchainFill
from desk.sim.fill_model import _walk
from desk.data.store import Store
from desk.sim.spread_model import SpreadModel, _price_bucket, _PRICE_EDGES

FINDINGS = os.path.join(os.path.dirname(__file__), "findings")
REPORT = os.path.join(FINDINGS, "slippage_calibration.json")

LATENCIES = (0, 1, 2, 5, 10, 30)        # seconds of decision->fill latency to sweep
SIZE_BUCKETS = (1.0, 5.0, 10.0, 25.0)
MIN_BUCKET_N = 12                       # min fills in a (price,size) cell to emit a calibration ratio


# ── helpers ───────────────────────────────────────────────────────────────────
def _walk_avg(book, side: str, size: float) -> Tuple[Optional[float], float]:
    levels = book.asks if side == "BUY" else book.bids
    raw, rem = _walk(levels, side, size, 0.999 if side == "BUY" else 0.001, 0)
    filled = size - rem
    if filled <= 1e-9:
        return None, 0.0
    return sum(r.price * r.qty for r in raw) / filled, filled


def _signed_cost(side: str, price: float, mid: float) -> float:
    """Taker crossing cost vs mid (price units). Positive = paid up (BUY) / sold down (SELL)."""
    return (price - mid) if side == "BUY" else (mid - price)


def _size_bucket(size: float) -> float:
    return min(SIZE_BUCKETS, key=lambda s: abs(s - size))


def _pct(vals: List[float], q: float) -> Optional[float]:
    if not vals:
        return None
    v = sorted(vals)
    return v[int(q * (len(v) - 1))]


def _med(vals: List[float]) -> Optional[float]:
    return round(statistics.median(vals), 6) if vals else None


# ── gather real fills (captured prints) ────────────────────────────────────────
def _gather(store: Store, max_tokens: int, max_per_token: int,
            target: int) -> Tuple[List[Tuple[str, str, OnchainFill]], str]:
    universe = sorted(store.universe(), key=lambda r: -r[4])[:max_tokens]   # richest books first
    out: List[Tuple[str, str, OnchainFill]] = []
    source = "none"
    for (m, tk, t0, t1, n) in universe:                                     # captured prints
        if len(out) >= target:
            break
        prints = store.prints_between(m, tk, t0, t1)
        for o in _aggregate_prints(prints):                                 # fold a sweep into one order
            out.append((m, tk, OnchainFill(market_id=m, token_id=tk, ts=o[0], price=o[1],
                                            size=o[2], side=o[3], maker="", taker="", tx_hash="", fee=0.0)))
        if prints:
            source = "prints"
    return out[:target], source


def _aggregate_prints(prints) -> List[Tuple[int, float, float, str]]:
    """A taker sweep prints as several rows at one ts; fold same-(ts,side) prints into one
    size-weighted order so the comparison is against a realistic taker order, not a single level."""
    by: dict = {}
    for p in prints:
        k = (p.ts, p.side)
        a = by.setdefault(k, [0.0, 0.0])   # [notional, size]
        a[0] += p.price * p.size
        a[1] += p.size
    return [(ts, round(notional / size, 6), round(size, 4), side)
            for (ts, side), (notional, size) in by.items() if size > 0]


# ── core ──────────────────────────────────────────────────────────────────────
def run(max_tokens: int = 220, max_per_token: int = 40, target: int = 500,
        store: Optional[Store] = None, peer_key=None, category_of=None) -> dict:
    """`peer_key` / `category_of` are OPTIONAL resolvers a strategy may inject so the spread model
    can back off to peer/category levels; both default to None (single flat default class)."""
    store = store or Store()
    sm = SpreadModel.load(peer_key=peer_key, category_of=category_of)

    fills, source = _gather(store, max_tokens, max_per_token, target)

    cases: List[dict] = []
    lat: dict = {d: [] for d in LATENCIES}      # Δ -> [signed price-unit residuals vs real fill]
    for (m, tk, fl) in fills:
        if fl.price <= 0 or fl.size <= 0:
            continue
        book0 = store.book_at(m, tk, fl.ts)
        if book0 is None or not book0.mid:
            continue
        mid = book0.mid
        realized = _signed_cost(fl.side, fl.price, mid)
        est = sm.estimate(m, tk, price=mid, size=fl.size, as_of_ts=fl.ts, band="median")
        walk_avg, filled = _walk_avg(book0, fl.side, fl.size)
        walk = _signed_cost(fl.side, walk_avg, mid) if walk_avg is not None else None
        # latency sweep: how far the Δ-stale book's predicted fill is from the REAL fill price
        for d in LATENCIES:
            bd = store.book_at(m, tk, fl.ts - d)
            if bd is None:
                continue
            wa, _ = _walk_avg(bd, fl.side, fl.size)
            if wa is not None:
                lat[d].append(fl.price - wa)
        cases.append({
            "token": tk[:12], "ts": fl.ts, "side": fl.side, "size": fl.size,
            "price": round(fl.price, 6), "mid": round(mid, 6),
            "realized_cost": round(realized, 6),
            "model_cost": round(est.cost, 6) if est else None,
            "model_src": est.source if est else None,
            "walk_cost": round(walk, 6) if walk is not None else None,
            "pb": _price_bucket(mid), "sb": _size_bucket(fl.size),
            "tier": book0.fidelity_tier, "tick": book0.tick_size,
        })

    return _summarize(cases, lat, source, store)


def _summarize(cases: List[dict], lat: dict, source: str, store: Store) -> dict:
    grounded = [c for c in cases if c["model_cost"] is not None
                and c["model_src"] not in (None, "insufficient", "mid")]
    # overall realized vs model (the calibration target)
    realized = [c["realized_cost"] for c in grounded]
    model = [c["model_cost"] for c in grounded]
    resid = [c["realized_cost"] - c["model_cost"] for c in grounded]
    ratios = [c["realized_cost"] / c["model_cost"] for c in grounded
              if c["model_cost"] > 1e-9 and c["realized_cost"] > 0]
    walk_resid = [c["realized_cost"] - c["walk_cost"] for c in cases if c["walk_cost"] is not None]

    # per (price_bucket, size_bucket) calibration ratios
    by_bucket: dict = {}
    calibration: dict = {}
    cells: dict = {}
    for c in grounded:
        cells.setdefault((c["pb"], c["sb"]), []).append(c)
    for (pb, sb), cs in sorted(cells.items()):
        rl = [x["realized_cost"] for x in cs]
        md = [x["model_cost"] for x in cs]
        rr = [x["realized_cost"] / x["model_cost"] for x in cs
              if x["model_cost"] > 1e-9 and x["realized_cost"] > 0]
        rec = {"price_lo": _PRICE_EDGES[pb], "size": sb, "n": len(cs),
               "realized_med_c": round((_med(rl) or 0) * 100, 3),
               "model_med_c": round((_med(md) or 0) * 100, 3),
               "ratio_med": round(_med(rr), 3) if rr else None}
        by_bucket[f"p{pb}_s{int(sb)}"] = rec
        if len(cs) >= MIN_BUCKET_N and rec["ratio_med"]:
            calibration.setdefault(str(pb), {})[str(int(sb))] = rec["ratio_med"]

    # latency dispersion (price units -> ¢)
    latency = {}
    for d in LATENCIES:
        vs = lat.get(d, [])
        latency[str(d)] = {"n": len(vs),
                           "med_abs_c": round((_med([abs(x) for x in vs]) or 0) * 100, 3),
                           "p90_abs_c": round((_pct([abs(x) for x in vs], 0.9) or 0) * 100, 3),
                           "med_signed_c": round((_med(vs) or 0) * 100, 3)}

    # split by the model's backoff source — only "market" (the token's OWN history) is an
    # apples-to-apples calibration; "global"/"category" backoff mixes a different population.
    by_source: dict = {}
    for src in ("market", "entity", "category", "global"):
        sub = [c for c in grounded if c["model_src"] == src]
        rr = [c["realized_cost"] / c["model_cost"] for c in sub
              if c["model_cost"] > 1e-9 and c["realized_cost"] > 0]
        by_source[src] = {"n": len(sub),
                          "realized_med_c": round((_med([c["realized_cost"] for c in sub]) or 0) * 100, 3),
                          "model_med_c": round((_med([c["model_cost"] for c in sub]) or 0) * 100, 3),
                          "ratio_med": round(_med(rr), 3) if rr else None}

    # tick scale of the sample (median 1-tick); realized≈½-tick ⇒ trades cleared AT THE TOUCH.
    tick_med = _med([c.get("tick") for c in cases if c.get("tick")]) or 0.001
    overall = {
        "n_grounded": len(grounded),
        "realized_med_c": round((_med(realized) or 0) * 100, 3),
        "model_med_c": round((_med(model) or 0) * 100, 3),
        "resid_med_c": round((_med(resid) or 0) * 100, 3),
        "resid_p90abs_c": round((_pct([abs(x) for x in resid], 0.9) or 0) * 100, 3),
        "ratio_med": round(_med(ratios), 3) if ratios else None,
        "walk_resid_med_c": round((_med(walk_resid) or 0) * 100, 3),   # sanity: ≈0 expected
        "half_tick_c": round(tick_med * 50, 3),                        # ½-tick in ¢ (touch-trade floor)
    }
    recommendation = _recommend(overall, by_source, latency, len(calibration))
    summary = {"source": source, "n_fills": len(cases), "n_grounded": len(grounded), **overall}

    out = {"summary": summary, "recommendation": recommendation, "by_source": by_source,
           "by_bucket": by_bucket, "latency": latency, "calibration": calibration, "cases_sample": cases[:40]}
    os.makedirs(FINDINGS, exist_ok=True)
    with open(REPORT, "w") as f:
        json.dump(out, f, indent=2)
    return out


def _recommend(overall: dict, by_source: dict, latency: dict, n_calib_cells: int) -> dict:
    rec = {"verdict": None, "detail": "", "what_to_add": None}
    if overall["n_grounded"] < 30:
        rec["verdict"] = "insufficient-data"
        rec["detail"] = (f"only {overall['n_grounded']} grounded fills — need more real fills (raise "
                         "--target, widen the universe, or enable on-chain) before any calibration.")
        return rec

    # CONFOUND CHECK 1 — touch-trade selection: real trades clear AT THE TOUCH (realized ≈ ½-tick),
    # but the model estimates aggress-anytime cost over ALL book states (incl. the thin moments a
    # signal-timed T1 order can hit). A market-wide print floor is NOT the cost we'd pay.
    touch = overall["realized_med_c"] <= overall["half_tick_c"] * 1.5
    # CONFOUND CHECK 2 — only the "market" backoff (the token's OWN history) is apples-to-apples;
    # "global"/"category" mixes a different population than these liquid prints.
    mkt = by_source.get("market", {})
    r_mkt = mkt.get("ratio_med")

    own_validated = (r_mkt is not None and mkt.get("n", 0) >= 30 and 0.8 <= r_mkt <= 1.25)
    if touch:
        rec["verdict"] = ("model VALIDATED on own-data; aggregate gap is selection/backoff confound"
                          if own_validated else "selection-confounded — do NOT down-calibrate")
        msg = ""
        if own_validated:
            msg = (f"On the apples-to-apples 'market'-source subset (n={mkt['n']}, the token's OWN "
                   f"history) realized/model = {r_mkt} — the model predicts real taker cost EXACTLY "
                   "where it has own data. ")
        rec["detail"] = (
            msg + f"Real trades clear at the TOUCH (realized median {overall['realized_med_c']}¢ ≈ "
            f"½-tick {overall['half_tick_c']}¢), while the model's backoff levels estimate "
            "aggress-at-ANY-book-state cost (higher). The aggregate gap is a SELECTION + backoff "
            "population confound (executions cluster in tight books; illiquid tokens fall back to a "
            "thin-book population), NOT a level error — a market-wide multiplier would make T1 fills "
            "falsely near-free and contradict the measured spread costs on wide books. "
            "Calibration stays OFF.")
        rec["what_to_add"] = "nothing — model is right where verifiable; the real gap needs OUR own fills"
    elif r_mkt is not None and mkt.get("n", 0) >= 30 and not (0.8 <= r_mkt <= 1.25):
        d = "UNDER" if r_mkt > 1 else "OVER"
        rec["verdict"] = "level-bias (own-data subset)"
        rec["detail"] = (f"On the apples-to-apples 'market'-source subset (n={mkt['n']}), R5 {d}-predicts "
                         f"realized cost by {r_mkt}x. This subset is fair (token's own history), so apply "
                         f"the per-(price,size) multipliers ({n_calib_cells} cells) via "
                         "SpreadModel.load(calibration_path=...).")
        rec["what_to_add"] = "calibration multiplier (market-source only)"
    elif r_mkt is not None and 0.8 <= r_mkt <= 1.25:
        rec["verdict"] = "well-calibrated (own-data subset)"
        rec["detail"] = (f"On the 'market'-source subset (n={mkt.get('n')}), realized/model = {r_mkt} "
                         "(±25%). The model matches real cost where it uses the token's own data; the "
                         "global-fallback mismatch is a population artifact, not a level error.")
        rec["what_to_add"] = "nothing (level)"
    else:
        rec["verdict"] = "inconclusive — need own-data fills"
        rec["detail"] = (f"Too few 'market'-source fills (n={mkt.get('n', 0)}) for a fair verdict; the "
                         "global-fallback comparison is population-confounded. Need fills on markets "
                         "with their own model data, or our OWN fills.")
        rec["what_to_add"] = "nothing yet"

    # latency: at a 5s capture cadence sub-cadence Δ hits the SAME snapshot — uninformative here.
    d0 = latency.get("0", {}).get("p90_abs_c") or 0
    d10 = latency.get("10", {}).get("p90_abs_c") or 0
    if d10 - d0 > 0.5:
        rec["latency"] = (f"latency adds ~{round(d10 - d0, 2)}¢ p90 dispersion by 10s → consider a "
                          "Δ-variance term once finer (sub-5s / own-fill) data exists.")
    else:
        rec["latency"] = ("latency NOT measurable here — the 5s capture cadence collapses sub-cadence Δ "
                          "to the same snapshot; needs sub-second or our own fills to quantify.")
    rec["caveat"] = ("market-wide fills calibrate the cost LEVEL only, and only weakly (selection + "
                     "population confounds). Directional adverse-selection and latency need our OWN "
                     "fills at signal-driven times → a small live bootstrap (the comparator ingests them).")
    return rec


def _md(out: dict) -> str:
    s, rec = out["summary"], out["recommendation"]
    L = [f"# Slippage calibration vs real fills (source={s['source']}, n={s['n_fills']}, grounded={s['n_grounded']})", "",
         "| metric (¢) | value |", "|---|---|",
         f"| realized taker cost (median) | {s['realized_med_c']} |",
         f"| model cost (median) | {s['model_med_c']} |",
         f"| **realized/model ratio (median)** | **{s['ratio_med']}** |",
         f"| residual realized-model (median / p90abs) | {s['resid_med_c']} / {s['resid_p90abs_c']} |",
         f"| engine-walk residual (median, ≈0 sanity) | {s['walk_resid_med_c']} |",
         f"| realized clears at touch? (½-tick={s['half_tick_c']}¢) | {'YES' if s['realized_med_c']<=s['half_tick_c']*1.5 else 'no'} |", "",
         "## realized/model by backoff source (only 'market' = apples-to-apples)", "",
         "| source | n | realized_c | model_c | ratio |", "|---|---|---|---|---|"]
    for src, b in out["by_source"].items():
        L.append(f"| {src} | {b['n']} | {b['realized_med_c']} | {b['model_med_c']} | {b['ratio_med']} |")
    L += ["", "## Latency dispersion (|real − walk@(ts−Δ)|, ¢)", "",
         "| Δ (s) | n | med_abs | p90_abs | med_signed |", "|---|---|---|---|---|"]
    for d in ("0", "1", "2", "5", "10", "30"):
        x = out["latency"].get(d, {})
        L.append(f"| {d} | {x.get('n')} | {x.get('med_abs_c')} | {x.get('p90_abs_c')} | {x.get('med_signed_c')} |")
    L += ["", "## Per-bucket realized vs model (¢) — calibration source", "",
          "| price≥ | size | n | realized | model | ratio |", "|---|---|---|---|---|---|"]
    for k, b in out["by_bucket"].items():
        L.append(f"| {b['price_lo']} | {int(b['size'])} | {b['n']} | {b['realized_med_c']} | {b['model_med_c']} | {b['ratio_med']} |")
    L += ["", f"## Recommendation: **{rec['verdict']}**", "", rec["detail"],
          "", f"- latency: {rec.get('latency','')}", f"- what to add: **{rec.get('what_to_add')}**",
          f"- caveat: {rec.get('caveat','')}", f"- calibration cells written: {len(out['calibration'])}"]
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    kw = {}
    for flag, key in (("--target", "target"), ("--max-tokens", "max_tokens"), ("--max-per-token", "max_per_token")):
        if flag in sys.argv:
            kw[key] = int(sys.argv[sys.argv.index(flag) + 1])
    out = run(**kw)
    print(_md(out))
    print(f"\nwritten: {REPORT}")
