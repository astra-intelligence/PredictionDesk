"""make_charts.py — render a trade chart to a self-contained SVG (no matplotlib).

A chart on its own isn't useful — so every chart carries a plain-English TAKEAWAY line and labels
the decision-relevant numbers directly. Two modes:

  capacity  (arb/RV):    profit & ROC vs capital, peak marked. From `depth_walk.py --json`.
      python make_charts.py --depthwalk <RUN>/depthwalk_<name>.json --out <RUN>/chart_<name>.svg \
             --takeaway "Best size ~$4k locks $34; edge gone past $8k."

  edge      (directional): market price vs your fair-value band (the gap = the edge), plus the
      win/lose payoff in dollars. From an edge spec JSON you write:
      {"name":"...", "market_pct":58, "fair_low":70, "fair_high":80,
       "win_usd":6700, "lose_usd":9300, "takeaway":"Market prices 58%, I think 70-80% -> buy."}
      python make_charts.py --edge <RUN>/edge_<name>.json --out <RUN>/chart_<name>.svg

Always pass/include a takeaway — the chart should make the decision obvious at a glance.
"""
import argparse, html, json, sys

W, H = 760, 470
ML, MR, MT = 70, 64, 64       # left/right/top margins (top leaves room for title + takeaway)
MB = 64

def esc(s): return html.escape(str(s))

def header(s, title, takeaway):
    s.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="system-ui,Arial" font-size="12">')
    s.append(f'<rect width="{W}" height="{H}" fill="white"/>')
    s.append(f'<text x="{W/2}" y="26" text-anchor="middle" font-size="15" font-weight="bold">{esc(title[:80])}</text>')
    if takeaway:
        s.append(f'<text x="{W/2}" y="46" text-anchor="middle" font-size="12.5" fill="#234">{esc(takeaway[:110])}</text>')

def capacity(d, out, takeaway):
    c = d.get("curve", [])
    if not c: print("no curve data", file=sys.stderr); sys.exit(1)
    takeaway = takeaway or d.get("takeaway")
    cap = [r["capital"] for r in c]; profit = [r["profit"] for r in c]; roc = [r["roc_pct"] for r in c]
    pw, ph = W - ML - MR, H - MT - MB
    xmax = max(cap) or 1; pmin, pmax = min(0, min(profit)), max(profit) or 1
    rmin, rmax = min(0, min(roc)), max(roc) or 1
    X = lambda v: ML + (v / xmax) * pw
    Yp = lambda v: MT + ph - (v - pmin) / (pmax - pmin or 1) * ph
    Yr = lambda v: MT + ph - (v - rmin) / (rmax - rmin or 1) * ph
    s = []; header(s, d.get("name") or "capacity curve", takeaway)
    s.append(f'<line x1="{ML}" y1="{MT}" x2="{ML}" y2="{MT+ph}" stroke="#333"/>')
    s.append(f'<line x1="{ML}" y1="{MT+ph}" x2="{ML+pw}" y2="{MT+ph}" stroke="#333"/>')
    if pmin < 0 < pmax: s.append(f'<line x1="{ML}" y1="{Yp(0):.1f}" x2="{ML+pw}" y2="{Yp(0):.1f}" stroke="#ccc" stroke-dasharray="3"/>')
    for x in cap:
        s.append(f'<text x="{X(x):.1f}" y="{MT+ph+18}" text-anchor="middle" fill="#555">${x:,.0f}</text>')
    for f in (0, 0.5, 1):
        v = pmin + f*(pmax-pmin); s.append(f'<text x="{ML-8}" y="{Yp(v)+4:.1f}" text-anchor="end" fill="#1a8">${v:,.0f}</text>')
        v = rmin + f*(rmax-rmin); s.append(f'<text x="{ML+pw+8}" y="{Yr(v)+4:.1f}" text-anchor="start" fill="#c63">{v:.1f}%</text>')
    s.append(f'<polyline points="{" ".join(f"{X(x):.1f},{Yr(y):.1f}" for x,y in zip(cap,roc))}" fill="none" stroke="#c63" stroke-width="2" stroke-dasharray="5 4" opacity="0.85"/>')
    s.append(f'<polyline points="{" ".join(f"{X(x):.1f},{Yp(y):.1f}" for x,y in zip(cap,profit))}" fill="none" stroke="#1a8" stroke-width="2.5"/>')
    for x, y in zip(cap, profit): s.append(f'<circle cx="{X(x):.1f}" cy="{Yp(y):.1f}" r="3.2" fill="#1a8"/>')
    peak = next((r for r in c if r["q"] == d.get("max_profit_q")), None)
    if peak:
        px, py = X(peak["capital"]), Yp(peak["profit"])
        s.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="5.5" fill="none" stroke="#111" stroke-width="1.5"/>')
        s.append(f'<text x="{px:.1f}" y="{py-12:.1f}" text-anchor="middle" font-weight="bold">peak ${d.get("max_profit",0):,.0f}</text>')
    s.append(f'<text x="{ML+pw/2}" y="{H-14}" text-anchor="middle" fill="#555">capital deployed ($) — profit is NET of taker fees</text>')
    s.append(f'<text x="{ML+10}" y="{MT+14}" fill="#1a8">— profit ($)</text><text x="{ML+86}" y="{MT+14}" fill="#c63">- - return on capital (%)</text>')
    s.append('</svg>'); open(out, "w").write("\n".join(s))

