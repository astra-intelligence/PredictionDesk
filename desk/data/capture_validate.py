"""Interim validator (the capturevalidate loop runs it every ~30 min). Asserts captured data meets
the T5 backtest needs, writes _status.json + appends a block to capture_health.log.

OBSERVE-ONLY (architecture review B19): validate() never mutates the store. Compaction lives in
`desk.data.compactor`, which the same loop calls as a separate entry point; this module keeps a
thin `compact()` wrapper delegating there (stable import for the eval + any older callers).

Also per B19, no full-dataset read: the scan is bounded to the ACTIVE window (last RECENT_DAYS
dated dirs), file recency comes from the flush epoch in part FILENAMES (no per-file stat storm),
and full row reads are capped to the newest PER_LEAF_READ part files per market leaf — exactly the
files that carry freshness/coverage truth. Growth/rate checks use the capture heartbeat's own
cumulative counters instead of re-counting every row on disk.

Exit code: 0 if ok (no HARD failures), 1 otherwise — so the loop/process supervisor surfaces problems.

Run:  python3 -m desk.data.capture_validate [config.json]
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import pyarrow.parquet as pq

from desk.data.compactor import DATE_RE, HOUR_RE, PART_EPOCH_RE, STATE_BASENAME
from desk.types import CaptureConfig

HEARTBEAT_MAX_AGE = 120        # s — liveness (>2-min alert)
FRESH_WINDOW = 1800            # s — "recent rows" window for coverage
SAMPLE_FILES = 40              # cap files scanned for ladder well-formedness
RECENT_DAYS = 2                # dated dirs the validator looks at (B19: never the whole dataset)
PER_LEAF_READ = 2              # newest part files fully read per market leaf (freshness/coverage)
COMPACTOR_FRESH_S = 2 * 3600   # soft alarm when the compactor hasn't completed a pass in this long


def _proc_mem(pid: int):
    """Return (rss_mb, cpu_percent) for pid via ps; (0,0) if unavailable."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-o", "rss=,%cpu=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.split()
        return (float(out[0]) / 1024.0, float(out[1])) if len(out) >= 2 else (0.0, 0.0)
    except Exception:  # noqa: BLE001
        return (0.0, 0.0)


def compact(out_dir: str):
    """Compat wrapper: full-sweep compaction via desk.data.compactor (all dates, unbounded).
    Kept so existing callers/tests reach the SAME merge path the scheduled loop runs; the loop
    itself calls `python3 -m desk.data.compactor` (bounded pass) — never through validate()."""
    from desk.data.compactor import compact_pass
    st = compact_pass(out_dir, max_dirs=10 ** 9, all_dates=True)
    return st["groups"], st["parts_removed"]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def _file_epoch(name: str) -> Optional[float]:
    """Flush epoch from the filename alone: part-<pid>-<epoch>-<idx> = capture's own write stamp;
    hour-<YYYYMMDDHH> compactions = end of that hour (old rows by construction). None = unknown."""
    m = PART_EPOCH_RE.match(name)
    if m:
        return float(m.group(1))
    m = HOUR_RE.match(name)
    if m:
        import calendar
        try:
            return calendar.timegm(time.strptime(m.group(1), "%Y%m%d%H")) + 3600.0
        except ValueError:
            return None
    return None


def _window_leafs(out: str, stream: str, days: int = RECENT_DAYS) -> List[List[Tuple[float, str]]]:
    """Per market leaf in the active window: [(epoch, path), ...] newest-first — from dirent names
    only, no per-file stats."""
    now = time.time()
    floor = time.strftime("%Y-%m-%d", time.gmtime(now - (days - 1) * 86400))
    leafs: List[List[Tuple[float, str]]] = []
    try:
        dates = sorted(d for d in os.listdir(out) if DATE_RE.match(d) and d >= floor)
    except OSError:
        return leafs
    for dd in dates:
        dp = os.path.join(out, dd)
        try:
            mkts = [e.path for e in os.scandir(dp) if e.is_dir()]
        except OSError:
            continue
        for mp in mkts:
            leaf = os.path.join(mp, stream)
            try:
                names = os.listdir(leaf)
            except OSError:
                continue
            files = [( _file_epoch(n) or 0.0, os.path.join(leaf, n))
                     for n in names if n.endswith(".parquet")]
            if files:
                files.sort(key=lambda t: t[0], reverse=True)
                leafs.append(files)
    return leafs


