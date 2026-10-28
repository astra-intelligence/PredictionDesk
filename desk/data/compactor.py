"""ENTRYPOINT: compact_pass(out_dir) / python3 -m desk.data.compactor [--drain].

Incremental, bounded, crash-safe compactor for the capture store. Merges completed-hour
`part-*.parquet` into one `hour-<YYYYMMDDHH>.parquet` per <date>/<market>/<stream> leaf dir —
the same lossless merge semantics as the old capture_validate.compact() (pure concat; capture
dedupes rows upstream by book hash; store.universe() recounts per file, proven by the eval's
compaction_recount guard) — but built to run against the LIVE stack with a ~2M-file backlog:

  bounded      at most `max_dirs` leaf dirs get compacted per pass, oldest date first, so a pass
               can never stall its host loop regardless of backlog. Default sized for steady
               state: ~65k part files/h land in ~2.4k active leaf dirs/h; at the loop's 10-min
               cadence 800 dirs/pass = 2x headroom.
  stat-free    candidate discovery parses the flush epoch out of part FILENAMES
               (part-<pid>-<epoch>-<idx>.parquet — same trick as the /ops collect_capture scan)
               via scandir/listdir names only; per-file stats happen only as a fallback inside
               leafs actually being compacted (legacy/synthetic names without an epoch).
  age floor    parts younger than MIN_AGE_S (10 min) are NEVER touched, on top of the old
               completed-hour-only rule. Capture part files are write-once (atomic rename, never
               reopened); readers glob per read and tolerate a vanished path, so older parts are
               safe to merge out from under the running stack.
  atomic       per leaf: stage every (hour) group — write hour tmp + fsync, verify its parquet row
               count equals sum(parts) + any prior hour file — then commit ONE journal manifest
               for the leaf (fsync file + dir), rename every tmp over its hour file (fsync dir),
               delete the folded parts (fsync dir), delete the manifest. The verify happens BEFORE
               anything is deleted; fsyncs batch per leaf, ordering (data -> journal -> rename ->
               delete) is what makes every crash point safe.
  crash-safe   every step reruns cleanly. Recovery per leaf before compacting it:
                 - manifest whose target hour file carries EXACTLY the journaled row count
                   => the rename happened; the listed parts are already folded in (duplicates)
                   => delete them + manifest. Row-count equality is unambiguous: the journaled
                   expected is strictly greater than any prior hour file's count (parts are
                   never empty), so an old target can't masquerade as the merged one.
                 - any other manifest/tmp leftover => the rename did NOT happen; delete the
                   leftovers, keep the parts — the merge simply reruns. No row is ever lost.
  serialized   one fcntl lock per out_dir (_compactor.lock): a drain and the scheduled loop
               never interleave — the loser skips its pass.

Scheduling: the capture_validate loop (ops/capture_validate_loop.sh) calls this module as its
own entry point; validate() itself never mutates (architecture review B19 split).

Drain mode (backlog): `python3 -m desk.data.compactor --drain` sweeps ALL dated dirs oldest-
first under a files/sec budget, prints progress, pauses inside the live fire window
(12:25-13:00 UTC) or while /api/health reports fire_in_progress, and aborts if capture's
heartbeat degrades (age or max_gap_s). Safe to interrupt: Ctrl-C finishes the current group.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from typing import Dict, Iterator, List, Optional, Tuple

import pyarrow as pa
import pyarrow.parquet as pq

PART_EPOCH_RE = re.compile(r"^part-\d+-(\d{9,12})-\d+\.parquet$")   # capture's part-<pid>-<epoch>-<idx>
PART_RE = re.compile(r"^part-.+\.parquet$")
HOUR_RE = re.compile(r"^hour-(\d{10})\.parquet$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MANIFEST_NAME = "compact.manifest.json"        # ONE journal per leaf dir (covers all its hour groups)
LEGACY_MANIFEST_RE = re.compile(r"^hour-(\d{10})\.manifest\.json$")  # pre-leaf-manifest shape
STREAMS = ("book_snapshots", "trades")

MIN_AGE_S = 600                 # 10-min floor: a part younger than this is never touched
DEFAULT_MAX_DIRS = 800          # leaf dirs compacted per scheduled pass (see module docstring)
DEFAULT_OLD_DATES = 8           # settled old dates re-swept per pass (round-robin) — catches stray
                                # old-date writes (trades re-fetched after a capture restart)
STATE_BASENAME = "_compactor_state.json"
LOCK_BASENAME = "_compactor.lock"

FIRE_WINDOW_UTC = ("12:25", "13:00")          # never drain inside this window (live fire 12:30-12:50)
HB_MAX_AGE_S = 180.0                           # drain aborts when capture heartbeat is this stale
HB_MAX_GAP_S = 60.0                            # ... or reports a poll gap this large (baseline ~11s)


def _new_stats() -> Dict:
    return {"dirs_processed": 0, "groups": 0, "parts_removed": 0, "rows_merged": 0,
            "recovered_manifests": 0, "recovered_parts": 0, "tmp_cleaned": 0,
            "skipped_groups": 0, "verify_failed": 0, "locked": False, "ts": int(time.time())}


def _fsync_path(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _num_rows(path: str) -> int:
    return pq.ParquetFile(path).metadata.num_rows


def _part_epoch(name: str, path: str, now: float) -> Optional[float]:
    """Flush epoch of a part file WITHOUT a stat when the name carries it (live capture files all
    do). Fallback: mtime — only for legacy/synthetic names, and only inside a leaf being compacted.
    A clock-junk future epoch falls back to mtime too (never let junk look young forever)."""
    m = PART_EPOCH_RE.match(name)
    if m:
        ts = float(m.group(1))
        if ts <= now + 300:
            return ts
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _write_manifest(path: str, payload: dict, fsync: bool) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
        if fsync:
            f.flush()
            os.fsync(f.fileno())
    os.replace(tmp, path)


def _manifest_entries(d: str, names: List[str]) -> List[Tuple[str, dict]]:
    """(manifest_path, group_entry) pairs found in leaf `d`: the leaf manifest's groups plus any
    legacy per-group manifests. An unreadable/partial manifest yields one empty entry (pre-commit
    crash: its tmp gets discarded, parts stay)."""
    out: List[Tuple[str, dict]] = []
    for n in names:
        if n != MANIFEST_NAME and not LEGACY_MANIFEST_RE.match(n):
            continue
        man_path = os.path.join(d, n)
        try:
            man = json.load(open(man_path))
            groups = man.get("groups") if isinstance(man.get("groups"), list) else [man]
        except Exception:  # noqa: BLE001 — unreadable/partial manifest = pre-commit crash
            groups = [{}]
        for g in groups:
            out.append((man_path, g if isinstance(g, dict) else {}))
    return out


def _recover_leaf(d: str, names: List[str], stats: Dict, min_age_s: float, now: float,
                  fsync: bool = True) -> List[str]:
    """Detect + clean crash leftovers in leaf `d` BEFORE merging anything there. Returns the
    refreshed name list. Per journaled group: a target hour file carrying EXACTLY the journaled
    row count proves its rename happened — the listed parts are duplicates and are deleted; any
    other leftover (tmp, manifest without a matching target) is discarded with the parts kept, so
    the merge simply reruns. An orphan *.parquet.tmp older than the age floor (no manifest claims
    it) is an incomplete write whose rows still live in the parts — deleted. Young tmps are left
    alone (could be capture's own in-flight atomic rename)."""
    nameset = set(names)
    changed = False
    man_paths = set()
    for man_path, g in _manifest_entries(d, names):
        man_paths.add(man_path)
        target_name = str(g.get("target", ""))
        try:
            expected = int(g.get("expected_rows", -1))
        except (TypeError, ValueError):
            expected = -1
        parts = [str(p) for p in g.get("parts", [])]
        target = os.path.join(d, target_name) if target_name else ""
        done = False
        if target and expected > 0 and os.path.exists(target):
            try:
                done = _num_rows(target) == expected
            except Exception:  # noqa: BLE001 — unreadable target: treat as not-merged
                done = False
        if done:
            # rename happened: the listed parts are already inside the hour file — duplicates.
            for p in parts:
                if p in nameset or os.path.exists(os.path.join(d, p)):
                    try:
                        os.remove(os.path.join(d, p))
                        stats["recovered_parts"] += 1
                        nameset.discard(p)
                        changed = True
                    except OSError:
                        pass
        # either way the group's staged tmp (if any) is garbage now
        tmp = (target + ".tmp") if target else ""
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
                nameset.discard(os.path.basename(tmp))
                stats["tmp_cleaned"] += 1
                changed = True
            except OSError:
                pass
    for man_path in man_paths:
        try:
            os.remove(man_path)
            nameset.discard(os.path.basename(man_path))
            stats["recovered_manifests"] += 1
            changed = True
        except OSError:
            pass
    # orphan tmps (no manifest): incomplete writes; rows are still in the parts. Age-gated so we
    # never race an in-flight atomic rename (capture's part tmps live for milliseconds).
    for n in list(nameset):
        if not n.endswith(".parquet.tmp"):
            continue
        p = os.path.join(d, n)
        try:
            if now - os.path.getmtime(p) >= min_age_s:
                os.remove(p)
                nameset.discard(n)
                stats["tmp_cleaned"] += 1
                changed = True
        except OSError:
            pass
    if changed and fsync:
        _fsync_path(d)
    return [n for n in names if n in nameset]


def _stage_group(d: str, h: str, part_names: List[str], stats: Dict, fsync: bool = True) -> Optional[dict]:
    """Stage one hour group: write the merged hour tmp (+fsync) and VERIFY its row count equals the
    parts (+ any prior hour file). Nothing is deleted here. Returns the group's journal entry."""
    hour_name = f"hour-{h}.parquet"
    hour_path = os.path.join(d, hour_name)
    tmp = hour_path + ".tmp"
    try:
        tables = [pq.read_table(os.path.join(d, n)) for n in part_names]
        if os.path.exists(hour_path):
            tables.append(pq.read_table(hour_path))          # fold in prior compaction for this hour
        expected = sum(t.num_rows for t in tables)
        if expected <= 0:
            stats["skipped_groups"] += 1
            return None
        pq.write_table(pa.concat_tables(tables), tmp, compression="zstd")
        if fsync:
            _fsync_path(tmp)                                  # data durable BEFORE any rename
        if _num_rows(tmp) != expected:                        # row-conservation gate (pre-delete)
            os.remove(tmp)
            stats["verify_failed"] += 1
            return None
        return {"target": hour_name, "expected_rows": expected, "parts": list(part_names)}
    except Exception:  # noqa: BLE001 — skip a bad group (schema drift, unreadable part); retried next pass
        stats["skipped_groups"] += 1
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return None


def compact_leaf(d: str, min_age_s: float = MIN_AGE_S, now: Optional[float] = None,
                 fsync: bool = True, stats: Optional[Dict] = None) -> Dict:
    """Recover + compact ONE leaf dir (<date>/<market>/<stream>). Only parts in a COMPLETED UTC
    hour and older than the age floor are eligible; a group merges only when it shrinks the file
    count (>=2 files, or >=1 part folding into an existing hour file).

    Commit protocol (fsyncs batched per LEAF, ordering preserved):
      stage every group (tmp write + fsync + row-count verify) -> ONE leaf manifest journaling all
      staged groups (fsync file + dir: durable BEFORE any rename) -> rename every tmp over its hour
      file -> fsync dir -> delete every folded part -> fsync dir -> delete the manifest.
    A crash anywhere reruns via _recover_leaf with no row lost or duplicated."""
    stats = _new_stats() if stats is None else stats
    now = time.time() if now is None else now
    cur_hour = time.strftime("%Y%m%d%H", time.gmtime(now))
    try:
        names = os.listdir(d)
    except OSError:
        return stats
    if any(n == MANIFEST_NAME or LEGACY_MANIFEST_RE.match(n) or n.endswith(".parquet.tmp")
           for n in names):
        names = _recover_leaf(d, names, stats, min_age_s, now, fsync)
    have_hour = {m.group(1) for n in names if (m := HOUR_RE.match(n))}
    by_hour: Dict[str, List[str]] = {}
    for n in names:
        if not PART_RE.match(n):
            continue
        ep = _part_epoch(n, os.path.join(d, n), now)
        if ep is None or (now - ep) < min_age_s:
            continue                                          # 10-min floor
        h = time.strftime("%Y%m%d%H", time.gmtime(ep))
        if h >= cur_hour:
            continue                                          # completed hours only
        by_hour.setdefault(h, []).append(n)
    staged: List[dict] = []
    for h in sorted(by_hour):
        ps = by_hour[h]
        if len(ps) < 2 and h not in have_hour:
            continue                                          # merging 1 part alone shrinks nothing
        g = _stage_group(d, h, sorted(ps), stats, fsync=fsync)
        if g is not None:
            staged.append(g)
    if not staged:
        return stats
    man = os.path.join(d, MANIFEST_NAME)
    try:
        # journal BEFORE any rename: the durable manifest is what makes a rerun dedupe-safe
        _write_manifest(man, {"groups": staged, "ts": int(time.time())}, fsync)
        if fsync:
            _fsync_path(d)
        for g in staged:
            os.replace(os.path.join(d, g["target"] + ".tmp"), os.path.join(d, g["target"]))
        if fsync:
            _fsync_path(d)
    except Exception:  # noqa: BLE001 — leftovers are recovery-covered; retried next pass
        stats["skipped_groups"] += len(staged)
        return stats
    # renames committed — deletions are recovery-covered from here on (manifest still on disk)
    for g in staged:
        for p in g["parts"]:
            try:
                os.remove(os.path.join(d, p))
            except OSError:
                pass
        stats["groups"] += 1
        stats["parts_removed"] += len(g["parts"])
        stats["rows_merged"] += g["expected_rows"]
    if fsync:
        _fsync_path(d)
    try:
        os.remove(man)
    except OSError:
        pass
    return stats


# ── sweep planning ─────────────────────────────────────────────────────────────────────────────────
def _dated_dirs(out_dir: str) -> List[str]:
    try:
        return sorted(d for d in os.listdir(out_dir) if DATE_RE.match(d))
    except OSError:
        return []


def _leaf_dirs(out_dir: str, dates: List[str]) -> Iterator[str]:
    """Leaf dirs oldest-date-first, names from scandir dirents only (no per-file stats)."""
    for dd in dates:
        dp = os.path.join(out_dir, dd)
        try:
            mkts = sorted(e.name for e in os.scandir(dp) if e.is_dir())
        except OSError:
            continue
        for mk in mkts:
            mp = os.path.join(dp, mk)
            try:
                subs = {e.name for e in os.scandir(mp) if e.is_dir()}
            except OSError:
                continue
            for stream in STREAMS:
                if stream in subs:
                    yield os.path.join(mp, stream)


def _state_path(out_dir: str) -> str:
    return os.path.join(out_dir, STATE_BASENAME)


def _load_state(out_dir: str) -> Dict:
    try:
        return json.load(open(_state_path(out_dir)))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(out_dir: str, state: Dict) -> None:
    tmp = _state_path(out_dir) + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, _state_path(out_dir))
    except OSError:
        pass


