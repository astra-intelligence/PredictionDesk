"""bond_screen.py — find NEAR-CERTAIN, soon-resolving markets trading at a DISCOUNT.

The "bond" trade (the user's term): a market whose outcome is essentially already decided by its
resolution rule + current facts, that resolves very soon, but trades a few cents below certainty
because holders sell early to free up capital (exit impatience). Buy $1 for ~96¢, collect $1 in days
= high annualized, low (not zero) risk. Net of taker fee these still clear because the fee at p≈0.97
is tiny.

This screen only SURFACES candidates by shape (near-certain price band + imminent + liquid). It cannot
tell a genuine 97% probability from an exit-discounted 99.7% one — that is the whole job of the next
step: READ THE RESOLUTION RULE + RECENT NEWS and decide whether reality is materially more certain
than the price. Only then is it a trade.

Side handled both ways: a YES at 0.90-0.985 (buy YES at a discount) OR a YES at 0.015-0.10 (the YES is
a near-certain NO -> buy NO at a discount). Output frames it as "buy <token> @ <ask>, collect $1".

Reads <RUN>/catalog.json. Writes <RUN>/bond_candidates.json + prints ranked shortlist.
Usage: python bond_screen.py --out <RUN> [--max-days 14] [--min-liq 3000] [--fee-rate 0.04]
"""
import argparse, json, sys
from datetime import datetime, timezone

NOW = datetime.now(timezone.utc)
def days_to(end):
    try: return max(0.0, (datetime.fromisoformat((end+"T12:00:00+00:00").replace("Z","")) - NOW).total_seconds()/86400)
    except Exception: return None

# Pure-chance markets: a high price here = the outcome's TRUE probability, not an exit-pressure
# discount. They dominate the price band and are NOT bonds. Excluded by default (--keep-chance to keep).
CHANCE_KW = ["exact score", "o/u", "over/under", "1+ goal", "2+ goal", "1+ assist", "to score",
    "anytime", "call of duty", "rainbow six", "counter-strike", "league of legends", "dota",
    "valorant", "csgo", " bo1", " bo3", " bo5", "map 1", "first map"]
CHANCE_TAGS = {"sports", "games", "esports", "soccer"}

def is_chance(e, l):
    t = (" ".join(e.get("tags") or [])).lower()
    if any(tag in t for tag in CHANCE_TAGS): return True
    s = ((l.get("q") or "") + " " + (e.get("title") or "")).lower()
    return any(k in s for k in CHANCE_KW)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-days", type=float, default=14, help="only markets resolving within N days")
    ap.add_argument("--min-liq", type=float, default=3000)
    ap.add_argument("--fee-rate", type=float, default=0.04, help="taker fee rate for net calc (0 = geopolitics/maker)")
    ap.add_argument("--keep-chance", action="store_true", help="keep pure-chance markets (sports/exact-score/props) — off by default")
    a = ap.parse_args()
    cat = json.load(open(f"{a.out}/catalog.json"))
    rows = []; n_chance = 0
    for e in cat:
        for l in e["legs"]:
            if not a.keep_chance and is_chance(e, l):
                n_chance += 1; continue
            bb, ba, liq = l.get("bb"), l.get("ba"), l.get("liq") or 0
            if bb is None or ba is None or liq < a.min_liq: continue
            dte = days_to(l.get("end") or "")
            if dte is None or dte > a.max_days or dte <= 0: continue
            yes_ask = ba
            # side that buys "near-certainty" at a discount and collects $1
            if 0.90 <= yes_ask <= 0.985:
                side, buy_price = "BUY YES", yes_ask
            else:
                # near-certain NO: buy the NO at a discount. NO ask ≈ 1 - YES bid.
                no_ask = 1 - bb
                if 0.90 <= no_ask <= 0.985:
                    side, buy_price = "BUY NO", no_ask
                else:
                    continue
            disc = 1 - buy_price                      # gross discount = profit per $1 if it resolves your way
            fee = a.fee_rate * buy_price * (1 - buy_price)   # taker fee per share
            net = disc - fee
            if net <= 0: continue
            ann = (net / buy_price) * 365 / dte
            rows.append(dict(event=e["title"], outcome=l.get("o"), question=l.get("q"),
                cond=l.get("cond"), tags=e.get("tags") or [],
                side=side, buy_price=round(buy_price,3), gross_discount=round(disc,3),
                fee_per_share=round(fee,4), net_per_share=round(net,4),
                annualized_pct=round(ann*100,0), dte_days=round(dte,1), liq=round(liq)))
    # rank by annualized net (sooner + bigger discount + liquid), tie-break liquidity
    rows.sort(key=lambda r: (r["annualized_pct"], r["liq"]), reverse=True)
    json.dump(rows, open(f"{a.out}/bond_candidates.json","w"), indent=1, default=str)
    print(f"\n=== NEAR-CERTAIN / BOND CANDIDATES (confirm certainty by reading rules + news!) ===")
    print(f"{len(rows)} candidates resolving <= {a.max_days:.0f}d at a discount, liq >= ${a.min_liq:.0f} "
          f"(excluded {n_chance} pure-chance sports/prop legs whose high price is just true probability, not a bond)\n")
    print(f"{'side':>7} {'buy@':>5} {'disc':>5} {'net':>6} {'ann%':>7} {'dte':>5} {'liq$':>9}  market")
    for r in rows[:40]:
        print(f"{r['side']:>7} {r['buy_price']:5.2f} {r['gross_discount']*100:4.1f}% "
              f"{r['net_per_share']*100:5.2f}% {r['annualized_pct']:7.0f} {r['dte_days']:5.1f} {r['liq']:9.0f}  "
              f"{(r['question'] or r['event'])[:44]}")
    print("\nNEXT (critical): these are price-SHAPED candidates only. A REAL bond needs the outcome to be "
          "GENUINELY DECIDED by the rule + current facts (a game already won, a deal that won't be signed by "
          "the date, a number already published/locked) — NOT just a naturally-high-probability event. For each "
          "you pursue, READ the rule (resolution.py) + recent news and confirm reality is materially MORE certain "
          "than the price (e.g. truly ~99.5% but trading 0.96 from exit selling). A 0.96 that is truly 96% is NOT "
          "an edge. Dig for SEVERAL real ones (event/geopolitics/macro/crypto-level), don't stop at one; rank by "
          "decidedness × liquidity, not raw annualized (same-day legs inflate it). Net of taker fee shown; rest as maker for 0.", file=sys.stderr)

if __name__ == "__main__":
    main()
