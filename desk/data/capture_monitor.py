"""Hourly LLM-judge monitor (runs every 3600s under the process supervisor).

Complements the deterministic `capture_validate.py`: builds a compact telemetry summary
(validator status + poll-ledger coverage + sampled per-market book stats) and asks Claude
to reason about whether collection looks healthy — catching subtle anomalies rules miss
(frozen/flatlined markets, implausible spreads, volume collapse). Cheap by design: summarize
deterministically first, then one Haiku call over the summary.

Degrades gracefully: if `anthropic` isn't installed or `ANTHROPIC_API_KEY` is unset, writes the
deterministic summary with a rule-based verdict and notes the LLM was skipped.

Run:  python3 -m desk.data.capture_monitor [--out_dir DIR] [--model claude-haiku-4-5]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import time
from collections import defaultdict
from typing import Dict, List, Optional

import pyarrow.parquet as pq

from desk.data.capture_coverage import coverage

MODEL = "claude-haiku-4-5"   # cheap; hourly automated check. Override with --model.
SYSTEM = (
    "You are a data-quality monitor for a Polymarket order-book collector that records L2 depth + "
    "trades to Parquet for backtesting. Given an hourly telemetry summary, judge whether collection is "
    "healthy. Watch for: collector downtime/gaps; a drop in poll or snapshot rate vs earlier; markets "
    "whose mid is frozen/flatlined for the whole hour despite being normally active; implausible spreads "
    "(very wide on a liquid market); zero trades where trading is expected; stale markets (no recent "
    "snapshot); memory/disk pressure. A quiet/illiquid market that simply didn't change is NORMAL, not an "
    "anomaly — only flag a flatline if rate/coverage or context suggests a real fault. The window may be "
    "shorter than an hour (see coverage_last_hr.span_min) right after a restart — if so, low absolute "
    "snapshot counts and short staleness are expected; judge relative to the actual span, not a full hour. "
    "A null mid/spread means a one-sided book (only bids or only asks), normal for some markets. Use "
    "effective_cadence_s (the collector's real sustained cadence for this universe size) as the baseline, "
    "not a fixed target. Respond with ONLY "
    'JSON: {"verdict":"OK|WARN|ALERT","flags":["short issue", ...],"summary":"one-line assessment"}.'
)


def _today_dirs(out_dir: str) -> List[str]:
    return [time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400)),
            time.strftime("%Y-%m-%d", time.gmtime())]


def _sample_market_stats(out_dir: str, cond: str, since_ts: int) -> Optional[dict]:
    files: List[str] = []
    for d in _today_dirs(out_dir):
        files += glob.glob(os.path.join(out_dir, d, cond, "book_snapshots", "*.parquet"))
    if not files:
        return None
    mids, spreads, last_ts = [], [], 0
    snaps = 0
    for f in files:
        try:
            t = pq.read_table(f, columns=["capture_ts", "mid", "spread", "best_bid", "best_ask"])
        except Exception:  # noqa: BLE001
            continue
        for r in t.to_pylist():
            if r["capture_ts"] < since_ts:
                continue
            snaps += 1
            last_ts = max(last_ts, r["capture_ts"])
            if r["best_bid"] and r["best_ask"]:
                mids.append(r["mid"])
                spreads.append(r["spread"])
    if not snaps:
        return None
    return {
        "snaps_last_hr": snaps,
        "stale_s": int(time.time()) - last_ts,
        "mid_now": round(mids[-1], 4) if mids else None,
        "mid_range": [round(min(mids), 4), round(max(mids), 4)] if mids else None,
        "mid_moved": (len(set(round(m, 4) for m in mids)) > 1) if mids else None,
        "spread_now": round(spreads[-1], 4) if spreads else None,
    }


def build_summary(out_dir: str) -> dict:
    now = int(time.time())
    hr_ago = now - 3600
    status = {}
    sp = os.path.join(out_dir, "_status.json")
    if os.path.exists(sp):
        try:
            status = json.load(open(sp))
        except (ValueError, OSError):
            status = {}
    cov = coverage(out_dir, since_ts=hr_ago, poll_secs=10.0)

    # one representative (highest-volume) market per lane
    uni_path = os.path.join(out_dir, "_universe.json")
    uni = json.load(open(uni_path)).get("tokens", {}) if os.path.exists(uni_path) else {}
    by_lane: Dict[str, dict] = {}
    for m in uni.values():
        lane = m.get("tag", "?")
        if lane not in by_lane or m["volume24hr"] > by_lane[lane]["volume24hr"]:
            by_lane[lane] = m
    samples = {}
    for lane, m in by_lane.items():
        s = _sample_market_stats(out_dir, m["market_id"], hr_ago)
        if s:
            samples[lane] = {"market": (m.get("question") or m.get("slug") or "")[:60], **s}

    checks = status.get("checks", {})
    return {
        "ts": now,
        "validator": {
            "ok": status.get("ok"),
            "failed_checks": [k for k, v in checks.items() if not v.get("ok")],
            "poll_rate_min": status.get("poll_rate_min"),
            "snap_rate_min": status.get("snap_rate_min"),
            "rss_mb": status.get("rss_mb"), "free_gb": status.get("free_gb"),
            "tokens_seen": status.get("tokens_seen"), "universe_tokens": status.get("universe_tokens"),
        },
        "coverage_last_hr": {k: cov.get(k) for k in
                             ("span_min", "uptime_pct", "polls_actual", "polls_expected",
                              "effective_cadence_s", "downtime_windows", "markets_with_failures")}
        if "error" not in cov else cov,
        "market_samples_last_hr": samples,
    }


def _rule_verdict(summary: dict) -> dict:
    v = summary["validator"]
    cov = summary.get("coverage_last_hr", {})
    flags = []
    if v.get("ok") is False:
        flags.append(f"validator hard-fail: {v.get('failed_checks')}")
    if isinstance(cov, dict) and (cov.get("uptime_pct") or 100) < 95:
        flags.append(f"uptime {cov.get('uptime_pct')}% last hr")
    return {"verdict": "ALERT" if flags else "OK", "flags": flags,
            "summary": "rule-based (LLM skipped)"}


def _ensure_key() -> bool:
    """A service manager's environment doesn't inherit the interactive shell env, so fall back to a
    gitignored key file (`~/.anthropic_key`, or ANTHROPIC_API_KEY=... in project `.env`)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    from desk.runtime.paths import repo_path
    for p in (os.path.expanduser("~/.anthropic_key"), repo_path(".env")):   # repo .env, never cwd-relative
        try:
            if not os.path.exists(p):
                continue
            for line in open(p):
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY"):
                    # strip inline `# comment` and quotes — keys are [A-Za-z0-9_-], never contain '#'
                    val = line.split("=", 1)[1].split("#", 1)[0].strip().strip('"\'')
                    if val:
                        os.environ["ANTHROPIC_API_KEY"] = val
                        return True
                if line and not line.startswith("#") and "=" not in line and p.endswith(".anthropic_key"):
                    os.environ["ANTHROPIC_API_KEY"] = line.split("#", 1)[0].strip()
                    return True
        except OSError:
            continue
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def judge(summary: dict, model: str) -> dict:
    if not _ensure_key():
        return {**_rule_verdict(summary), "llm": "skipped: no ANTHROPIC_API_KEY (set ~/.anthropic_key)"}
    try:
        import anthropic
    except ImportError:
        return {**_rule_verdict(summary), "llm": "skipped: anthropic not installed"}
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model, max_tokens=600, system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(summary, indent=2)}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        verdict = json.loads(text)
        verdict["llm"] = model
        return verdict
    except Exception as e:  # noqa: BLE001 — never let monitoring crash; fall back to rules
        return {**_rule_verdict(summary), "llm": f"error: {e}"}


