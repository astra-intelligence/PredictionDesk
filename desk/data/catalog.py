"""data/catalog.py — the single "what data do we have" index (live + resolved).

The live universe (markets currently polled) is NOT the full picture: when a market
resolves it drops out of polling, but its captured books persist in the parquet tree and
its outcome is recorded in resolutions.jsonl. An agent that reads only the live universe
concludes "no resolved data" — which is wrong. This builds one catalog combining:
  - store.universe()          → markets with captured books (first/last ts, n_snaps)
  - store.resolved_markets()  → outcomes from resolutions.jsonl (payout, outcome)
so any agent can discover resolved markets to backtest end-to-end.

Run:  python3 -m desk.data.catalog        (writes catalog.json + prints a summary)
Each market is flagged `resolved` and `backtestable` (resolved AND has captured books).
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, Optional

from desk.data.store import Store


def _questions(capture_dir: str) -> Dict[str, str]:
    """market_id -> question (from resolutions.jsonl; human-friendly, not in the Resolution type)."""
    out: Dict[str, str] = {}
    try:
        with open(os.path.join(capture_dir, "resolutions.jsonl")) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("question"):
                    out[r.get("market_id")] = r["question"]
    except FileNotFoundError:
        pass
    return out


def build_catalog(store: Optional[Store] = None, now: Optional[int] = None) -> dict:
    store = store or Store()
    questions = _questions(store.capture_dir)
    res = {(r.market_id, r.token_id): r for r in store.resolved_markets()}
    markets: Dict[str, dict] = {}

    # 1) markets with captured books (live + any resolved whose parquet still exists)
    for (m, tk, t0, t1, n) in store.universe():
        mk = markets.setdefault(m, {"market_id": m, "question": questions.get(m),
                                    "first_ts": t0, "last_ts": t1, "resolved": False, "tokens": {}})
        rr = res.get((m, tk))
        mk["tokens"][tk] = {"first_ts": t0, "last_ts": t1, "n_snaps": n, "has_books": True,
                            "resolved": rr is not None,
                            "payout": rr.payout if rr else None, "outcome": rr.outcome if rr else None}
        mk["first_ts"] = min(mk["first_ts"], t0)
        mk["last_ts"] = max(mk["last_ts"], t1)
        if rr is not None:
            mk["resolved"] = True
            mk["resolved_ts"] = rr.resolved_ts

    # 2) resolved markets recorded in jsonl but with no book parquet left (outcome only)
    for (m, tk), rr in res.items():
        mk = markets.setdefault(m, {"market_id": m, "question": questions.get(m),
                                    "first_ts": None, "last_ts": None, "resolved": True,
                                    "resolved_ts": rr.resolved_ts, "tokens": {}})
        mk["resolved"] = True
        mk.setdefault("resolved_ts", rr.resolved_ts)
        if tk not in mk["tokens"]:
            mk["tokens"][tk] = {"n_snaps": 0, "has_books": False, "resolved": True,
                                "payout": rr.payout, "outcome": rr.outcome}

    for mk in markets.values():
        mk["backtestable"] = mk["resolved"] and any(t.get("has_books") for t in mk["tokens"].values())

    n_res = sum(1 for v in markets.values() if v["resolved"])
    n_bt = sum(1 for v in markets.values() if v["backtestable"])
    return {"schema_version": 1,
            "generated_ts": int(now if now is not None else time.time()),
            "n_markets": len(markets), "n_live": len(markets) - n_res,
            "n_resolved": n_res, "n_backtestable_resolved": n_bt, "markets": markets}


def write_catalog(store: Optional[Store] = None) -> str:
    store = store or Store()
    cat = build_catalog(store)
    path = os.path.join(store.capture_dir, "catalog.json")   # gitignored, lives with the data
    with open(path, "w") as f:
        json.dump(cat, f)
    return path


if __name__ == "__main__":
    s = Store()
    cat = build_catalog(s)
    path = write_catalog(s)
    print(f"catalog: {cat['n_markets']} markets · {cat['n_live']} live · "
          f"{cat['n_resolved']} resolved · {cat['n_backtestable_resolved']} backtestable (resolved + books)")
    print(f"written: {path}")
    bt = [m for m in cat["markets"].values() if m["backtestable"]]
    print(f"\nexample backtestable resolved markets (first 5):")
    for m in bt[:5]:
        out = next((t.get("outcome") for t in m["tokens"].values() if t.get("outcome")), "?")
        print(f"  {m['market_id'][:14]}  outcome={out}  {(m.get('question') or '')[:60]}")
