"""data/sources.py — free official APIs. No auth.

Provides the T1 (mid price) and T0 (resolution) tiers that store falls back to
when no recorded book exists. All calls are public/no-auth and fail soft (return
None) so paper trading over captured data never hard-crashes on a network blip.

  Gamma  /markets         -> resolution outcome + token mapping (T0)
  CLOB   /prices-history   -> mid/last price series (T1)
  CLOB   /midpoint,/price  -> live point mid (when running forward)
"""
from __future__ import annotations

from bisect import bisect_right
from typing import List, Optional

import requests

from desk.types import PriceSeries, Resolution

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
TIMEOUT = 10


class Sources:
    def __init__(self, gamma_url: str = GAMMA, clob_url: str = CLOB, data_url: str = DATA,
                 session: Optional[requests.Session] = None):
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.data_url = data_url.rstrip("/")
        self.s = session or requests.Session()
        self._ph_cache: dict = {}   # token_id -> sorted [(ts, price)]

    # ── T1: price history (mid/last series) ──────────────────────────────────
    def prices_history(self, token_id: str, start_ts: Optional[int] = None,
                       end_ts: Optional[int] = None, fidelity: int = 1) -> Optional[PriceSeries]:
        params = {"market": token_id, "fidelity": fidelity}
        if start_ts is not None or end_ts is not None:
            if start_ts is not None:
                params["startTs"] = start_ts
            if end_ts is not None:
                params["endTs"] = end_ts
        else:
            params["interval"] = "max"   # endpoint requires a time component or interval
        try:
            r = self.s.get(f"{self.clob_url}/prices-history", params=params, timeout=TIMEOUT)
            r.raise_for_status()
            hist = r.json().get("history", [])
        except Exception:
            return None
        points = [[int(p["t"]), float(p["p"])] for p in hist if "t" in p and "p" in p]
        return PriceSeries(market_id="", token_id=token_id, points=points,
                           interval=f"{fidelity}m")

    def series(self, token_id: str) -> List[list]:
        """Full price series for a token (interval=max), fetched once and cached. The whole
        life of a market — early/pre-convergence included — so a full-life replay doesn't refetch."""
        if token_id not in self._ph_cache:
            ps = self.prices_history(token_id)   # no start/end => interval=max
            self._ph_cache[token_id] = ps.points if ps else []
        return self._ph_cache[token_id]

    def mid_at(self, token_id: str, t: int) -> Optional[float]:
        """T1 fallback: price as-of t from /prices-history (latest point with ts <= t)."""
        pts = self.series(token_id)
        if not pts:
            return None
        i = bisect_right([p[0] for p in pts], t)
        return pts[i - 1][1] if i > 0 else None

    def midpoint(self, token_id: str) -> Optional[float]:
        """Live point mid (forward-running only)."""
        try:
            r = self.s.get(f"{self.clob_url}/midpoint", params={"token_id": token_id}, timeout=TIMEOUT)
            r.raise_for_status()
            return float(r.json()["mid"])
        except Exception:
            return None

    # ── T0: resolution ───────────────────────────────────────────────────────
    def resolution(self, market_id: str, token_id: str) -> Optional[Resolution]:
        """Resolved payout for THIS token: Gamma first, then the CLOB /markets fallback for markets Gamma
        has already dropped (a settled market otherwise looks open forever). Resolution.source tags
        provenance (GAMMA | CLOB) so verdict aggregations can filter to Gamma-settled only. No price-pin
        heuristics here — a market with neither source stays OPEN (MTM), never a proxy settle."""
        res = self._resolution_gamma(market_id, token_id)
        if res is not None:
            return res
        return self._resolution_clob(market_id, token_id)

    def _resolution_clob(self, market_id: str, token_id: str) -> Optional[Resolution]:
        from desk.data.market_feed import clob_resolution
        try:
            p = clob_resolution(market_id, token_id)
        except Exception:
            return None
        if p is None:
            return None
        return Resolution(market_id=market_id, token_id=token_id, resolved_ts=0,
                          payout=float(p), outcome="", source="CLOB")

    def _resolution_gamma(self, market_id: str, token_id: str) -> Optional[Resolution]:
        """Gamma /markets?condition_ids=<market_id>. payout=1.0 if THIS token won.

        Token->outcome mapping via clobTokenIds; payout via outcomePrices once closed.
        """
        try:
            r = self.s.get(f"{self.gamma_url}/markets",
                          params={"condition_ids": market_id}, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            mkt = data[0] if isinstance(data, list) and data else data
        except Exception:
            return None
        if not mkt or not mkt.get("closed"):
            return None
        token_ids = _as_list(mkt.get("clobTokenIds"))
        prices = _as_list(mkt.get("outcomePrices"))
        outcomes = _as_list(mkt.get("outcomes"))
        if token_id not in token_ids or len(prices) != len(token_ids):
            return None
        idx = token_ids.index(token_id)
        try:
            payout = float(prices[idx])
        except (TypeError, ValueError):
            return None
        # resolved_ts is METADATA; the payout above is the truth. Gamma's endDate/umaEndDate are ISO-8601
        # strings on closed markets (a bare int() raised here, aborting the whole settle chain before the
        # CLOB fallback could run), so parse defensively: one bad date field must never block a settle.
        try:
            resolved_ts = _ts_epoch(mkt.get("umaEndDate") or mkt.get("endDate"))
            resolved_ts = _ts_epoch(mkt.get("closedTime")) or resolved_ts
        except Exception:
            resolved_ts = 0
        return Resolution(
            market_id=market_id, token_id=token_id, resolved_ts=resolved_ts,
            payout=payout, outcome=outcomes[idx] if idx < len(outcomes) else "",
            source="GAMMA")


def _ts_epoch(v) -> int:
    """Gamma date field -> unix seconds, fail-soft. Accepts epoch int/float (passthrough), numeric
    strings, and ISO-8601 datetimes (trailing 'Z' = UTC; naive = UTC). Unparseable/None -> 0, NEVER an
    exception — closed Gamma markets carry ISO endDate strings and a raise here would abort resolution
    before the CLOB fallback runs (the settle chain would resolve nothing)."""
    if v is None:
        return 0
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def _as_list(v):
    """Gamma sometimes returns JSON-encoded arrays as strings."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        import json
        try:
            return json.loads(v)
        except Exception:
            return [v]
    return [v]
