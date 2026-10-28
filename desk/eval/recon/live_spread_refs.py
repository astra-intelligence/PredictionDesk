"""eval/recon/live_spread_refs.py — precompute the cost-model's LIVE-REFERENCE FLOOR (waterfall tier 4).

The cost model (sim/spread_model.py) prices a fill from the best available historic data,
falling back to a STRUCTURAL out-of-window proxy (`_oow`). In the rare case where even the
all-time global bucket is insufficient (no historic obs at all), the model must STILL never price
a fill free — it falls to a REAL live-reference spread, never a fabricated/static number:

  * `live_ref`      — THIS market is open: its CURRENT live spread from Gamma bestBid/bestAsk.
  * `live_adjacent` — THIS market is closed (no live book): an LLM picks the most comparable ACTIVE
                      market and we use ITS live spread. A real reference, just from a peer market.

Because hitting Gamma / an LLM inside the per-fill loop would make backtests slow + non-deterministic,
this is PRECOMPUTED ONCE (offline) for the markets a run needs → findings/live_spread_refs.json. The
cost model loads it via `ExecCostModel.load(live_refs_path=...)` (mirrors `calibration_path`).

Output schema (findings/live_spread_refs.json):
  { "<market_id>": {"half_spread": 0.012, "source": "live_ref",       "spread": 0.024, "ref_market": null},
    "<market_id>": {"half_spread": 0.018, "source": "live_adjacent",  "spread": 0.036, "ref_market": "0x..", "ref_question": "..."},
    "_by_category": {"<category>": {"half_spread": 0.015, "source": "live_ref"}},   # category-level floor
    "_meta": {"generated_ts": ..., "n_live_ref": .., "n_live_adjacent": .., "pool_n": ..} }

Run:  python3 -m desk.eval.recon.live_spread_refs [--max N] [--market 0x..] ...
Default market set = locally resolved markets (the closed set most likely to need `live_adjacent`).
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from typing import Dict, List, Optional

import requests

GAMMA = "https://gamma-api.polymarket.com"
OUT = os.path.join(os.path.dirname(__file__), "findings", "live_spread_refs.json")
TIMEOUT = 15
POOL_N = 100                 # active markets fetched once as the LLM-adjacency candidate pool (Gamma caps /markets at 100)
POOL_MAX_SPREAD = 0.25       # ignore junk-wide illiquid actives when forming the pool / category floor
ADJ_MODEL = "claude-haiku-4-5-20251001"   # cheap classifier for the adjacency pick


def _half(spread: Optional[float], bid, ask) -> Optional[float]:
    """Half-spread (per-share crossing cost vs mid) from Gamma fields; needs a real two-sided book."""
    if bid is None or ask is None:
        return None
    try:
        s = float(spread) if spread is not None else (float(ask) - float(bid))
    except (TypeError, ValueError):
        return None
    return round(s / 2.0, 6) if s > 0 else None


def gamma_market(condition_id: str, closed: Optional[bool] = None, session=None) -> Optional[dict]:
    """One market by condition_id. Gamma omits closed markets unless closed=true is passed."""
    s = session or requests
    params = {"condition_ids": condition_id}
    if closed is not None:
        params["closed"] = "true" if closed else "false"
    try:
        r = s.get(f"{GAMMA}/markets", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None
    return (data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None))


def active_pool(session=None) -> List[dict]:
    """The candidate pool of LIQUID active markets (real two-sided book) for `live_adjacent` picks."""
    s = session or requests
    try:
        r = s.get(f"{GAMMA}/markets",
                  params={"active": "true", "closed": "false", "order": "volume",
                          "ascending": "false", "limit": POOL_N}, timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
    except Exception:
        return []
    pool = []
    for mk in rows if isinstance(rows, list) else []:
        hs = _half(mk.get("spread"), mk.get("bestBid"), mk.get("bestAsk"))
        if hs is not None and hs * 2 <= POOL_MAX_SPREAD:
            pool.append({"id": mk.get("conditionId") or mk.get("condition_id"),
                         "question": mk.get("question") or mk.get("slug") or "",
                         "half_spread": hs, "liquidity": float(mk.get("liquidity") or 0)})
    return pool


def _llm_pick_adjacent(question: str, pool: List[dict]) -> Optional[int]:
    """LLM judgment: index of the pool market most comparable to `question` (same kind of event /
    price dynamics → similar spread). Fail-soft to None if the SDK/key/call is unavailable."""
    if not pool or not question:
        return None
    try:
        import anthropic  # optional dep; precompute is offline + rare
    except Exception:
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    listing = "\n".join(f"[{i}] {m['question'][:120]} (liq={m['liquidity']:.0f})" for i, m in enumerate(pool))
    try:
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=ADJ_MODEL, max_tokens=16,
            system=("You match a CLOSED prediction market to the most comparable ACTIVE market so its "
                    "live bid/ask spread can proxy the closed one's. Prefer same event type, similar "
                    "resolution mechanics and price range, and decent liquidity. Reply with ONLY the "
                    "integer index in brackets, e.g. 7."),
            messages=[{"role": "user", "content": [
                {"type": "text", "text": f"CLOSED market:\n{question[:200]}\n\nACTIVE candidates:",
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": listing}]}])
        txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        digits = "".join(ch if ch.isdigit() else " " for ch in txt).split()
        if digits:
            i = int(digits[0])
            return i if 0 <= i < len(pool) else None
    except Exception:
        return None
    return None


def _deterministic_adjacent(question: str, pool: List[dict]) -> Optional[int]:
    """Fallback adjacency when the LLM is unavailable: highest-liquidity pool market sharing the most
    keywords with the question (still a REAL live market's spread — never a fabricated number)."""
    if not pool:
        return None
    qwords = {w for w in question.lower().split() if len(w) > 3}
    best, best_score = None, -1.0
    for i, m in enumerate(pool):
        overlap = len(qwords & {w for w in m["question"].lower().split() if len(w) > 3})
        score = overlap * 1e6 + m["liquidity"]   # keyword overlap first, then liquidity
        if score > best_score:
            best, best_score = i, score
    return best


def build_refs(market_ids: List[str], category_of=None, session=None,
               use_llm: bool = True, log: bool = True) -> dict:
    s = session or requests.Session()
    pool = active_pool(s)
    out: Dict[str, dict] = {}
    by_cat_hs: Dict[str, List[float]] = {}
    n_ref = n_adj = 0
    for n, mid in enumerate(market_ids):
        mk = gamma_market(mid, closed=False, session=s)   # open?
        hs = _half(mk.get("spread"), mk.get("bestBid"), mk.get("bestAsk")) if mk else None
        if hs is not None:                                 # tier-4a: this market's own live book
            out[mid] = {"half_spread": hs, "source": "live_ref",
                        "spread": round(hs * 2, 6), "ref_market": None}
            n_ref += 1
            if category_of:
                by_cat_hs.setdefault(category_of(mid) or "other", []).append(hs)
        else:                                              # tier-4b: closed -> LLM-adjacent live market
            cm = gamma_market(mid, closed=True, session=s)
            q = (cm.get("question") if cm else "") or ""
            idx = (_llm_pick_adjacent(q, pool) if use_llm else None)
            if idx is None:
                idx = _deterministic_adjacent(q, pool)
            if idx is not None:
                ref = pool[idx]
                out[mid] = {"half_spread": ref["half_spread"], "source": "live_adjacent",
                            "spread": round(ref["half_spread"] * 2, 6),
                            "ref_market": ref["id"], "ref_question": ref["question"][:120]}
                n_adj += 1
        if log and (n + 1) % 25 == 0:
            print(f"  ...{n + 1}/{len(market_ids)} refs ({n_ref} live_ref, {n_adj} live_adjacent)")
    by_category = {c: {"half_spread": round(statistics.median(v), 6), "source": "live_ref"}
                   for c, v in by_cat_hs.items() if v}
    # pool-wide median floor for categories we never saw open (still a real live reference)
    if pool:
        by_category.setdefault("_pool", {"half_spread": round(statistics.median(m["half_spread"] for m in pool), 6),
                                         "source": "live_ref"})
    out["_by_category"] = by_category
    out["_meta"] = {"generated_ts": int(time.time()), "n_live_ref": n_ref, "n_live_adjacent": n_adj,
                    "pool_n": len(pool), "n_markets": len(market_ids)}
    # NOTE: a market the LLM+deterministic adjacency both skip (only when the active pool is empty,
    # e.g. a Gamma outage) gets no per-market entry — the cost model then falls to `_by_category._pool`
    # and finally its code-level global_backstop, so the never-0 invariant still holds downstream.
    return out


def main() -> int:
    from desk.data.store import Store
    argv = sys.argv[1:]
    use_llm = "--no-llm" not in argv
    max_n = int(argv[argv.index("--max") + 1]) if "--max" in argv else None
    explicit = [argv[i + 1] for i, a in enumerate(argv) if a == "--market"]
    store = Store()
    cat = None            # optional category resolver a strategy may inject (default: single class)
    if explicit:
        market_ids = explicit
    else:
        market_ids = sorted({r.market_id for r in store.resolved_markets()})
    if max_n:
        market_ids = market_ids[:max_n]
    print(f"building live_spread_refs for {len(market_ids)} markets (use_llm={use_llm}) ...")
    refs = build_refs(market_ids, category_of=cat, use_llm=use_llm)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(refs, f, indent=0)
    print(f"\nwrote {OUT}")
    print(f"  {refs['_meta']}")
    print(f"  by_category: {refs['_by_category']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