def _acquire_lock(out_dir: str):
    """Non-blocking per-out_dir lock: the drain and the scheduled pass never interleave."""
    try:
        lk = open(os.path.join(out_dir, LOCK_BASENAME), "w")
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lk
    except BlockingIOError:
        return None
    except OSError:
        return None


def compact_pass(out_dir: str, max_dirs: int = DEFAULT_MAX_DIRS, min_age_s: float = MIN_AGE_S,
                 old_dates: int = DEFAULT_OLD_DATES, all_dates: bool = False,
                 files_per_sec: float = 0.0, now: Optional[float] = None, fsync: bool = True,
                 pause_cb=None, progress_cb=None) -> Dict:
    """ONE bounded pass. Sweeps the hot dates (today + yesterday — where capture writes) plus a
    round-robin slice of older dates (stray writes: trades re-fetched after a restart), oldest
    first; compacts at most `max_dirs` leaf dirs that actually need work. all_dates=True sweeps
    every dated dir (drain / compat). `pause_cb` (drain) is consulted between leafs and may block;
    returning False aborts the pass."""
    stats = _new_stats()
    lock = _acquire_lock(out_dir)
    if lock is None:
        stats["locked"] = True
        return stats
    t0 = time.time()
    try:
        dates = _dated_dirs(out_dir)
        if dates:
            state = _load_state(out_dir)
            if all_dates:
                sweep = dates
            else:
                hot = dates[-2:]
                old = dates[:-2]
                sel_old: List[str] = []
                if old and old_dates > 0:
                    cur = state.get("old_cursor") or ""
                    after = [d for d in old if d > cur] + [d for d in old if d <= cur]
                    sel_old = sorted(after[:old_dates])
                    state["old_cursor"] = sel_old[-1] if sel_old else cur
                sweep = sel_old + hot
            aborted = False
            for leaf in _leaf_dirs(out_dir, sweep):
                if pause_cb is not None and not pause_cb():
                    aborted = True
                    break
                before = (stats["groups"], stats["recovered_manifests"], stats["tmp_cleaned"])
                compact_leaf(leaf, min_age_s=min_age_s, now=now, fsync=fsync, stats=stats)
                if (stats["groups"], stats["recovered_manifests"], stats["tmp_cleaned"]) != before:
                    stats["dirs_processed"] += 1
                    if progress_cb is not None:
                        progress_cb(stats, leaf)
                    if stats["dirs_processed"] >= max_dirs:
                        break
                    if files_per_sec > 0:                      # files/sec budget (drain pacing)
                        ahead = stats["parts_removed"] / files_per_sec - (time.time() - t0)
                        if ahead > 0:
                            time.sleep(min(ahead, 5.0))
            stats["aborted"] = aborted
            stats["elapsed_s"] = round(time.time() - t0, 2)
            state["last_pass"] = {k: stats[k] for k in
                                  ("ts", "dirs_processed", "groups", "parts_removed", "rows_merged",
                                   "recovered_manifests", "recovered_parts", "tmp_cleaned",
                                   "skipped_groups", "verify_failed", "elapsed_s")}
            _save_state(out_dir, state)
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except OSError:
            pass
    return stats


