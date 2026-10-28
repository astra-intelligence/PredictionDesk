"""resolution.py — Step 6: pull the FULL resolution rules for the legs of a candidate.

The edge means nothing if the two legs can resolve inconsistently. This dumps each market's
full description (the resolution criteria), end date, and resolution source, so you can read
them side by side and judge: is the logical identity airtight (risk-free arb), or can the
definitions diverge (relative value with basis risk)? See references/resolution-risk.md.

Usage: python resolution.py --conds 0xabc,0xdef,...
"""
import argparse, json, sys, textwrap
import requests

GAMMA = "https://gamma-api.polymarket.com"
s = requests.Session()

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--conds", required=True)
    a = ap.parse_args()
    for cond in a.conds.split(","):
        cond = cond.strip()
        try:
            r = s.get(f"{GAMMA}/markets", params={"condition_ids": cond}, timeout=20).json()
            m = r[0] if isinstance(r, list) and r else (r if isinstance(r, dict) else {})
        except Exception as e:
            print(f"\n{cond}: fetch error {e}"); continue
        print("=" * 90)
        print(f"Q: {m.get('question')}")
        print(f"   cond={cond}  end={m.get('endDate')}  closed={m.get('closed')}  "
              f"umaResolutionStatuses={m.get('umaResolutionStatuses')}")
        src = m.get("resolutionSource") or ""
        if src: print(f"   resolutionSource: {src}")
        desc = (m.get("description") or "").strip()
        print("   RULES: " + textwrap.fill(desc, 100, subsequent_indent="          ")[:1600])

if __name__ == "__main__":
    main()