def validate(cfg: CaptureConfig) -> Dict:
    out = cfg.out_dir
    now = int(time.time())
    checks: Dict[str, dict] = {}
    hard_fail = False

    def add(name, ok, detail, hard=False):
        nonlocal hard_fail
        checks[name] = {"ok": bool(ok), "detail": detail}
        if hard and not ok:
            hard_fail = True

    # 1. liveness
    hb_path = os.path.join(out, "_heartbeat")
    hb = {}
    if os.path.exists(hb_path):
        try:
            hb = json.load(open(hb_path))
        except (ValueError, OSError):
            hb = {}
    age = now - int(hb.get("ts", 0)) if hb else 10**9
    pid_ok = _pid_alive(int(hb["pid"])) if hb.get("pid") else False
    add("liveness", age <= HEARTBEAT_MAX_AGE and pid_ok,
        f"heartbeat_age={age}s pid_alive={pid_ok} polls={hb.get('polls')}", hard=True)

    # universe for coverage
    uni = {}
    uni_path = os.path.join(out, "_universe.json")
    if os.path.exists(uni_path):
        try:
            uni = json.load(open(uni_path)).get("tokens", {})
        except (ValueError, OSError):
            uni = {}

    book_leafs = _window_leafs(out, "book_snapshots")
    trade_leafs = _window_leafs(out, "trades")
    n_book_files = sum(len(l) for l in book_leafs)
    n_trade_files = sum(len(l) for l in trade_leafs)
    add("has_book_files", n_book_files > 0,
        f"{n_book_files} book parquet files in last {RECENT_DAYS}d", hard=True)

    # 6. readable + collect stats: the newest PER_LEAF_READ files per leaf carry the freshness /
    # coverage truth (ladder checks sample the newest SAMPLE_FILES of those)
    read_paths = [p for leaf in book_leafs for _, p in leaf[:PER_LEAF_READ]]
    read_paths.sort(key=lambda p: os.path.basename(p), reverse=True)   # newest-ish first for sampling
    rows_read = 0
    bytes_read = 0
    tokens_seen: set = set()
    max_capture_ts = 0
    unreadable: List[str] = []
    bad_ts = 0
    ladder_ok = True
    ladder_detail = "ok"
    per_token_last: Dict[str, int] = {}
    for i, f in enumerate(read_paths):
        try:
            t = pq.read_table(f)
            bytes_read += os.path.getsize(f)
        except Exception as e:  # noqa: BLE001
            unreadable.append(f"{os.path.basename(f)}:{e}")
            continue
        rows_read += t.num_rows
        d = t.to_pylist()
        for r in d:
            tokens_seen.add(r["token_id"])
            cts = r["capture_ts"]
            max_capture_ts = max(max_capture_ts, cts)
            # sanity (order-independent — store sorts by capture_ts at read, so cross-file
            # ordering is not an invariant): flag timestamps that are corrupt or in the future
            if not (1_700_000_000 <= cts <= now + 60):
                bad_ts += 1
            per_token_last[r["token_id"]] = max(per_token_last.get(r["token_id"], 0), cts)
        if i < SAMPLE_FILES and ladder_ok:
            for r in d:
                bids, asks = r["bids"], r["asks"]
                if any(not (0 < p < 1) or s <= 0 for p, s in bids + asks):
                    ladder_ok, ladder_detail = False, f"bad price/size in {os.path.basename(f)}"
                    break
                if bids and any(bids[j][0] < bids[j + 1][0] for j in range(len(bids) - 1)):
                    ladder_ok, ladder_detail = False, f"bids not desc in {os.path.basename(f)}"
                    break
                if asks and any(asks[j][0] > asks[j + 1][0] for j in range(len(asks) - 1)):
                    ladder_ok, ladder_detail = False, f"asks not asc in {os.path.basename(f)}"
                    break
                if bids and asks and not (r["best_bid"] < r["best_ask"]):
                    ladder_ok, ladder_detail = False, f"best_bid>=best_ask in {os.path.basename(f)}"
                    break

    add("parquet_readable", not unreadable,
        f"unreadable={unreadable[:3]}" if unreadable else f"all {len(read_paths)} read files readable",
        hard=True)
    add("ladder_well_formed", ladder_ok, ladder_detail, hard=True)
    add("capture_ts_sane", bad_ts == 0, "all in range" if bad_ts == 0 else f"{bad_ts} ts corrupt/future",
        hard=True)

    # 2. coverage (soft): fraction of universe tokens seen in the newest per-leaf files
    fresh_tokens = {tok for tok, last in per_token_last.items() if max_capture_ts - last <= FRESH_WINDOW}
    cov = len(tokens_seen & set(uni)) / len(uni) if uni else (1.0 if tokens_seen else 0.0)
    add("coverage", cov >= 0.8, f"{len(tokens_seen)}/{len(uni)} tokens seen, {len(fresh_tokens)} fresh")

    # poll gap (soft) from heartbeat
    add("poll_gap", float(hb.get("max_gap_s", 0)) <= HEARTBEAT_MAX_AGE,
        f"max_gap_s={hb.get('max_gap_s')}")

    # 5. trades present (soft — some markets may have no recent prints)
    add("trades_present", n_trade_files > 0, f"{n_trade_files} trade files in last {RECENT_DAYS}d")

    # 7+8. growth + RATE / drop-off vs previous status — from the heartbeat's CUMULATIVE counters
    # (per-process), not a full-dataset row recount (B19)
    prev = {}
    status_path = os.path.join(out, "_status.json")
    if os.path.exists(status_path):
        try:
            prev = json.load(open(status_path))
        except (ValueError, OSError):
            prev = {}
    hb_rows = int(hb.get("book_rows", 0))
    cur_polls = int(hb.get("polls", 0))
    cur_hb_ts = int(hb.get("ts", now))
    cur_pid = int(hb.get("pid", 0))
    same_proc = prev.get("pid") == cur_pid and cur_pid  # restart resets baseline
    prev_rows = int(prev.get("book_rows", 0)) if same_proc else 0
    add("growth", (not same_proc) or hb_rows >= prev_rows,
        f"hb book_rows {prev_rows}->{hb_rows}" + ("" if same_proc else " (new process — baseline)"))

    dt_min = (cur_hb_ts - int(prev.get("heartbeat_ts", 0))) / 60.0 if (prev.get("heartbeat_ts") and same_proc) else 0.0
    poll_rate = book_rate = None
    if dt_min > 0.5:
        poll_rate = round((cur_polls - int(prev.get("polls", 0))) / dt_min, 2)
        book_rate = round((hb_rows - prev_rows) / dt_min, 1)
        # drop-off: process alive but polls not advancing over a real interval = stalled loop
        add("poll_rate", not (pid_ok and dt_min >= 5 and poll_rate <= 0),
            f"{poll_rate} polls/min, {book_rate} snaps/min over {dt_min:.0f}m", hard=True)
    else:
        add("poll_rate", True, "first run / short interval — baseline")

    # process memory + cpu (leak / runaway guard)
    rss_mb, cpu = _proc_mem(int(hb["pid"])) if hb.get("pid") else (0.0, 0.0)
    add("memory", rss_mb <= 3000, f"rss={rss_mb:.0f}MB cpu={cpu}%", hard=(rss_mb > 3000))

    # free disk (hard if dangerously low — fills up = silent data loss)
    import shutil
    free_gb = shutil.disk_usage(out).free / 1e9
    add("disk_free", free_gb >= 5, f"{free_gb:.1f} GB free", hard=(free_gb < 5))

    # compaction health (soft, READ-ONLY — the compactor loop owns all mutation; B19)
    comp = {}
    try:
        comp = (json.load(open(os.path.join(out, STATE_BASENAME))) or {}).get("last_pass", {}) or {}
    except Exception:  # noqa: BLE001
        comp = {}
    cg, cr = int(comp.get("groups", 0)), int(comp.get("parts_removed", 0))
    comp_age = (now - int(comp["ts"])) if comp.get("ts") else None
    add("compactor_fresh", comp_age is None or comp_age <= COMPACTOR_FRESH_S,
        f"last pass {comp_age}s ago: {cg} groups / {cr} parts" if comp_age is not None
        else "no compactor state yet (loop not active on this dir)")

    status = {
        "ts": now, "ok": not hard_fail, "checks": checks,
        "book_rows": hb_rows, "window_rows_read": rows_read, "window_files": n_book_files,
        "bytes_read": bytes_read, "polls": cur_polls, "heartbeat_ts": cur_hb_ts,
        "pid": cur_pid,
        "poll_rate_min": poll_rate, "snap_rate_min": book_rate, "rss_mb": round(rss_mb),
        "free_gb": round(free_gb, 1), "compacted_groups": cg, "parts_removed": cr,
        "tokens_seen": len(tokens_seen), "universe_tokens": len(uni),
        "max_capture_ts": max_capture_ts, "heartbeat_age_s": age,
    }
    tmp = status_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp, status_path)

    # append health log block
    failed = [k for k, v in checks.items() if not v["ok"]]
    line = (f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now))}Z "
            f"{'OK ' if not hard_fail else 'FAIL'} hb_rows={hb_rows} files2d={n_book_files} "
            f"read={rows_read}r/{len(read_paths)}f tokens={len(tokens_seen)}/{len(uni)} "
            f"rate={poll_rate}poll/m,{book_rate}snap/m "
            f"rss={rss_mb:.0f}MB gap={hb.get('max_gap_s')} compact={cg}g/{cr}p "
            f"{'failed=' + ','.join(failed) if failed else ''}")
    with open(os.path.join(out, "capture_health.log"), "a") as f:
        f.write(line + "\n")
    print(line)
    return status


if __name__ == "__main__":
    from desk.runtime.paths import repo_path, resolve_data_rel
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else repo_path("desk", "capture_config.json")
    cfg = CaptureConfig.from_json(json.load(open(cfg_path)))
    cfg.out_dir = resolve_data_rel(cfg.out_dir)   # a relative config out_dir must never follow cwd
    st = validate(cfg)
    sys.exit(0 if st["ok"] else 1)