def run(out_dir: str, model: str = MODEL) -> dict:
    summary = build_summary(out_dir)
    verdict = judge(summary, model)
    out = {"ts": summary["ts"], **verdict, "summary_payload": summary}
    with open(os.path.join(out_dir, "_monitor.json"), "w") as f:
        json.dump(out, f, indent=2)
    line = (f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(summary['ts']))}Z "
            f"{verdict.get('verdict')} [{verdict.get('llm')}] {verdict.get('summary')} "
            f"{('flags=' + '; '.join(verdict.get('flags', []))) if verdict.get('flags') else ''}")
    with open(os.path.join(out_dir, "capture_monitor.log"), "a") as f:
        f.write(line + "\n")
    print(line)
    return out


if __name__ == "__main__":
    from desk.runtime.guard import job_guard, rotate_log
    from desk.runtime.paths import data_path, repo_path, resolve_data_rel
    job_guard("capturemonitor", deadline_s=900)
    for _lg in ("capturemonitor.out", "capturemonitor.err"):
        rotate_log(data_path("data", "capture", _lg))
    import sys
    from desk.types import CaptureConfig
    ap = argparse.ArgumentParser()
    ap.add_argument("config", nargs="?", default=repo_path("desk", "capture_config.json"),
                    help="path to capture_config.json (out_dir is read from it)")
    ap.add_argument("--model", default=MODEL)
    a = ap.parse_args()
    try:
        out_dir = resolve_data_rel(CaptureConfig.from_json(json.load(open(a.config))).out_dir)
    except (OSError, ValueError):
        out_dir = data_path("data", "capture")
    run(out_dir, a.model)
