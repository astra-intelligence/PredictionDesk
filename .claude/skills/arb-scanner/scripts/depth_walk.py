"""depth_walk.py — Step 5: full-depth capacity curve for ONE trade.

The touch edge (from scan_markets / check_links) only tells you the edge on the FIRST few
shares. As you size up you walk down the bid ladders and up the ask ladders, and the edge
closes. This computes, for a range of sizes, the real profit / capital / return-on-capital,
using the FULL public L2 book (free via CLOB /book — no paid data needed for a live snapshot).

Trade spec (JSON, --trade <file>):
{
  "name": "human description",
  "settle_per_unit": 1,          # guaranteed $ that ONE fully-assembled unit pays at resolution
  "legs": [                      # each unit: buy/sell 1 of each leg
    {"cond": "0x..", "side": "buy",  "label": "binary Dem control"},
    {"cond": "0x..", "side": "sell", "label": "seat-count R<=47"}
  ]
}
side "buy"  -> we take YES at the ask;  side "sell" -> we short by taking the NO at its ask.
See references/edge-formulas.md for how to set settle_per_unit per structure (it's just: add
up what the assembled bundle pays in every outcome — for a correct hedge it's the same number
in all of them). profit(q) = settle_per_unit*q - total_cost(q); capital = total_cost(q).

Usage: python depth_walk.py --trade <spec.json> [--sizes 100,500,1000,...]
"""
import argparse, json, sys
import requests

GAMMA = "https://gamma-api.polymarket.com"; CLOB = "https://clob.polymarket.com"
s = requests.Session()

def tokens(cond):
    """return (yes_token, no_token) for a conditionId via Gamma."""
    r = s.get(f"{GAMMA}/markets", params={"condition_ids": cond}, timeout=20).json()
    m = r[0] if isinstance(r, list) and r else (r if isinstance(r, dict) else {})
    ct = m.get("clobTokenIds")
    try: ct = json.loads(ct) if isinstance(ct, str) else ct
    except Exception: ct = []
    return (ct[0] if ct else None, ct[1] if len(ct) > 1 else None)

def asks(token):
    r = s.get(f"{CLOB}/book", params={"token_id": token}, timeout=12).json()
    return sorted([(float(a["price"]), float(a["size"])) for a in r.get("asks", [])])

def walk_cost(ladder, q):
    need, cost = q, 0.0
    for p, sz in ladder:
        t = min(need, sz); cost += t * p; need -= t
        if need <= 1e-9: return cost
    return None  # insufficient depth

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trade", required=True)
    ap.add_argument("--sizes", default="100,250,500,1000,2000,4000,8000")
    ap.add_argument("--json", help="also write the capacity curve to this JSON file (for make_charts.py)")
    ap.add_argument("--fee-rate", dest="fee_rate", type=float, default=None,
                    help="taker fee rate (overrides spec.fee_rate); 0 for maker/geopolitical")
    a = ap.parse_args()
    spec = json.load(open(a.trade))
    settle = float(spec["settle_per_unit"])
    sizes = [int(x) for x in a.sizes.split(",")]
    # Taker fee = shares * rate * p * (1-p) per leg (p = fill price). Maker = 0; geopolitics = 0.
    # Set spec["fee_rate"] by category (crypto .07, sports .03, fin/pol/tech .04, econ/culture/weather .05).
    # VERIFY the current schedule online before trusting — it has changed. Default 0 = assume maker/geopolitical.
    fee_rate = float(a.fee_rate if a.fee_rate is not None else spec.get("fee_rate", 0))

    ladders = []
    print(f"TRADE: {spec.get('name')}  (settle_per_unit={settle})\n", file=sys.stderr)
    for leg in spec["legs"]:
        yes, no = tokens(leg["cond"])
        tok = yes if leg["side"] == "buy" else no   # buy YES ask, or short via NO ask
        lad = asks(tok) if tok else []
        ladders.append((leg, lad))
        top = lad[0] if lad else None
        print(f"  {leg['side']:4s} {str(leg.get('label',''))[:34]:34s} "
              f"touch={top[0] if top else None} sz={int(top[1]) if top else 0} levels={len(lad)}", file=sys.stderr)

    print(f"\ntaker fee_rate = {fee_rate} (0 = maker/geopolitical). profit shown is NET of taker fees.")
    print(f"{'q(units)':>8} {'cost$':>10} {'fee$':>8} {'gross$':>9} {'net$':>9} {'capital$':>9} {'netROC%':>7}")
    best = (0, -1e9); curve = []
    for q in sizes:
        costs = [walk_cost(lad, q) for _, lad in ladders]
        if any(c is None for c in costs):
            print(f"{q:8d}   insufficient depth on >=1 leg"); continue
        tot = sum(costs); payout = settle * q; gross = payout - tot
        # per-leg taker fee = q * rate * p * (1-p), with p = leg cost / q
        fee = sum(fee_rate * c * (1 - (c / q)) for c in costs) if fee_rate else 0.0
        net = gross - fee
        roc = net / tot * 100 if tot else 0
        print(f"{q:8d} {tot:10.1f} {fee:8.2f} {gross:9.2f} {net:9.2f} {tot:9.0f} {roc:7.2f}")
        curve.append({"q": q, "capital": round(tot, 2), "gross_profit": round(gross, 2),
                      "fee": round(fee, 2), "profit": round(net, 2), "roc_pct": round(roc, 3)})
        if net > best[1]: best = (q, net)
    print(f"\nMax profit ~${best[1]:.0f} near q={best[0]} units. Beyond the peak, marginal units lose "
          f"(edge fully consumed). Profit is locked until resolution unless you can exit by convergence.", file=sys.stderr)
    if a.json:
        json.dump({"name": spec.get("name"), "settle_per_unit": settle, "curve": curve,
                   "max_profit": round(best[1], 2), "max_profit_q": best[0]}, open(a.json, "w"), indent=1)
        print(f"wrote curve -> {a.json}", file=sys.stderr)

if __name__ == "__main__":
    main()