# ── drain mode (backlog) ───────────────────────────────────────────────────────────────────────────
def _in_fire_window(now: Optional[float] = None) -> bool:
    hhmm = time.strftime("%H:%M", time.gmtime(now if now is not None else time.time()))
    return FIRE_WINDOW_UTC[0] <= hhmm < FIRE_WINDOW_UTC[1]


def _fetch_health(url: str) -> Optional[dict]:
    # timeout > the endpoint's own cold build (~9s when its 30s cache is cold on a big store)
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def _heartbeat_state(out_dir: str) -> Tuple[Optional[float], Optional[float]]:
    """(heartbeat age s, max_gap_s) from the capture daemon's _heartbeat file."""
    try:
        hb = json.load(open(os.path.join(out_dir, "_heartbeat")))
        return time.time() - float(hb.get("ts", 0)), float(hb.get("max_gap_s", 0.0))
    except Exception:  # noqa: BLE001
        return None, None


class _DrainGate:
    """pause_cb for drain: throttled to one health probe per `interval`; pauses through the fire
    window / fire_in_progress; returns False (abort) when capture's heartbeat degrades or the
    health endpoint stays dead."""
    def __init__(self, out_dir: str, health_url: str, check_hb: bool = True,
                 check_url: bool = True, interval: float = 20.0):
        self.out_dir, self.url = out_dir, health_url
        self.check_hb, self.check_url = check_hb, check_url
        self.interval, self.last = interval, 0.0
        self.url_fails = 0
        self.abort_reason = ""

    def __call__(self) -> bool:
        if time.time() - self.last < self.interval:
            return True
        self.last = time.time()
        while True:
            if self.check_hb:
                age, gap = _heartbeat_state(self.out_dir)
                if age is not None and age > HB_MAX_AGE_S:
                    self.abort_reason = f"capture heartbeat stale ({age:.0f}s > {HB_MAX_AGE_S:.0f}s)"
                    return False
                if gap is not None and gap > HB_MAX_GAP_S:
                    self.abort_reason = f"capture max_gap_s degraded ({gap:.0f}s > {HB_MAX_GAP_S:.0f}s)"
                    return False
            if _in_fire_window():
                print(f"[compactor] inside fire window {FIRE_WINDOW_UTC[0]}-{FIRE_WINDOW_UTC[1]} UTC "
                      f"— paused", flush=True)
                time.sleep(30)
                continue
            if self.check_url:
                fire = None
                h = _fetch_health(self.url)
                if h is not None:
                    self.url_fails = 0
                    fire = bool(h.get("fire_in_progress"))
                else:
                    # dashboard slow/down is NOT a reason to stop a drain — fall back to the SAME
                    # file-based signal /api/health serves (driver heartbeat age heuristic)
                    self.url_fails += 1
                    try:
                        from desk import control_plane as CP
                        fire = bool(CP.fire_in_progress())
                        if self.url_fails in (1, 10, 100):
                            print(f"[compactor] health endpoint unreachable x{self.url_fails} — using "
                                  f"direct fire_in_progress() fallback", flush=True)
                    except Exception:  # noqa: BLE001
                        fire = None
                    if fire is None:
                        if self.url_fails >= 10:
                            self.abort_reason = (f"health endpoint unreachable x{self.url_fails} and "
                                                 f"direct fallback failed ({self.url})")
                            return False
                        print(f"[compactor] health unknown ({self.url_fails}/10) — pausing 30s", flush=True)
                        time.sleep(30)
                        continue
                if fire:
                    print("[compactor] fire_in_progress — paused", flush=True)
                    time.sleep(30)
                    continue
            self.last = time.time()
            return True


