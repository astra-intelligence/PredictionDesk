"""Coverage / data-fidelity report over the poll ledger.

Answers "what data am I missing, and when" — distinguishing collector DOWNTIME (no poll record)
and per-market FAILURE (market in a failed batch) from a quiet market that simply didn't change.

Reads `poll_ledger.jsonl` (one record per poll: poll_ts, attempted, tokens_ok, batches_failed,
failed_markets). Part files / book parquet are NOT needed — the ledger is the source of truth for
coverage.

Usage:
  python3 -m desk.data.capture_coverage [--out_dir DIR] [--since 6h|2d|<unix>]
                                        [--market <cond|slug|question-substr>] [--poll-secs N]
                                        [--gap-threshold S] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Dict, List, Optional


def load_ledger(out_dir: str, since_ts: int = 0) -> List[dict]:
    path = os.path.join(out_dir, "poll_ledger.jsonl")
    rows: List[dict] = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue  # partial last line being appended — skip
            if e.get("poll_ts", 0) >= since_ts:
                rows.append(e)
    rows.sort(key=lambda e: e["poll_ts"])
    return rows


def _universe(out_dir: str) -> Dict[str, dict]:
    p = os.path.join(out_dir, "_universe.json")
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p)).get("tokens", {})
    except (ValueError, OSError):
        return {}


def resolve_market(out_dir: str, q: str) -> Optional[str]:
    """Resolve a cond id / slug / question substring to a conditionId."""
    by_cond = {}
    for m in _universe(out_dir).values():
        by_cond[m["market_id"]] = m
    if q in by_cond:
        return q
    ql = q.lower()
    for cond, m in by_cond.items():
        if ql in (m.get("slug") or "").lower() or ql in (m.get("question") or "").lower():
            return cond
    return None


def _merge_windows(ts_pairs: List[tuple]) -> List[tuple]:
    """Merge [(start,end)] intervals that touch/overlap."""
    if not ts_pairs:
        return []
    ts_pairs = sorted(ts_pairs)
    out = [list(ts_pairs[0])]
    for s, e in ts_pairs[1:]:
        if s <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [tuple(x) for x in out]


def coverage(out_dir: str, since_ts: int = 0, market: Optional[str] = None,
             poll_secs: float = 10.0, gap_threshold: float = 120.0) -> dict:
    led = load_ledger(out_dir, since_ts)
    if not led:
        return {"error": "no poll ledger found (collector not running / ledger empty)"}

    t0, t1 = led[0]["poll_ts"], led[-1]["poll_ts"]
    span = max(t1 - t0, 1)
    actual = len(led)

    # global downtime: gaps between consecutive polls > threshold
    downtime: List[tuple] = []
    intervals: List[int] = []
    for a, b in zip(led, led[1:]):
        gap = b["poll_ts"] - a["poll_ts"]
        intervals.append(gap)
        if gap > gap_threshold:
            downtime.append((a["poll_ts"], b["poll_ts"]))
    downtime_secs = sum(e - s for s, e in downtime)
    # effective cadence = MEAN inter-poll interval = real sustained throughput for this universe size.
    # (Median misleads: the distribution is bimodal — most polls are fast, but trades/fee cycles inject
    # slower ones, so median≈poll_secs while mean is ~2x.) "expected" is based on this, NOT the configured
    # poll_secs, so we don't cry wolf about an unmeetable target. Drop-off vs earlier = validator's job.
    cadence = round(span / (len(intervals))) if intervals else poll_secs
    expected = span / max(cadence, 1)

    # per-market failure tally (degraded but not full downtime)
    fail_count: Dict[str, int] = {}
    fail_windows: Dict[str, List[tuple]] = {}
    prev_ts = None
    for e in led:
        for m in e.get("failed_markets", []):
            fail_count[m] = fail_count.get(m, 0) + 1
            fail_windows.setdefault(m, []).append((e["poll_ts"], e["poll_ts"] + int(poll_secs)))
        prev_ts = e["poll_ts"]

    uni = _universe(out_dir)
    cond_label = {m["market_id"]: (m.get("question") or m.get("slug") or m["market_id"])
                  for m in uni.values()}

    report = {
        "from_ts": t0, "to_ts": t1, "span_min": round(span / 60, 1),
        "polls_actual": actual, "polls_expected": round(expected),
        "effective_cadence_s": cadence,
        "uptime_pct": round(100 * max(0, span - downtime_secs) / span, 2),
        "downtime_windows": [
            {"start": s, "end": e, "mins": round((e - s) / 60, 1),
             "utc": f"{_fmt(s)}–{_fmt(e)}"} for s, e in downtime],
        "downtime_total_min": round(downtime_secs / 60, 1),
        "markets_with_failures": sorted(
            ({"market": cond_label.get(m, m)[:60], "cond": m, "failed_polls": c}
             for m, c in fail_count.items()), key=lambda x: -x["failed_polls"])[:20],
    }

    if market:
        cond = resolve_market(out_dir, market) or market
        wins = _merge_windows(
            [(e["poll_ts"], e["poll_ts"] + int(poll_secs)) for e in led
             if cond in e.get("failed_markets", [])])
        report["market_detail"] = {
            "cond": cond, "label": cond_label.get(cond, cond),
            "failed_polls": fail_count.get(cond, 0),
            # both collector downtime AND this market's own failures are gaps for this market
            "gap_windows": [{"utc": f"{_fmt(s)}–{_fmt(e)}", "mins": round((e - s) / 60, 1)}
                            for s, e in _merge_windows(downtime + wins)],
        }
    return report


def _fmt(ts: int) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(ts)) + "Z"


def _parse_since(s: str) -> int:
    if not s:
        return 0
    now = int(time.time())
    if s.endswith("m"):
        return now - int(float(s[:-1]) * 60)
    if s.endswith("h"):
        return now - int(float(s[:-1]) * 3600)
    if s.endswith("d"):
        return now - int(float(s[:-1]) * 86400)
    return int(s)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out_dir", default="desk/data/capture/")
    ap.add_argument("--since", default="24h")
    ap.add_argument("--market", default=None)
    ap.add_argument("--poll-secs", type=float, default=10.0)
    ap.add_argument("--gap-threshold", type=float, default=120.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rep = coverage(a.out_dir, _parse_since(a.since), a.market, a.poll_secs, a.gap_threshold)
    if a.json:
        print(json.dumps(rep, indent=2))
    elif "error" in rep:
        print(rep["error"])
    else:
        print(f"Coverage {_fmt(rep['from_ts'])} → {_fmt(rep['to_ts'])} "
              f"({rep['span_min']}m): uptime {rep['uptime_pct']}%  "
              f"polls {rep['polls_actual']}/{rep['polls_expected']}")
        if rep["downtime_windows"]:
            print(f"DOWNTIME ({rep['downtime_total_min']}m total):")
            for w in rep["downtime_windows"]:
                print(f"  {w['utc']}  ({w['mins']}m) — no data for ANY market")
        else:
            print("No collector downtime windows.")
        if rep["markets_with_failures"]:
            print("Markets with per-market poll failures (degraded, not full downtime):")
            for m in rep["markets_with_failures"]:
                print(f"  {m['failed_polls']:>4} polls  {m['market']}")
        if rep.get("market_detail"):
            d = rep["market_detail"]
            print(f"\n{d['label'][:70]}\n  failed polls: {d['failed_polls']}; gap windows:")
            for w in d["gap_windows"]:
                print(f"    {w['utc']}  ({w['mins']}m)")
