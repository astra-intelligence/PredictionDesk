"""sample_weather/strategy.py — single-model gaussian weather pricing (the tutorial strategy).

The whole pricing model in one sentence: take ONE Open-Meteo model's point forecast for the city's
daily max/min, wrap a gaussian with a FIXED per-city σ around it, integrate the bucket, and buy the
token whose executable edge (fair minus the ask you'd actually cross) clears `entry_threshold`.

What this deliberately does NOT do (each is where a real strategy earns its keep):
  · no forecast-error calibration — σ is a hand-set constant; the gaussian is assumed, not fitted
  · no model blending — one model, taken at face value
  · no settlement-gauge mapping — city-center coordinates, not the station the market resolves on
  · no guards (regime filters, cross-model vetoes, staleness checks), no sizing beyond a flat qty
No edge is claimed. The tenant exists so the full pipeline — registry discovery → driver fire →
paper engine against the captured book → ledger → dashboard — runs end-to-end on a fresh clone,
with honest per-trade decomps on the dashboard.

Data in: the capture layer's `_universe.json` (which markets exist), the captured order book at
fire time (what you'd pay), Open-Meteo's free forecast API (one call per city per fire), and one
Gamma lookup per CANDIDATE market to label which token is "Yes" (never guess a side).
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

import requests

from desk.types import Order
from .cities import CITIES, bucket_prob, parse_slug

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
GAMMA_URL = "https://gamma-api.polymarket.com/markets"

_OUTCOME_CACHE: Dict[str, Optional[str]] = {}   # slug -> YES token_id (per process)


def _universe_meta(store) -> dict:
    """(market_id, token_id) -> universe row ({'slug', 'question', ...}) from the capture layer's
    light `_universe.json` index — never the parquet corpus. {} when absent (nothing to trade)."""
    cached = getattr(store, "_sample_weather_meta", None)
    if cached is not None:
        return cached
    out: dict = {}
    try:
        with open(os.path.join(getattr(store, "capture_dir"), "_universe.json")) as f:
            toks = json.load(f).get("tokens", {})
        for r in (toks.values() if isinstance(toks, dict) else toks):
            out[(r.get("market_id"), r.get("token_id"))] = r
    except Exception:
        out = {}
    try:
        store._sample_weather_meta = out
    except Exception:
        pass
    return out


def _open_meteo_daily(city: str, model: str, cache: dict) -> dict:
    """The city's 7-day daily forecast arrays from the chosen model (ONE call per city per fire,
    cached in `cache`). {} on any failure — that city is skipped this pass, loudly."""
    if city in cache:
        return cache[city]
    c = CITIES[city]
    daily: dict = {}
    try:
        r = requests.get(OPEN_METEO_URL, timeout=10, params={
            "latitude": c["lat"], "longitude": c["lon"],
            "daily": "temperature_2m_max,temperature_2m_min",
            "models": model, "timezone": c["tz"], "forecast_days": 7})
        daily = (r.json() or {}).get("daily") or {}
    except Exception as e:
        print(json.dumps({"sample_weather": "open-meteo fetch failed — city skipped",
                          "city": city, "err": repr(e)[:120]}), flush=True)
    cache[city] = daily
    return daily


def _mu_for(daily: dict, target_date: str, metric: str) -> Optional[float]:
    """The model's point forecast (°C) for metric on target_date, or None. Tolerates Open-Meteo's
    model-suffixed variable names (temperature_2m_max_<model>)."""
    key = "temperature_2m_max" if metric == "highest" else "temperature_2m_min"
    times = daily.get("time") or []
    if target_date not in times:
        return None
    vals = None
    for k, v in daily.items():
        if k == key or k.startswith(key + "_"):
            vals = v
            break
    try:
        x = (vals or [])[times.index(target_date)]
        return float(x) if x is not None else None
    except Exception:
        return None


def _yes_token(slug: str) -> Optional[str]:
    """token_id of the 'Yes' outcome for this market, via one Gamma lookup (cached per process).
    None = unresolvable → the market is SKIPPED: a strategy must never guess which side it holds."""
    if slug in _OUTCOME_CACHE:
        return _OUTCOME_CACHE[slug]
    tok: Optional[str] = None
    try:
        mk = (requests.get(GAMMA_URL, params={"slug": slug}, timeout=10).json() or [{}])[0] or {}
        outs = json.loads(mk.get("outcomes") or "[]")
        toks = json.loads(mk.get("clobTokenIds") or "[]")
        for o, t in zip(outs, toks):
            if str(o).strip().lower() == "yes":
                tok = str(t)
                break
    except Exception as e:
        print(json.dumps({"sample_weather": "gamma outcome lookup failed — market skipped",
                          "slug": slug, "err": repr(e)[:120]}), flush=True)
    _OUTCOME_CACHE[slug] = tok
    return tok


def generate_orders(store, as_of_ts: Optional[int] = None, params: Optional[dict] = None,
                    positions: Optional[dict] = None, out_signals: Optional[dict] = None,
                    universe_rows: Optional[list] = None) -> List[Order]:
    """The tenant entrypoint (see AGENTS.md: the driver passes only the kwargs this signature names).
    Scans captured temperature markets inside the lead window, prices each bucket with the gaussian,
    and emits IOC taker BUYs where executable edge ≥ entry_threshold. One position per market."""
    params = params or {}
    sid = params.get("strategy_id", "sample_weather")
    model = params.get("model", "gfs_global")
    thr = float(params.get("entry_threshold", 0.2))
    lead_min = int(params.get("lead_min", 1))
    lead_max = int(params.get("lead_max", 5))
    qty = float(params.get("qty", 5.0))
    min_price = float(params.get("min_price", 0.05))
    as_of_ts = int(as_of_ts if as_of_ts is not None else time.time())
    today = datetime.fromtimestamp(as_of_ts, tz=timezone.utc).date()   # lead counted in UTC days —
    # a half-day coarser than each city's local calendar; acceptable for a 1–5 day entry window.
    positions = positions or {}
    held_markets = {m for (m, _tk) in positions}

    meta = _universe_meta(store)
    uni = universe_rows if universe_rows is not None else [(m, tk) for (m, tk, *_r) in store.universe()]
    fc_cache: dict = {}
    out_signals = out_signals if out_signals is not None else {}
    orders: List[Order] = []
    ordered_markets: set = set()
    n_views = 0
    for m, tk in uni:
        slug = (meta.get((m, tk)) or {}).get("slug", "")
        spec = parse_slug(slug)
        if spec is None or m in held_markets or m in ordered_markets:
            continue
        try:
            lead = (date.fromisoformat(spec["date"]) - today).days
        except ValueError:
            continue
        if lead < lead_min or lead > lead_max:
            continue
        daily = _open_meteo_daily(spec["city"], model, fc_cache)
        mu = _mu_for(daily, spec["date"], spec["metric"])
        if mu is None:
            continue
        sigma = float(CITIES[spec["city"]]["sigma_c"])
        p_yes = bucket_prob(mu, sigma, spec["lo_c"], spec["hi_c"])
        d = store.route(m, tk, as_of_ts)
        book = getattr(d, "book", None)
        ask = getattr(book, "best_ask", None) if book is not None else None
        if ask is None or not (0.0 < ask < 1.0) or ask < min_price:
            continue
        n_views += 1
        # Cheap pre-screen BEFORE the Gamma label lookup: whichever outcome this token is, its fair
        # is p_yes or 1-p_yes — if neither clears the threshold vs this ask, no lookup is needed.
        if max(p_yes - ask, (1.0 - p_yes) - ask) < thr:
            continue
        yes_tok = _yes_token(slug)
        if yes_tok is None:
            continue
        fair = p_yes if tk == yes_tok else 1.0 - p_yes
        edge = fair - ask                                   # EXECUTABLE edge: vs the touch we'd cross
        if edge < thr:
            continue
        orders.append(Order(
            order_id=f"{sid}-{as_of_ts}-{len(orders)}", strategy_id=sid, market_id=m, token_id=tk,
            side="BUY", submit_ts=as_of_ts, limit_price=round(ask, 4), qty=qty, tif="IOC",
            expiry_ts=None, mode="taker"))
        ordered_markets.add(m)                              # one entry per market per fire
        out_signals[(m, tk)] = {
            "slug": slug, "fair": round(fair, 4), "mid": getattr(d, "mid", None),
            "edge": round(edge, 4),
            "gap": round(fair - d.mid, 4) if getattr(d, "mid", None) is not None else None,
            "engine": f"{model}+gaussian", "lead": lead, "city": spec["city"],
            "target_date": spec["date"],
            "decomp": {"models": [{"model": model, "value_c": round(mu, 2)}],
                       "central_c": round(mu, 2), "central_method": "point",
                       "metric": spec["metric"],
                       "bucket_lo": spec["lo_c"], "bucket_hi": spec["hi_c"],
                       "gauge": {"dist": "gaussian", "lead": lead, "offset": 0.0,
                                 "sigma": sigma, "n_errors": 0, "fair": round(p_yes, 4)}}}
    out_signals["__n_views__"] = n_views
    return orders
