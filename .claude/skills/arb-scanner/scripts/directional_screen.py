"""directional_screen.py — surface candidate markets worth forming a VIEW on.

Directional edge = price-vs-reality (the market's probability is wrong), not price-vs-logic.
This is a light screen only: it surfaces tradeable, researchable markets and tags the angle
(favorite-longshot extreme / inefficient small market / near-term catalyst). The actual edge
comes from the LLM researching each candidate's real-world question and forming a grounded fair
value (see references/directional-plays.md) — NOT from this script.

Reads <RUN>/catalog.json. Writes <RUN>/directional_candidates.json + prints a shortlist.
Usage: python directional_screen.py --out <RUN> [--max-days 45] [--min-liq 5000]
"""
import argparse, json, sys
from datetime import datetime, timezone

NOW = datetime.now(timezone.utc)
def days_to(end):
    try: return max(0.0, (datetime.fromisoformat((end+"T12:00:00+00:00").replace("Z","")) - NOW).total_seconds()/86400)
    except Exception: return None

def theme_of(tags, text):
    t = " ".join(tags or []).lower() + " " + (text or "").lower()
    for name, kws in [
        ("crypto", ["crypto", "bitcoin", "btc", "ethereum", "solana"]),
        ("politics", ["election", "senate", "house", "president", "governor", "primary", "midterm", "congress", "approval"]),
        ("geopolitics", ["geopolit", "war", "ceasefire", "strait", "iran", "israel", "russia", "ukraine", "nato", "hostage"]),
        ("econ", ["fed", "inflation", "cpi", "gdp", "rate", "recession", "jobs", "unemploy", "interest"]),
        ("finance", ["finance", "ipo", "earnings", "stock", "nasdaq", "s&p", "tech", "market cap"]),
        ("sports", ["sport", "soccer", "nba", "nfl", "world cup", "tennis", "match", "champion"]),
        ("culture", ["culture", "movie", "box office", "award", "oscar", "music", "celebrity", "tv"]),
    ]:
        if any(k in t for k in kws): return name
    return "other"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-days", type=float, default=45, help="only markets resolving within N days (catalyst soon)")
    ap.add_argument("--min-liq", type=float, default=5000, help="min leg liquidity to be tradeable")
    a = ap.parse_args()
    cat = json.load(open(f"{a.out}/catalog.json"))
    rows = []
    for e in cat:
        tags = e.get("tags") or []
        for l in e["legs"]:
            bb, ba, liq = l.get("bb"), l.get("ba"), l.get("liq") or 0
            if bb is None or ba is None or liq < a.min_liq: continue
            mid = (bb+ba)/2; spread = ba-bb
            dte = days_to(l.get("end") or "")
            if dte is None or dte > a.max_days: continue
            if not (0.06 <= mid <= 0.94): continue           # skip already-decided
            angles = []
            if mid <= 0.15: angles.append("longshot_fade?")   # retail overpays longshots -> maybe sell YES
            if mid >= 0.85: angles.append("near-certainty_overpay?")  # retail overpays certainty -> maybe sell YES (buy NO)
            if 0.35 <= mid <= 0.65: angles.append("midrange_underpriced?")  # often underpriced
            if liq < 100000: angles.append("small_inefficient(<100k)")
            if dte <= 7: angles.append("catalyst<=7d")
            rows.append(dict(event=e["title"], outcome=l.get("o"), question=l.get("q"),
                cond=l.get("cond"), tags=tags, theme=theme_of(tags, e["title"]+" "+(l.get("q") or "")),
                mid=round(mid,3), spread=round(spread,3),
                liq=round(liq), dte_days=round(dte,1), angles=angles))
    # rank within theme: prefer liquid, near-term, with an angle; small-but-tradeable bonus
    def score(r):
        s = (1 if r["angles"] else 0) * 2
        s += min(r["liq"]/50000, 4)                 # liquidity (capped)
        s += max(0, (45 - r["dte_days"]) / 45)      # sooner = better
        s += 1 if any("small" in x for x in r["angles"]) else 0
        return s
    rows.sort(key=score, reverse=True)
    # DIVERSIFY: one event contributes at most a couple legs; interleave across themes so the
    # shortlist spans politics/econ/crypto/geopolitics/sports/culture, not 3 variants of one event.
    from collections import defaultdict
    by_theme = defaultdict(list); seen_event = defaultdict(int)
    for r in rows:
        if seen_event[r["event"]] >= 1:   # at most 1 leg per event in the shortlist
            continue
        seen_event[r["event"]] += 1
        by_theme[r["theme"]].append(r)
    shortlist = []
    order = sorted(by_theme, key=lambda t: -len(by_theme[t]))
    i = 0
    while len(shortlist) < 30 and any(by_theme[t][i:] for t in order if i < len(by_theme[t])):
        for t in order:
            if i < len(by_theme[t]):
                shortlist.append(by_theme[t][i])
        i += 1
    json.dump({"shortlist": shortlist[:30], "all": rows}, open(f"{a.out}/directional_candidates.json","w"), indent=1, default=str)
    print(f"\n=== DIRECTIONAL CANDIDATES — diversified across themes (research these) ===")
    counts = {t: len(v) for t, v in by_theme.items()}
    print(f"{len(rows)} markets resolving <= {a.max_days:.0f}d, liq >= ${a.min_liq:.0f}; by theme: {counts}\n")
    print(f"{'theme':>11} {'mid':>5} {'liq$':>9} {'dte':>5}  angles / market")
    for r in shortlist[:30]:
        print(f"{r['theme']:>11} {r['mid']:5.2f} {r['liq']:9.0f} {r['dte_days']:5.1f}  "
              f"{','.join(r['angles']) or '-':32s} | {(r['question'] or r['event'])[:44]}")
    print("\nNEXT: research a DIVERSE handful (ideally the best idea from several themes, not multiple "
          "deadlines of one event). Estimate fair value from cited sources; trade only where "
          "|fair - price| beats spread + taker fee + a margin. See references/directional-plays.md. "
          "These are RISK trades, not locks.", file=sys.stderr)

if __name__ == "__main__":
    main()
