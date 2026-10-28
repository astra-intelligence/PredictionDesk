"""scan_markets.py — Step 1 of the arb scan.

Pulls EVERY open Polymarket event via the public Gamma API, writes a compact catalog
(for the cross-event step), and screens every mutually-exclusive (negRisk) basket for
intra-event arbitrage using the executable touch (bestBid/bestAsk), NOT mids.

Why bestBid/bestAsk and not mids: a mutually-exclusive basket's YES prices must sum to
$1 (exactly one outcome wins). A mid-based sum overstates edge by ~half the summed
spread — the "edge" is often just the spread. Buying costs the ASK, selling earns the
BID, so the only real signals are:
    buy_edge  = 1 - sum(bestAsk_i)   > 0  -> buy whole basket < $1  (riskless to resolution)
    sell_edge = sum(bestBid_i) - 1   > 0  -> sell whole basket > $1 (instant via negRisk convert)

Completeness matters: a basket is only valid if EVERY outcome has a live book. A dropped
illiquid tail leg falsely deflates the sum (fake buy_edge). Sell_edge (sum>1) is robust to
missing legs; buy_edge (sum<1) is suspect unless complete==True.

Usage: python scan_markets.py --out <run_dir>
Outputs: <run_dir>/catalog.json , <run_dir>/intra_candidates.json
"""
from __future__ import annotations
import argparse, json, os, sys, time
from datetime import datetime, timezone
import requests

GAMMA = "https://gamma-api.polymarket.com"

def f(x, d=None):
    try: return float(x)
    except Exception: return d
def jl(x):
    try: return json.loads(x) if isinstance(x, str) else (x or [])
    except Exception: return []

def pull_events(session):
    out, off, lim = [], 0, 100
    while True:
        b = None
        for attempt in (0, 1):                 # one retry — a transient 429/500 mid-pagination must not
            try:                               # silently truncate the universe (partial scan reads as full)
                r = session.get(f"{GAMMA}/events", params={"closed": "false", "limit": lim, "offset": off,
                                "order": "volume24hr", "ascending": "false"}, timeout=40)
                r.raise_for_status()       # a 429/500 JSON error BODY parses fine — without this it would
                b = r.json()               # dodge the retry and read as end-of-pagination (silent truncation)
                break
            except Exception:
                if attempt:
                    print(f"  WARNING: pagination aborted at offset {off} after retry — "
                          f"scan covers only the {len(out)} events pulled so far", file=sys.stderr)
                else:
                    time.sleep(2)
        if b is None: break
        if not isinstance(b, list) or not b: break
        b = [x for x in b if isinstance(x, dict)]
        if not b: break
        out.extend(b); off += lim
        if off % 1000 == 0: print(f"  pulled {len(out)} events...", file=sys.stderr)
        if len(b) < lim or off >= 40000: break
        time.sleep(0.05)
    return out

def days_to(end, now):
    try:
        dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max(0.1, (dt - now).total_seconds() / 86400)
    except Exception: return None

