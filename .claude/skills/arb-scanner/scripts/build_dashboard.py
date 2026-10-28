"""build_dashboard.py — render PASS-only ideas.json into ONE self-contained dashboard.html.

The user's deliverable: a single .html they double-click (works offline). It opens with the best
ideas ranked by conviction (0-100), grouped into sections, and each idea expands into a deep-dive —
the headline block being "How I got the fair price," explained simply with a worked numeric example.

Dependency-free: inline CSS/JS template (assets/dashboard.html), data embedded as JSON, chart SVGs
base64-embedded as data: URIs (so there are zero external requests) — a single-file HTML pattern.

INPUT ideas.json schema:
{
 "generated_at": "YYYY-MM-DD HH:MM",
 "summary": {"headline": "...", "bottom_line": ["...", "..."]},
 "ideas": [{
   "section": "BOND|ARBITRAGE|DIRECTIONAL|SIGNAL",
   "title": "...", "one_liner": "...", "conviction": 0-100,
   "badge": "FADE", "headline_metric": "~220%/yr", "headline_metric_label": "annualized",
   "thesis": "...",
   "fair_price_explainer": "SOL is $63 now; ~4%/day vol; 1 day left -> ~8% it closes <$60, so YES~92c vs 88c market -> 4c edge.",
   "mispricing": "...", "market_price": "0.88", "fair_value": "~0.92",
   "edge_net": "+3.6%", "returns": "~$218 on $7.8k, ~8 days", "resolution_date": "2026-12-31",
   "execution": "buy YES @ 0.88 ...", "risk": "...",
   "sources": [{"label":"CME FedWatch","url":"https://..."}],
   "chart": {"file": "chart_sol60.svg", "type": "capacity|edge", "explainer": "(optional override)"},
   "verification": {"status":"PASS","checks":[{"name":"live price","verdict":"OK","evidence":"0.88 on CLOB"}],"notes":"..."},
   "backtest_spec": {...}   # SIGNAL only
 }],
 "rejected": [{"title":"...","section":"...","reason":"failed fact-check: source did not support claim"}]
}

Usage: python build_dashboard.py --ideas <RUN>/ideas.json --out <RUN>/dashboard.html
"""
import argparse, base64, json, os, sys

SECTIONS = [
    ("BOND",        "Near-certain “bond” trades"),
    ("ARBITRAGE",   "Arbitrage / relative value"),
    ("DIRECTIONAL", "Directional bets"),
    ("SIGNAL",      "Signal ideas — for your backtester"),
]
EXPLAINERS = {
 "capacity": ("How to read this: the X-axis is how much money you put in. The green line is the profit you "
   "lock at resolution; the orange dashed line is your return on capital (profit ÷ money in — your % yield). "
   "The circled peak is the most you can make before the edge runs out — roughly your max sensible size; past it, "
   "extra money earns less. All net of taker fees."),
 "edge": ("How to read this: the red mark is the market’s price = the probability the crowd implies. The green "
   "band is OUR estimated fair probability (how we got it is shown above). The gap between them is your edge. The two "
   "bars show what you’d win if right vs lose if wrong on a SAMPLE stake — an illustration of the payoff shape, "
   "NOT a recommended bet size. Size to your bankroll and the book depth."),
}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ideas", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--template", default=os.path.join(os.path.dirname(__file__), "..", "assets", "dashboard.html"))
    a = ap.parse_args()
    d = json.load(open(a.ideas))
    base = os.path.dirname(os.path.abspath(a.ideas))

    # embed charts + attach default explainers
    for it in d.get("ideas", []):
        ch = it.get("chart")
        if ch and ch.get("file"):
            p = ch["file"] if os.path.isabs(ch["file"]) else os.path.join(base, ch["file"])
            try:
                raw = open(p, "rb").read()
                ch["data_uri"] = "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii")
            except Exception as e:
                print(f"warn: chart {p}: {e}", file=sys.stderr); ch["data_uri"] = None
            if not ch.get("explainer"):
                ch["explainer"] = EXPLAINERS.get(ch.get("type"), "")

    # group into ordered sections (drop empty)
    by = {}
    for it in d.get("ideas", []):
        by.setdefault((it.get("section") or "DIRECTIONAL").upper(), []).append(it)
    sections = [{"key": k, "label": lbl, "ideas": by.get(k, [])} for k, lbl in SECTIONS if by.get(k)]
    # any unknown section keys -> append
    known = {k for k, _ in SECTIONS}
    for k, items in by.items():
        if k not in known:
            sections.append({"key": k, "label": k.title(), "ideas": items})

    data = {"generated_at": d.get("generated_at"), "summary": d.get("summary"),
            "sections": sections, "rejected": d.get("rejected", [])}

    tpl = open(os.path.abspath(a.template)).read()
    marker = "/*__EMBEDDED_DATA__*/ {}"
    if marker not in tpl:
        print("template missing data marker", file=sys.stderr); sys.exit(1)
    html = tpl.replace(marker, json.dumps(data, ensure_ascii=False))
    open(a.out, "w").write(html)
    n = sum(len(s["ideas"]) for s in sections)
    print(f"wrote {a.out}  ({n} ideas across {len(sections)} sections, {len(data['rejected'])} rejected)", file=sys.stderr)

if __name__ == "__main__":
    main()
