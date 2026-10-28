"""partition_catalog.py — Step 2 of the arb scan.

Splits the catalog into themed slices so the cross-event link-finder agents each get a
focused, digestible chunk. Cross-event logical links almost always live within a theme
(all Fed markets, all BTC-price markets, all midterm markets) because that's where the
same underlying is priced by multiple separate events that different crowds trade.

Usage: python partition_catalog.py --out <run_dir>
Reads <run_dir>/catalog.json ; writes <run_dir>/slice_<theme>.json + prints sizes.
"""
import argparse, json, re, sys

# Order matters: an event is claimed by the FIRST theme it matches. Politics is checked before macro
# so political races (e.g. "Next UK PM", elections) aren't grabbed by macro keywords like "bank of".
THEMES = {
 "crypto": r"\b(bitcoin|btc|ethereum|\beth\b|solana|\bsol\b|crypto|coinbase|dogecoin|xrp|ripple|binance|stablecoin|microstrategy)\b",
 "politics": r"\b(election|senate|house|governor|president|midterm|nominee|primary|congress|parliament|prime minister|\bpm\b|approval|impeach|cabinet|secretary|supreme court|shutdown|speaker|balance of power)\b",
 "macro": r"\b(fed|federal reserve|interest rate|rate (cut|hike)|inflation|cpi|pce|gdp|ecb|boe|central bank|recession|unemploy|jobs|payroll|jobless|treasury|powell|fomc|basis point|bps|ipo|earnings|nasdaq|s&p|dow|vix|tariff)\b",
 "sports": r"\b(world cup|champion|final|semifinal|quarterfinal|win the|advance|group stage|playoff|super bowl|nba|nfl|premier league|tennis|grand slam|series)\b",
}

def text(e):
    return (e["title"] or "") + " " + " ".join((l["q"] or "") for l in e["legs"]) + " " + " ".join(e["tags"] or [])

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cat = json.load(open(f"{a.out}/catalog.json"))
    assigned, counts = set(), {}
    for name, pat in THEMES.items():
        rx = re.compile(pat, re.I); sl = []
        for e in cat:
            if e["id"] in assigned: continue
            if rx.search(text(e)): sl.append(e); assigned.add(e["id"])
        comp = [dict(title=e["title"], negRisk=e["negRisk"], vol1mo=round(e["vol1mo"] or 0),
                legs=[dict(o=l["o"], q=l["q"], bb=l["bb"], ba=l["ba"], cond=l["cond"],
                          end=l["end"], liq=round(l["liq"] or 0)) for l in e["legs"]]) for e in sl]
        json.dump(comp, open(f"{a.out}/slice_{name}.json", "w"), indent=0, default=str)
        counts[name] = len(sl)
    print(f"slice sizes: {counts} | unassigned: {len(cat)-len(assigned)} | total: {len(cat)}", file=sys.stderr)

if __name__ == "__main__":
    main()