def drain(out_dir: str, batch_dirs: int = 300, min_age_s: float = MIN_AGE_S,
          files_per_sec: float = 2000.0, health_url: str = "http://127.0.0.1:8788/api/health",
          max_batches: int = 0, check_health: bool = True, max_sweeps: int = 5,
          workers: int = 4) -> Dict:
    """Backlog drain: sweep ALL dated dirs oldest-first, ONE leaf enumeration per sweep (never a
    re-walk per batch), compacting leafs on a small thread pool (leaf dirs are independent; pyarrow
    releases the GIL on IO), in bounded batches of `batch_dirs` work-leafs with the health gate
    consulted between chunks (throttled) and a global files/sec budget. Re-sweeps until a whole
    sweep finds no work (hours that completed mid-drain), capped at `max_sweeps`; the scheduled
    loop owns steady state after that. Interrupt-safe: every leaf commit is atomic + journaled, so
    Ctrl-C / kill at any point loses no rows."""
    from concurrent.futures import ThreadPoolExecutor
    gate = _DrainGate(out_dir, health_url, check_hb=check_health, check_url=check_health)
    totals = _new_stats()
    totals["batches"] = 0
    t0 = time.time()
    lock = _acquire_lock(out_dir)
    if lock is None:
        print("[compactor] another compactor holds the lock — drain exits", flush=True)
        totals["abort_reason"] = "locked"
        return totals

    def _merge_stats(st: Dict) -> int:
        work = 1 if (st["groups"] or st["recovered_manifests"] or st["tmp_cleaned"]) else 0
        for k in ("groups", "parts_removed", "rows_merged", "recovered_manifests",
                  "recovered_parts", "tmp_cleaned", "skipped_groups", "verify_failed"):
            totals[k] += st[k]
        totals["dirs_processed"] += work
        return work

    def _batch_line(sweep: int, work: int) -> None:
        el = time.time() - t0
        print(f"[compactor] drain sweep {sweep} batch {totals['batches']}: +{work} dirs | total "
              f"dirs={totals['dirs_processed']} groups={totals['groups']} "
              f"parts_removed={totals['parts_removed']} rows={totals['rows_merged']} "
              f"recovered={totals['recovered_manifests']}m/{totals['recovered_parts']}p "
              f"rate={totals['parts_removed'] / max(el, 1e-9):.0f}f/s elapsed={el / 60:.1f}m", flush=True)

    def _chunks(it, n):
        buf = []
        for x in it:
            buf.append(x)
            if len(buf) >= n:
                yield buf
                buf = []
        if buf:
            yield buf

    workers = max(1, int(workers))
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sweep in range(1, max_sweeps + 1):
                sweep_work = 0
                batch_work = 0
                for chunk in _chunks(_leaf_dirs(out_dir, _dated_dirs(out_dir)), workers * 4):
                    if check_health and not gate():
                        totals["abort_reason"] = gate.abort_reason
                        print(f"[compactor] DRAIN ABORTED: {gate.abort_reason}", flush=True)
                        return totals
                    for st in ex.map(lambda leaf: compact_leaf(leaf, min_age_s=min_age_s,
                                                               stats=_new_stats()), chunk):
                        w = _merge_stats(st)
                        sweep_work += w
                        batch_work += w
                        if w and batch_work >= batch_dirs:
                            totals["batches"] += 1
                            _batch_line(sweep, batch_work)
                            batch_work = 0
                            if max_batches and totals["batches"] >= max_batches:
                                print(f"[compactor] drain stopped at --max-batches {max_batches}",
                                      flush=True)
                                return totals
                    if files_per_sec > 0:                     # global pacing budget
                        ahead = totals["parts_removed"] / files_per_sec - (time.time() - t0)
                        if ahead > 0:
                            time.sleep(min(ahead, 5.0))
                if batch_work:
                    totals["batches"] += 1
                    _batch_line(sweep, batch_work)
                if sweep_work == 0:
                    print("[compactor] drain complete — no compactable leaf dirs left", flush=True)
                    break
    except KeyboardInterrupt:
        totals["abort_reason"] = "interrupted"
        print("[compactor] drain interrupted — state is consistent (leaf commits are atomic); "
              "rerun to resume", flush=True)
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except OSError:
            pass
        totals["elapsed_s"] = round(time.time() - t0, 1)
    return totals


