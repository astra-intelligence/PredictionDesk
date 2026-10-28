"""check_links.py — Step 4 of the arb scan: verify proposed cross-event links live.

The link-finder agents (Step 3) write links_<theme>.json files describing logically-forced
relationships between markets in DIFFERENT events. This script re-fetches LIVE order books
(agents work from stale snapshots) and computes the executable edge per relationship type,
then ranks. It also reports touch capacity (min size across the legs you must cross) so you
can immediately see whether a big edge is real or a tiny stale-quote artifact.

Edge math per type (all use bid to sell / ask to buy — what you can actually transact):
  DUPLICATE   P(A)=P(B):      edge = max(bid_B-ask_A, bid_A-ask_B)
  COMPLEMENT  P(A)+P(B)=1:    edge = max(1-(ask_A+ask_B), (bid_A+bid_B)-1)
  IMPLICATION P(A)<=P(B):     edge = bid_A - ask_B   (sell the rich A, buy the cheap B)
  SUBSET_SUM  sum(A_i)=P(B):  edge = max(bid_B-sum(ask_A_i), sum(bid_A_i)-ask_B)

Usage: python check_links.py --out <run_dir>
Reads <run_dir>/catalog.json + <run_dir>/links_*.json ; writes <run_dir>/link_candidates.json
"""
import argparse, json, sys, time
import requests

