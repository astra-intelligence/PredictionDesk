#!/usr/bin/env python3
"""refresh_live.py — LOAD-TIME re-mark for the live dashboard page.

The driver's mark pass freezes a market's quote once it drops out of the active pricing universe near
resolution — which is exactly when the winning bucket jumps to ~1.0 — so live.html could show a resolved
winner at its stale pre-jump price (e.g. a +$1.50 winner rendered as a −$0.38 loser). This refetches the
LIVE CLOB book for every OPEN live position, rewrites marks.json with fresh quotes, then re-renders
live.html. The dashboard server runs it on each GET of the live page, so a reload shows PnL accurate as of
that load (stale again only until the next reload). Fail-soft per token — an unfetchable token keeps its
existing mark; the render still stays pure (reads the freshly-written marks.json, no network of its own).

    python3 .claude/skills/desk-dashboard/scripts/refresh_live.py    # (run with cwd = repo root)
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

CLOB = os.environ.get("CLOB_HOST") or "https://clob.polymarket.com"
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))  # repo root
MARKS_PATH = os.path.join(_ROOT, "desk", "tracker", "marks.json")   # anchored — runnable from any cwd
POS_GLOB = os.path.join(_ROOT, "desk", "driver", "positions_*.json")


def _book(token: str):
    """(best_bid, best_ask) from the live CLOB book, or (None, None) fail-soft."""
    try:
        d = requests.get(f"{CLOB}/book", params={"token_id": token}, timeout=4).json()
        bids = sorted((float(x["price"]) for x in d.get("bids", [])), reverse=True)
        asks = sorted(float(x["price"]) for x in d.get("asks", []))
        return (bids[0] if bids else None, asks[0] if asks else None)
    except Exception:
        return (None, None)


def refresh_live_marks() -> dict:
    """Refetch the live book for every OPEN live position and overwrite its marks.json quote with a fresh ts.
    Marks are per-market|token (shared across arms). Fail-soft: a token that won't fetch keeps its old mark."""
    keys = {}                                             # "market|token" -> token_id
    for pf in glob.glob(POS_GLOB):
        try:
            pos = json.load(open(pf))
        except Exception:
            continue
        for k, v in pos.items():
            if v.get("source") != "live" or abs(float(v.get("qty") or 0.0)) < 1e-4 or "|" not in k:
                continue
            keys[k] = k.split("|", 1)[1]
    try:
        marks = json.load(open(MARKS_PATH))
    except Exception:
        marks = {}
    if not keys:
        return {"refreshed": 0, "open_positions": 0}
    with ThreadPoolExecutor(max_workers=8) as ex:                     # parallel so a page load stays ~seconds
        books = dict(zip(keys, ex.map(lambda k: _book(keys[k]), keys)))
    now = int(time.time())
    refreshed = 0
    for k, (b, a) in books.items():
        if b is None and a is None:
            continue                                                 # keep the old (stale) mark, don't blank it
        mid = (b + a) / 2 if (b is not None and a is not None) else (b if b is not None else a)
        marks[k] = {"bid": b, "ask": a,
                    "live_mid": round(mid, 4) if mid is not None else None,
                    "basis": "live CLOB touch @ dashboard load", "ts": now}
        refreshed += 1
    if refreshed:
        tmp = MARKS_PATH + ".tmp"                                     # atomic write (server reads concurrently)
        with open(tmp, "w") as f:
            json.dump(marks, f)
        os.replace(tmp, MARKS_PATH)
    return {"refreshed": refreshed, "open_positions": len(keys)}


def _render_live() -> None:
    from adapters import ledger as L
    L._MARKS = None                                                  # drop the import-time marks cache -> reread
    from render import render
    render(L.build_live(), os.path.join("dashboard", "live.html"))


if __name__ == "__main__":
    out = refresh_live_marks()
    try:
        _render_live()
        out["rendered"] = True
    except Exception as e:
        out["render_error"] = repr(e)[:160]
    print(json.dumps(out))