def _default_out_dir() -> str:
    from desk.runtime.paths import data_path, repo_path, resolve_data_rel
    try:
        from desk.types import CaptureConfig
        cfg = CaptureConfig.from_json(json.load(open(repo_path("desk", "capture_config.json"))))
        return resolve_data_rel(cfg.out_dir)
    except Exception:  # noqa: BLE001
        return data_path("data", "capture")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="capture store compactor (bounded pass / backlog drain)")
    ap.add_argument("--out-dir", default=None, help="capture out_dir (default: capture_config.json)")
    ap.add_argument("--drain", action="store_true", help="drain the whole backlog in bounded batches")
    ap.add_argument("--max-dirs", type=int, default=None,
                    help=f"leaf dirs per pass/batch (default {DEFAULT_MAX_DIRS} pass / 300 drain)")
    ap.add_argument("--min-age-s", type=float, default=MIN_AGE_S)
    ap.add_argument("--files-per-sec", type=float, default=None,
                    help="pacing budget (default: unpaced pass / 2000 drain)")
    ap.add_argument("--old-dates", type=int, default=DEFAULT_OLD_DATES,
                    help="old dates re-swept per scheduled pass (round-robin)")
    ap.add_argument("--max-batches", type=int, default=0, help="drain: stop after N batches (0=all)")
    ap.add_argument("--workers", type=int, default=4, help="drain: leaf-compaction threads")
    ap.add_argument("--health-url", default="http://127.0.0.1:8788/api/health")
    ap.add_argument("--no-health", action="store_true",
                    help="drain: skip health/fire gating (dev/test dirs only)")
    a = ap.parse_args(argv)
    out_dir = a.out_dir or _default_out_dir()
    if not os.path.isdir(out_dir):
        print(f"[compactor] out_dir missing: {out_dir}")
        return 2
    if a.drain:
        st = drain(out_dir, batch_dirs=(a.max_dirs or 300), min_age_s=a.min_age_s,
                   files_per_sec=(2000.0 if a.files_per_sec is None else a.files_per_sec),
                   health_url=a.health_url, max_batches=a.max_batches,
                   check_health=not a.no_health, workers=a.workers)
    else:
        st = compact_pass(out_dir, max_dirs=(a.max_dirs or DEFAULT_MAX_DIRS), min_age_s=a.min_age_s,
                          old_dates=a.old_dates,
                          files_per_sec=(a.files_per_sec or 0.0))
        tag = "locked (skipped)" if st["locked"] else (
            f"dirs={st['dirs_processed']} groups={st['groups']} parts_removed={st['parts_removed']} "
            f"rows={st['rows_merged']} recovered={st['recovered_manifests']}m/{st['recovered_parts']}p "
            f"tmp={st['tmp_cleaned']} skipped={st['skipped_groups']} in {st.get('elapsed_s', 0)}s")
        print(f"[compactor] pass: {tag}", flush=True)
    return 0 if not st.get("abort_reason") else 1


if __name__ == "__main__":
    sys.exit(main())