def edge(d, out, takeaway, win_label=None, lose_label=None):
    takeaway = takeaway or d.get("takeaway")
    mkt = float(d["market_pct"]); flo = float(d["fair_low"]); fhi = float(d.get("fair_high", flo))
    win = float(d.get("win_usd", 0)); lose = float(d.get("lose_usd", 0))
    # for a FADE (buy NO) the win case is NO — pass win_label/lose_label (or set them in the spec).
    wlab = win_label or d.get("win_label", "if YES")
    llab = lose_label or d.get("lose_label", "if NO")
    pw = W - ML - MR
    s = []; header(s, d.get("name") or "directional edge", takeaway)
    # --- probability track (0..100%) ---
    ty = 120; th = 34
    X = lambda p: ML + (p/100.0) * pw
    s.append(f'<text x="{ML}" y="{ty-12}" font-weight="bold" fill="#234">Implied probability — the gap is your edge</text>')
    s.append(f'<rect x="{ML}" y="{ty}" width="{pw}" height="{th}" fill="#f0f2f5" stroke="#ccc"/>')
    for p in (0,25,50,75,100):
        s.append(f'<line x1="{X(p):.1f}" y1="{ty}" x2="{X(p):.1f}" y2="{ty+th+4}" stroke="#ccc"/>')
        s.append(f'<text x="{X(p):.1f}" y="{ty+th+18}" text-anchor="middle" fill="#777">{p}%</text>')
    # fair-value band
    s.append(f'<rect x="{X(flo):.1f}" y="{ty}" width="{max(2,X(fhi)-X(flo)):.1f}" height="{th}" fill="#1a8" opacity="0.30"/>')
    fmid = (flo+fhi)/2
    s.append(f'<text x="{X(fmid):.1f}" y="{ty+th+34}" text-anchor="middle" fill="#1a8" font-weight="bold">fair {flo:.0f}-{fhi:.0f}%</text>')
    # market marker
    s.append(f'<line x1="{X(mkt):.1f}" y1="{ty-6}" x2="{X(mkt):.1f}" y2="{ty+th+6}" stroke="#c33" stroke-width="2.5"/>')
    s.append(f'<text x="{X(mkt):.1f}" y="{ty-10}" text-anchor="middle" fill="#c33" font-weight="bold">market {mkt:.0f}%</text>')
    # edge arrow
    s.append(f'<line x1="{X(mkt):.1f}" y1="{ty+th/2:.1f}" x2="{X(fmid):.1f}" y2="{ty+th/2:.1f}" stroke="#111" stroke-width="1.5" marker-end="url(#ar)"/>')
    s.append('<defs><marker id="ar" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6" fill="#111"/></marker></defs>')
    s.append(f'<text x="{(X(mkt)+X(fmid))/2:.1f}" y="{ty+th/2-8:.1f}" text-anchor="middle" font-weight="bold">edge ~{abs(fmid-mkt):.0f} pts</text>')
    # --- payoff bars ---
    by = 250; bh = 120; cx = ML + 80; bw = 90
    s.append(f'<text x="{ML}" y="{by-10}" font-weight="bold" fill="#234">Payoff on your size (this is a BET — you can lose the stake)</text>')
    base = by + bh
    maxv = max(win, lose, 1)
    wh = win/maxv*bh; lh = lose/maxv*bh
    s.append(f'<line x1="{ML}" y1="{base}" x2="{ML+pw}" y2="{base}" stroke="#333"/>')
    s.append(f'<rect x="{cx}" y="{base-wh:.1f}" width="{bw}" height="{wh:.1f}" fill="#1a8"/>')
    s.append(f'<text x="{cx+bw/2}" y="{base-wh-8:.1f}" text-anchor="middle" fill="#1a8" font-weight="bold">{esc(wlab)}: +${win:,.0f}</text>')
    s.append(f'<rect x="{cx+bw+80}" y="{base}" width="{bw}" height="{lh:.1f}" fill="#c33"/>')
    s.append(f'<text x="{cx+bw+80+bw/2}" y="{base+lh+16:.1f}" text-anchor="middle" fill="#c33" font-weight="bold">{esc(llab)}: -${lose:,.0f}</text>')
    s.append('</svg>'); open(out, "w").write("\n".join(s))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depthwalk", help="capacity-curve JSON from depth_walk.py --json")
    ap.add_argument("--edge", help="directional edge spec JSON")
    ap.add_argument("--out", required=True)
    ap.add_argument("--takeaway", help="plain-English one-line conclusion drawn on the chart")
    ap.add_argument("--win-label", dest="win_label", help="edge chart: payoff bar label for the winning outcome (e.g. 'if NO' for a fade)")
    ap.add_argument("--lose-label", dest="lose_label", help="edge chart: payoff bar label for the losing outcome")
    a = ap.parse_args()
    if a.edge:
        edge(json.load(open(a.edge)), a.out, a.takeaway, a.win_label, a.lose_label)
    elif a.depthwalk:
        capacity(json.load(open(a.depthwalk)), a.out, a.takeaway)
    else:
        print("pass --depthwalk or --edge", file=sys.stderr); sys.exit(1)
    print(f"wrote {a.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
