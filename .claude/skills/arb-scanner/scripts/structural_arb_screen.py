"""structural_arb_screen.py — Step 4b: screen STRUCTURAL arb families across the live board.

Three families (all verified live at least once; see research-notes.md):
1. BUY-ALL basket: short-dated negRisk basket where sum of live-leg YES asks < $1.00 and the
   outcome set is effectively closed (esp. "determined-but-unpublished": the measurement window
   already ended, so placeholder legs are dead by construction — e.g. weekly Netflix charts).
2. RANKED-PAIR complement: Polymarket lists "#1 X" AND "#2 X" events over the SAME ranking.
   For any candidate ~certain to finish top-2: YES(#1) + YES(#2) is a $1 complement — arb when
   the two asks sum < $1. Also implies cross-identities (A#1 <=> B#2 for a locked pair).
3. CONVERT pair: two dominant legs of a negRisk basket whose YES *bids* sum > $1.00 — buy both
   NOs below combined $1 (maker), then NegRiskAdapter.convertPositions pays $1/pair instantly
   (verify feeBips + guaranteed-winner; see research-notes.md §1; Maine Senate = worked example).

These edges are TRANSIENT (minutes, esp. during one-leg panics). A snapshot screen finds the
persistent ones; for the panic windows run with --watch to poll.

Usage: python structural_arb_screen.py --out <run_dir> [--max-days 7] [--watch SECONDS]
Writes <run_dir>/structural_candidates.json ; prints a ranked table.
"""
from __future__ import annotations
import argparse, json, re, sys, time
from datetime import datetime, timezone, timedelta
import requests

GAMMA = "https://gamma-api.polymarket.com"

def get(s, url, params=None, tries=3):
    for i in range(tries):
        try:
            r = s.get(url, params=params or {}, timeout=45)
            return r.json()
        except Exception as e:
            if i == tries - 1:
                print(f"warn: {url} -> {e}", file=sys.stderr); return None
            time.sleep(1.5)

def f(x, d=None):
    try: return float(x)
    except Exception: return d

def pull_short_dated(s, max_days):
    """Open events ending within max_days (paginated by endDate)."""
    out, now = [], datetime.now(timezone.utc)
    for off in range(0, 5000, 100):
        evs = get(s, f"{GAMMA}/events", {"closed": "false", "limit": 100, "offset": off,
                                          "order": "endDate", "ascending": "true"})
        if not isinstance(evs, list) or not evs: break
        stop = False
        for ev in evs:
            try: dt = datetime.fromisoformat(ev["endDate"].replace("Z", "+00:00"))
            except Exception: continue
            if dt > now + timedelta(days=max_days): stop = True; break
            if dt < now - timedelta(days=3): continue          # long-past zombie events
            if any(m.get("closed") for m in ev.get("markets", [])): continue  # partially resolved: remaining legs may all be losers
            out.append(ev)
        if stop: break
    return out

def leg_quotes(ev):
    legs = []
    for m in ev.get("markets", []):
        if m.get("closed"): continue
        legs.append(dict(t=(m.get("groupItemTitle") or m.get("question") or "?"),
                         ask=f(m.get("bestAsk"), 99.0), bid=f(m.get("bestBid"), 0.0),
                         cond=m.get("conditionId")))
    return legs