import re
def exhaustiveness_hint(labels):
    """Triage whether a basket's listed outcomes plausibly cover EVERY resolution.
    Numeric/range partitions are inherently exhaustive; named fields (people/parties/teams)
    usually hide an unpriced 'Other'/independent winner -> a buy-all sum<1 is then correct
    pricing, NOT arb. Only a hint; confirm by reading the resolution rule (Step 6)."""
    labs = [(l or "").strip() for l in labels]
    if not labs: return "unknown"
    numlike = sum(1 for l in labs if re.search(r"\d", l) or any(k in l.lower() for k in
                  ["or higher", "or more", "or fewer", "or less", "°", "%", "≤", "≥", "<", ">", "+"]))
    if numlike / len(labs) >= 0.7:
        return "numeric_partition_OK"            # buckets/thresholds: exhaustive by construction
    low = " ".join(labs).lower()
    if len(labs) == 2 and "democr" in low and "republic" in low:
        return "two_way_party_CHECK_INDEPENDENT"  # a viable independent breaks exhaustiveness
    return "named_field_CHECK_EXHAUSTIVE"          # candidates/teams/parties: likely unpriced 'Other'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    s = requests.Session()
    evs = pull_events(s)
    print(f"total open events: {len(evs)}", file=sys.stderr)
    now = datetime.now(timezone.utc)

    catalog, cands = [], []
    for ev in evs:
        mks = [m for m in ev.get("markets", []) if m.get("enableOrderBook") and not m.get("closed")]
        if not mks: continue
        tags = [t.get("label") for t in (ev.get("tags") or []) if isinstance(t, dict)]
        legs = []
        for m in mks:
            ct = jl(m.get("clobTokenIds"))
            legs.append(dict(o=m.get("groupItemTitle"), q=m.get("question"),
                bb=f(m.get("bestBid")), ba=f(m.get("bestAsk")), sp=f(m.get("spread")),
                yes=ct[0] if ct else None, cond=m.get("conditionId"),
                end=(m.get("endDate") or "")[:10], liq=f(m.get("liquidityNum"), 0)))
        catalog.append(dict(id=ev.get("id"), title=ev.get("title"), slug=ev.get("slug"),
            negRisk=ev.get("negRisk"), tags=tags, vol1mo=f(ev.get("volume1mo"), 0),
            n=len(legs), legs=legs))
        # intra-event basket screen (negRisk only)
        if not ev.get("negRisk") or len(legs) < 2: continue
        have_book = all(l["bb"] is not None and l["ba"] is not None for l in legs)
        if not have_book: continue
        sum_ask = sum(l["ba"] for l in legs)
        sum_bid = sum(l["bb"] for l in legs)
        buy_edge = 1 - sum_ask
        sell_edge = sum_bid - 1
        best_edge = max(buy_edge, sell_edge)
        if best_edge <= 0: continue
        ends = [l["end"] for l in legs if l["end"]]
        dte = min([d for d in (days_to(e + "T12:00:00Z", now) for e in ends) if d is not None], default=None)
        hint = exhaustiveness_hint([l["o"] for l in legs])
        cands.append(dict(event_id=ev.get("id"), title=ev.get("title"), N=len(legs),
            negRisk=True, complete=True, exhaustiveness_hint=hint,
            buy_edge=round(buy_edge, 4), sell_edge=round(sell_edge, 4),
            best_edge=round(best_edge, 4),
            direction="BUY_basket(<1)" if buy_edge >= sell_edge else "SELL_basket(>1)",
            sum_ask=round(sum_ask, 4), sum_bid=round(sum_bid, 4),
            dte_days=round(dte, 1) if dte else None,
            min_liq=round(min(l["liq"] for l in legs)),
            tot_liq=round(sum(l["liq"] for l in legs)),
            max_spread=max((l["sp"] for l in legs if l["sp"] is not None), default=None),
            vol1mo=round(f(ev.get("volume1mo"), 0)),
            legs=[dict(o=l["o"], cond=l["cond"], bb=l["bb"], ba=l["ba"], sp=l["sp"], liq=round(l["liq"])) for l in legs]))

    json.dump(catalog, open(f"{a.out}/catalog.json", "w"), default=str)
    cands.sort(key=lambda c: -c["best_edge"])
    json.dump(cands, open(f"{a.out}/intra_candidates.json", "w"), indent=1, default=str)
    print(f"\nwrote catalog ({len(catalog)} events) + {len(cands)} intra-event basket candidates", file=sys.stderr)
    print(f"\n=== TOP INTRA-EVENT BASKET CANDIDATES (executable touch edge) ===")
    print(f"{'edge%':>6} {'dir':>16} {'exhaustive?':>26} {'minLiq$':>8} {'dte':>5} {'N':>3}  title")
    for c in cands[:30]:
        flag = c["exhaustiveness_hint"]
        # a BUY-side edge on a non-numeric field is the classic fake (unpriced 'Other'/independent)
        warn = " ⚠" if (c["direction"].startswith("BUY") and "OK" not in flag) else ""
        print(f"{c['best_edge']*100:6.2f} {c['direction']:>16} {flag:>26}{warn:<2} {c['min_liq']:8.0f} "
              f"{str(c['dte_days'] or '-'):>5} {c['N']:3d}  {(c['title'] or '')[:40]}")
    print("\nNOTE: ⚠ = BUY-side edge on a named field -> probably NON-EXHAUSTIVE (unpriced 'Other'/"
          "independent), so sum<1 is correct pricing, not arb. Confirm with resolution.py. "
          "Also: high edge + tiny minLiq = stale-quote artifact; verify with depth_walk.py.", file=sys.stderr)

if __name__ == "__main__":
    main()