CLOB = "https://clob.polymarket.com"

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    s = requests.Session()
    cat = json.load(open(f"{a.out}/catalog.json"))
    cond2yes, cond2meta = {}, {}
    for e in cat:
        for l in e["legs"]:
            cond2yes[l["cond"]] = l["yes"]
            cond2meta[l["cond"]] = dict(o=l["o"], q=l["q"], end=l["end"], liq=l["liq"], title=e["title"])

    links = []
    import glob
    for path in glob.glob(f"{a.out}/links_*.json"):
        try:
            for x in json.load(open(path)):
                x["_src"] = path.split("links_")[-1].replace(".json", ""); links.append(x)
        except Exception as ex:
            print(f"load fail {path}: {ex}", file=sys.stderr)
    print(f"{len(links)} candidate links", file=sys.stderr)

    conds = set()
    for x in links:
        for side in ("A", "B"):
            for m in x.get(side, []) or []: conds.add(m["cond"])
    book = {}
    for i, c in enumerate(conds):
        tok = cond2yes.get(c)
        if not tok: book[c] = None; continue
        try:
            r = s.get(f"{CLOB}/book", params={"token_id": tok}, timeout=12).json()
            bids = sorted([(float(b["price"]), float(b["size"])) for b in r.get("bids", [])], key=lambda z: -z[0])
            asks = sorted([(float(x_["price"]), float(x_["size"])) for x_ in r.get("asks", [])])
            book[c] = dict(bid=bids[0][0] if bids else None, ask=asks[0][0] if asks else None,
                           bsz=bids[0][1] if bids else 0, asz=asks[0][1] if asks else 0)
        except Exception:
            book[c] = None
        if (i + 1) % 50 == 0: print(f"  {i+1}/{len(conds)} books", file=sys.stderr); time.sleep(0.02)

    def bk(c): return book.get(c)
    def ok(c): return bk(c) and bk(c)["bid"] is not None and bk(c)["ask"] is not None
    def cap_of(pairs):  # pairs of (cond, 'buy'|'sell')
        return min([(bk(c)["asz"] if side == "buy" else bk(c)["bsz"]) for c, side in pairs], default=0)

    results = []
    for x in links:
        A = x.get("A", []) or []; B = x.get("B", []) or []
        allc = [m["cond"] for m in A + B]
        if not all(ok(c) for c in allc): continue
        t = x["type"]; edge = trade = None; cap = 0
        if t == "DUPLICATE" and len(A) == 1 and len(B) == 1:
            a_, b_ = A[0]["cond"], B[0]["cond"]
            e1 = bk(b_)["bid"] - bk(a_)["ask"]; e2 = bk(a_)["bid"] - bk(b_)["ask"]
            if e1 >= e2: edge, trade, cap = e1, f"buy A@{bk(a_)['ask']} / sell B@{bk(b_)['bid']}", cap_of([(a_,'buy'),(b_,'sell')])
            else: edge, trade, cap = e2, f"buy B@{bk(b_)['ask']} / sell A@{bk(a_)['bid']}", cap_of([(b_,'buy'),(a_,'sell')])
        elif t == "COMPLEMENT" and len(A) == 1 and len(B) == 1:
            a_, b_ = A[0]["cond"], B[0]["cond"]
            buy = 1 - (bk(a_)["ask"] + bk(b_)["ask"]); sell = (bk(a_)["bid"] + bk(b_)["bid"]) - 1
            if buy >= sell: edge, trade, cap = buy, f"buy BOTH @ {bk(a_)['ask']}+{bk(b_)['ask']}", cap_of([(a_,'buy'),(b_,'buy')])
            else: edge, trade, cap = sell, f"sell BOTH @ {bk(a_)['bid']}+{bk(b_)['bid']}", cap_of([(a_,'sell'),(b_,'sell')])
        elif t == "IMPLICATION" and len(A) == 1 and len(B) == 1:
            a_, b_ = A[0]["cond"], B[0]["cond"]
            edge = bk(a_)["bid"] - bk(b_)["ask"]; trade = f"sell A@{bk(a_)['bid']} / buy B@{bk(b_)['ask']}"
            cap = cap_of([(a_, 'sell'), (b_, 'buy')])
        elif t == "SUBSET_SUM" and len(B) == 1 and len(A) >= 1:
            b_ = B[0]["cond"]
            sum_ask = sum(bk(m["cond"])["ask"] for m in A); sum_bid = sum(bk(m["cond"])["bid"] for m in A)
            e1 = bk(b_)["bid"] - sum_ask; e2 = sum_bid - bk(b_)["ask"]
            if e1 >= e2: edge, trade = e1, f"buy basket@{sum_ask:.3f} / sell B@{bk(b_)['bid']}"
            else: edge, trade = e2, f"sell basket@{sum_bid:.3f} / buy B@{bk(b_)['ask']}"
            cap = min([bk(m["cond"])["asz"] for m in A] + [bk(b_)["bsz"]])
        else:
            continue
        if edge is None: continue
        results.append(dict(src=x["_src"], type=t, edge=round(edge, 4), cap=round(cap or 0),
            conf=x.get("confidence"), title=x.get("title"), rationale=x.get("rationale"),
            risk=x.get("risk"), identity=x.get("identity"), trade=trade,
            legs=[(cond2meta.get(c, {}).get("title", "?") + " :: " + str(cond2meta.get(c, {}).get("o") or cond2meta.get(c, {}).get("q", ""))[:40]) for c in allc],
            conds=allc, ends=[cond2meta.get(c, {}).get("end") for c in allc]))
    results.sort(key=lambda r: -r["edge"])
    json.dump(results, open(f"{a.out}/link_candidates.json", "w"), indent=1, default=str)
    pos = [r for r in results if r["edge"] > 0.005]
    print(f"\n=== CROSS-EVENT LINKS WITH POSITIVE EXECUTABLE EDGE ===")
    print(f"{len(pos)} links with edge>0.5% (of {len(results)} checkable)\n")
    print(f"{'edge%':>6} {'cap$':>7} {'conf':>4} {'type':>11} {'src':>8}  title")
    for r in pos[:40]:
        print(f"{r['edge']*100:6.2f} {r['cap']:7.0f} {str(r['conf'])[:4]:>4} {r['type']:>11} {r['src']:>8}  {(r['title'] or '')[:46]}")
    print("\nNOTE: huge edge + tiny cap$ = stale-quote artifact OR an agent mislabel "
          "(check the two legs really are the same thing). Real finds = modest edge + real cap$.", file=sys.stderr)

if __name__ == "__main__":
    main()