def screen(events):
    cands = []
    # --- family 1 + 3: per-basket screens
    for ev in events:
        if not ev.get("negRisk"): continue
        legs = leg_quotes(ev)
        if len(legs) < 2: continue
        live = [l for l in legs if not (l["ask"] >= 0.985 and l["bid"] <= 0.01)]   # drop dead placeholders
        blocked = [l for l in live if l["ask"] > 0.985 and l["bid"] > 0.01]        # real leg, no offer
        if len(live) >= 2 and not blocked:
            cost = sum(l["ask"] for l in live)
            if cost < 0.995:
                cands.append(dict(family="BUY_ALL", edge=round(1 - cost, 4),
                    title=ev.get("title"), end=(ev.get("endDate") or "")[:10],
                    detail=f"{len(live)} live legs, cost {round(cost,3)}",
                    legs=[(l["t"][:24], l["ask"]) for l in live][:6]))
        top2 = sorted(live, key=lambda l: -l["bid"])[:2]
        if len(top2) == 2:
            bidsum = top2[0]["bid"] + top2[1]["bid"]
            if bidsum > 1.005:
                cands.append(dict(family="CONVERT_PAIR", edge=round(bidsum - 1, 4),
                    title=ev.get("title"), end=(ev.get("endDate") or "")[:10],
                    detail=f"YES bids {top2[0]['bid']}+{top2[1]['bid']}={round(bidsum,3)}; buy both NOs < $1, convert",
                    legs=[(l["t"][:24], l["bid"]) for l in top2]))
    # --- family 2: ranked pairs — match "#1"/"top" vs "#2" over same subject
    by_subject = {}
    for ev in events:
        t = (ev.get("title") or "").lower()
        m = re.match(r"what will be the (top|#?\s?2|second)\s+(.*)", t)
        if not m: continue
        rank = "1" if m.group(1) == "top" else "2"
        by_subject.setdefault(m.group(2).strip(), {})[rank] = ev
    for subj, pair in by_subject.items():
        if "1" not in pair or "2" not in pair: continue
        L1 = {l["t"]: l for l in leg_quotes(pair["1"])}
        L2 = {l["t"]: l for l in leg_quotes(pair["2"])}
        for t, l1 in L1.items():
            if l1["bid"] < 0.02 or t not in L2: continue           # only plausible top-2 candidates
            ssum = l1["ask"] + L2[t]["ask"]
            if ssum < 0.995:
                cands.append(dict(family="RANKED_PAIR", edge=round(1 - ssum, 4),
                    title=f"{t} — {subj}", end=(pair['1'].get("endDate") or "")[:10],
                    detail=f"#1 ask {l1['ask']} + #2 ask {L2[t]['ask']} = {round(ssum,3)} (pays $1 if top-2)",
                    legs=[("#1", l1["ask"]), ("#2", L2[t]["ask"])]))
    # min-order feasibility: ~$1 min notional per order -> a leg at price p needs ceil(1/p) shares;
    # legs <2c force huge share counts that the sibling leg's depth rarely matches (kill-lesson #6)
    for c in cands:
        worst = min((pr for _, pr in c.get("legs", []) if isinstance(pr, (int, float)) and pr > 0), default=None)
        if worst is not None and worst < 0.02:
            c["min_order_note"] = f"leg at {worst}: $1 min order = {int(1/worst)+1} shares — verify sibling depth matches or label INFEASIBLE"
    cands.sort(key=lambda c: -c["edge"])
    return cands

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-days", type=float, default=7)
    ap.add_argument("--watch", type=int, default=0, help="poll every N seconds, print NEW hits only")
    a = ap.parse_args()
    s = requests.Session()
    seen = set()
    while True:
        events = pull_short_dated(s, a.max_days)
        cands = screen(events)
        json.dump(cands, open(f"{a.out}/structural_candidates.json", "w"), indent=1)
        fresh = [c for c in cands if (c["family"], c["title"]) not in seen]
        for c in cands: seen.add((c["family"], c["title"]))
        show = fresh if a.watch else cands
        print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {len(events)} short-dated events -> "
              f"{len(cands)} structural candidates ({'new: '+str(len(fresh)) if a.watch else 'all'})")
        for c in show[:12]:
            print(f"  {c['family']:12} +{c['edge']*100:4.1f}%  end {c['end']}  {(c['title'] or '')[:52]}")
            print(f"    {c['detail']}")
            if c.get("min_order_note"): print(f"    MIN-ORDER: {c['min_order_note']}")
        if not a.watch: break
        time.sleep(a.watch)
    print("\nNOTE: snapshot edges must be depth-walked (touch sizes are often dust) and fee-checked "
          "(taker fee can exceed a <1% edge — maker/convert paths preferred). Exhaustiveness: BUY_ALL "
          "requires the outcome set closed (window ended / guaranteed winner).", file=sys.stderr)

if __name__ == "__main__":
    main()
