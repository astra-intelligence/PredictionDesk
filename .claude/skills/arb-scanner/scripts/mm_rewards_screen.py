"""mm_rewards_screen.py — Step 2b: rank markets by liquidity-rewards yield potential (maker income).

Polymarket pays makers DAILY (midnight UTC) from a per-market rewards pool. The economics are
machine-readable (research-notes.md §2): quadratic tightness scoring, ~3x for two-sided quotes,
one-sided scores ZERO when mid is outside [0.10, 0.90]. Realized yield is pro-rata vs competing
makers, so this screen RANKS candidates by a reward-vs-competition proxy (poly-maker's
gm_reward_per_100 idea): daily reward rate relative to resting liquidity, filtered to markets where
quoting is safest (mid well inside [0.10,0.90], real spread room, decent DTE so quotes aren't picked
off at resolution).

This surfaces MAKER-YIELD candidates — income from quoting, not a directional view. The lane agent
must still judge adverse-selection risk (news-driven markets pick you off; prefer slow/low-vol).

Usage: python mm_rewards_screen.py --out <run_dir> [--min-daily-rate 10] [--top 40]
Reads <run_dir>/catalog.json (for liq/end dates); writes <run_dir>/mm_candidates.json.
"""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime, timezone
import requests

CLOB = "https://clob.polymarket.com"

def f(x, d=None):
    try: return float(x)
    except Exception: return d

def pull_sampling_markets(s):
    """All reward-carrying markets via CLOB /sampling-markets (paginated)."""
    out, cursor = [], ""
    while True:
        r = s.get(f"{CLOB}/sampling-markets", params={"next_cursor": cursor} if cursor else {}, timeout=30)
        b = r.json()
        data = b.get("data", [])
        out.extend(data)
        cursor = b.get("next_cursor")
        if not cursor or cursor == "LTE=" or not data: break
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-daily-rate", type=float, default=10.0, help="min $ rewards/day to bother")
    ap.add_argument("--min-dte", type=float, default=2.0, help="skip markets resolving sooner (pick-off risk)")
    ap.add_argument("--top", type=int, default=40)
    a = ap.parse_args()
    s = requests.Session()

    # catalog: cond -> (liq, end, title) for competition proxy + DTE filter
    liq_by_cond, end_by_cond, title_by_cond = {}, {}, {}
    try:
        for ev in json.load(open(f"{a.out}/catalog.json")):
            for l in ev.get("legs", []):
                if l.get("cond"):
                    liq_by_cond[l["cond"]] = f(l.get("liq"), 0.0)
                    end_by_cond[l["cond"]] = l.get("end") or ""
                    title_by_cond[l["cond"]] = l.get("q") or ev.get("title") or ""
    except FileNotFoundError:
        print("warn: no catalog.json — run scan_markets first (liquidity proxy will be missing)", file=sys.stderr)

    now = datetime.now(timezone.utc)
    def dte(cond):
        e = end_by_cond.get(cond) or ""
        try: return max(0.0, (datetime.fromisoformat(e + "T12:00:00+00:00") - now).total_seconds() / 86400)
        except Exception: return None

    mkts = pull_sampling_markets(s)
    print(f"sampling-markets (reward-carrying): {len(mkts)}", file=sys.stderr)

    cands = []
    for m in mkts:
        cond = m.get("condition_id")
        rw = m.get("rewards") or {}
        rate = sum(f(r.get("rewards_daily_rate"), 0.0) for r in (rw.get("rates") or []))
        if rate < a.min_daily_rate: continue
        toks = m.get("tokens") or []
        mid = f(toks[0].get("price")) if toks else None
        if mid is None: continue
        two_sided_safe = 0.10 <= mid <= 0.90           # outside -> one-sided scores 0, forced pick-off exposure
        d = dte(cond)
        if d is not None and d < a.min_dte: continue
        liq = liq_by_cond.get(cond, 0.0)
        # reward-vs-competition proxy: $/day per $100 of existing resting liquidity (higher = less crowded)
        score = rate / max(liq, 100.0) * 100.0
        cands.append(dict(cond=cond, question=(m.get("question") or title_by_cond.get(cond) or "")[:90],
                          daily_rate=round(rate, 2), min_size=f(rw.get("min_size"), None),
                          max_spread=f(rw.get("max_spread"), None), mid=round(mid, 3),
                          two_sided_safe=two_sided_safe, dte_days=round(d, 1) if d is not None else None,
                          liq=round(liq), reward_per_100liq=round(score, 3)))
    cands.sort(key=lambda c: -c["reward_per_100liq"])
    cands = cands[: max(a.top * 3, 60)]                 # keep a wider tail in the file than we print
    json.dump(cands, open(f"{a.out}/mm_candidates.json", "w"), indent=1)

    print(f"\n=== TOP MAKER-YIELD CANDIDATES (rank = $/day per $100 resting liq; judge adverse-selection!) ===")
    print(f"{'$/day':>7} {'per$100':>8} {'mid':>5} {'2side':>5} {'dte':>5} {'liq$':>9}  question")
    for c in cands[: a.top]:
        print(f"{c['daily_rate']:7.0f} {c['reward_per_100liq']:8.2f} {c['mid']:5.2f} "
              f"{'OK' if c['two_sided_safe'] else 'RISK':>5} {str(c['dte_days'] or '-'):>5} {c['liq']:9.0f}  {c['question'][:60]}")
    print(f"\nwrote {len(cands)} -> {a.out}/mm_candidates.json  (params are LIVE per-market data; re-pull every run)", file=sys.stderr)

if __name__ == "__main__":
    main()
