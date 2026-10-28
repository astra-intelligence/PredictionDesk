"""data/market_feed.py — ONE shared live Polymarket feed (Gamma events / CLOB books / DATA prints).

Every live consumer (driver, strategies, recon) reads the exchange through THIS module — no hand-rolled
fetch loops elsewhere. All calls are bounded (per-request timeouts) and fail-soft (None/[] on error);
callers must treat None as "skip".
"""
from __future__ import annotations

import calendar
import json

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0"


def _as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return []


def open_markets(tag: int):
    """All OPEN markets under a Gamma `tag` id, via paginated Gamma /events.
    Returns [(condition_id, slug, question, yes_token_id)]."""
    out = []
    for off in range(0, 2500, 100):
        try:
            evs = S.get(f"{GAMMA}/events", params={"tag_id": tag, "closed": "false",
                        "limit": 100, "offset": off, "order": "startDate", "ascending": "false"},
                        timeout=20).json()
        except Exception:
            break
        if not evs:
            break
        for ev in evs:
            for m in (ev.get("markets") or []):
                if m.get("closed"):
                    continue
                toks = _as_list(m.get("clobTokenIds")); outs = _as_list(m.get("outcomes"))
                cond = m.get("conditionId")
                if cond and len(toks) == len(outs) and "Yes" in outs:
                    out.append((cond, m.get("slug", ""), m.get("question", ""), toks[outs.index("Yes")]))
        if len(evs) < 100:
            break
    return out


def clob_book(token):
    """Full L2 book for a token as {bids, asks, mid} (price-sorted, floats), or None."""
    try:
        r = S.get(f"{CLOB}/book", params={"token_id": token}, timeout=15).json()
    except Exception:
        return None
    bids = sorted(([float(x["price"]), float(x["size"])] for x in (r.get("bids") or [])), key=lambda z: -z[0])
    asks = sorted(([float(x["price"]), float(x["size"])] for x in (r.get("asks") or [])), key=lambda z: z[0])
    if not bids or not asks:
        return None
    return {"bids": bids, "asks": asks, "mid": (bids[0][0] + asks[0][0]) / 2.0}


def clob_midpoint(token):
    try:
        r = S.get(f"{CLOB}/midpoint", params={"token_id": token}, timeout=12).json()
        return float(r["mid"]) if "mid" in r else None
    except Exception:
        return None


def clob_resolution(cond, token):
    """Resolution for a CLOSED market via CLOB /markets/<cond>. Gamma drops resolved markets from its
    condition_ids filter AND the orderbook (hence midpoint) disappears, so a settled market otherwise looks
    'open' forever. This endpoint still returns the winning token for closed markets. -> payout 1.0/0.0, or None."""
    try:
        r = S.get(f"{CLOB}/markets/{cond}", timeout=12).json()
    except Exception:
        return None
    if not r.get("closed"):
        return None
    toks = r.get("tokens") or []
    # Settle ONLY when the market has an EXPLICIT winner ('closed' alone != resolved — winner=False on
    # every token means not-yet-reported, and a price-field fallback would be a price-pin by another name).
    if not any(t.get("winner") is True for t in toks):
        return None
    for t in toks:
        if str(t.get("token_id")) == str(token):
            return 1.0 if t.get("winner") is True else 0.0
    return None


def trades_since(cond, token, since_ts):
    """Real taker prints for `token` (asset) on this market since `since_ts`: [(price, size, taker_side)]."""
    try:
        d = S.get(f"{DATA}/trades", params={"market": cond, "limit": 500}, timeout=15).json()
    except Exception:
        return []
    out = []
    for t in (d if isinstance(d, list) else []):
        if str(t.get("asset")) == str(token) and int(t.get("timestamp", 0)) >= since_ts:
            try:
                out.append((float(t["price"]), float(t["size"]), t.get("side")))
            except Exception:
                pass
    return out


def res_ts(target_date):
    """Resolution-time proxy for a daily market = 23:00 UTC on the target date (override per strategy)."""
    y, m, d = (int(x) for x in target_date.split("-"))
    return int(calendar.timegm((y, m, d, 23, 0, 0, 0, 0, 0)))
