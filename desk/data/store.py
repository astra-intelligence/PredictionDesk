"""data/store.py — one accessor over all data tiers.

The engine never reads files directly: it asks store for "the best data about
(market, token) at time t". store returns the HIGHEST fidelity tier present and
stamps which one was used, so estimated fills are never confused with exact ones.

Tiers in this build:
  T5  recorded full L2 book (capture Parquet)  -> exact ladder-walk  [primary]
  T1  mid price (CLOB /prices-history)         -> mid estimate       [fallback]
  T0  resolution outcome (Gamma/UMA)           -> settle 1/0

as-of(t): book@t = latest recorded snapshot with capture_ts <= t (no look-ahead).
Storage layout (contract with capture.py): out_dir/<date>/<market_id>/{book_snapshots,trades}/*.parquet
"""
from __future__ import annotations

import fcntl
import glob
import json
import os
import resource
import subprocess
import sys
import time
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pyarrow.parquet as pq
import pyarrow.dataset as ds

from desk.runtime.paths import data_path
from desk.types import BookSnapshot, Trade, Resolution

# A book older than this vs the query time is "stale" -> degraded warning.
DEFAULT_STALENESS_S = 30.0
# Fields of BookSnapshot we hydrate from a parquet row (ignore unknown columns).
_BOOK_FIELDS = set(BookSnapshot.__dataclass_fields__)  # type: ignore[attr-defined]
_TRADE_FIELDS = set(Trade.__dataclass_fields__)         # type: ignore[attr-defined]


@dataclass
class DataAtT:
    """fidelity-router result for (market, token, t). fill_model consumes this."""
    market_id: str
    token_id: str
    t: int
    tier: str                       # T5 | T1 | NONE
    book: Optional[BookSnapshot] = None
    mid: Optional[float] = None
    as_of_ts: Optional[int] = None
    stale_secs: Optional[float] = None
    degraded: bool = False          # True when tier < T5 OR book is stale
    warnings: List[str] = field(default_factory=list)


_RSS_CACHE = {"ts": 0.0, "mb": 0.0}   # throttle the macOS `ps` fallback to ~2s so it can't storm the scan


def _proc_rss_mb() -> float:
    """CURRENT process RSS in MB, stdlib-only (psutil is not installed). Must be CURRENT, not peak: the book-
    cache RSS backstop has to UN-trip once memory recovers, else a single transient spike would disable
    caching for the whole daemon lifetime (a latency cliff that could starve live fires). Linux: /proc.
    macOS: `ps -o rss=` (current). Peak ru_maxrss only as a last-resort fallback. Result cached ~2s."""
    now = time.time()
    if now - _RSS_CACHE["ts"] < 2.0:
        return _RSS_CACHE["mb"]
    mb = 0.0
    try:
        with open("/proc/self/statm") as f:                       # Linux: CURRENT rss (pages -> MB)
            mb = int(f.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") / (1024.0 * 1024.0))
    except Exception:
        try:                                                      # macOS: CURRENT rss via ps (KiB -> MB)
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                                 capture_output=True, text=True, timeout=2.0).stdout.strip()
            mb = int(out) / 1024.0 if out else 0.0
        except Exception:
            try:
                r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss   # last resort: PEAK (darwin=bytes)
                mb = r / (1024.0 * 1024.0) if sys.platform == "darwin" else r / 1024.0
            except Exception:
                mb = 0.0
    _RSS_CACHE["ts"], _RSS_CACHE["mb"] = now, mb
    return mb


def _dated_dirs_in_window(t: float, window_days: int) -> List[str]:
    """The UTC <date> dir names to glob for a book_at(t): (t - window_days) .. (t + 1 day), newest first.
    Bounds the read to the ACTIVE window instead of ALL dated dirs, keeping it off the cold multi-month tail
    (offline dataless stubs that hang the process on read + balloon RAM). NOTE: book_at returns the latest
    snap <= t regardless of age (staleness only FLAGS degraded, never drops), so window_days must comfortably
    exceed a live position's capture lifespan — else a still-held market's last snap falls outside the glob
    and book_at wrongly returns None (blocking its exit)."""
    day = datetime.fromtimestamp(t, tz=timezone.utc).date()
    return [(day - timedelta(days=d)).isoformat() for d in range(-1, max(0, window_days) + 1)]


class Store:
    def __init__(self, capture_dir: Optional[str] = None, sources=None,
                 staleness_s: float = DEFAULT_STALENESS_S, universe_ttl_s: float = 3600.0,
                 book_max_files: Optional[int] = None):
        # book_max_files: explicit per-instance override of DESK_BOOK_MAX_FILES (0 = read ALL part-files).
        # Callers needing full history (recon replaying an old fill) pass 0 here instead of reaching into
        # the private attribute — robust vs ambient env and attribute-name drift.
        # capture_dir default: env-resolved (DESK_DATA_DIR) / __file__-anchored — never cwd-relative
        # (the old "desk/data/capture" literal read an empty store from any non-repo-root cwd).
        self.capture_dir = (capture_dir or data_path("data", "capture")).rstrip("/")
        self.sources = sources                      # data/sources.py client (T1/T0); optional
        self.staleness_s = staleness_s
        self.universe_ttl_s = universe_ttl_s        # disk-cached universe index freshness (today-dir rescan is ~minutes at 700k files/day)
        self._universe_cache = None
        # LRU (bounded) so a per-fire universe scan can't accumulate ALL ~400 active markets' full-history
        # ladders in RAM (the unbounded memory growth that froze the box on large capture stores). Evicting a
        # prior market releases it; a later re-read is deterministic (same parquet) so correctness is
        # unchanged — only cache hit-rate.
        self._book_cache: "OrderedDict[str, Dict[str, List[BookSnapshot]]]" = OrderedDict()  # key: mkt|datetag
        self._book_window_days = int(os.environ.get("DESK_BOOK_WINDOW_DAYS", "10"))  # date-bound as-of reads;
        # must exceed a held market's capture lifespan (else a still-open position's last snap falls outside
        # the glob -> book_at None -> blocked exit). Weather markets resolve in days; 10 gives ample margin.
        self._book_cache_max = int(os.environ.get("DESK_BOOK_CACHE_MAX", "32"))      # markets kept resident
        self._book_max_files = (int(book_max_files) if book_max_files is not None
                                else int(os.environ.get("DESK_BOOK_MAX_FILES", "0")))  # 0=all (backtest). The LIVE
        # driver sets a small K: read only the NEWEST K part-files (= the current book) instead of a busy day's
        # ~400 per-market part-files — the unbounded read that balloons RAM (routing 20 mkts cost +422MB
        # reading all files).
        self._book_rss_budget_mb = float(os.environ.get("DESK_BOOK_RSS_BUDGET_MB", "2000"))  # hard backstop
        self._book_rss_warned = False

    # ── T5: recorded book ────────────────────────────────────────────────────
    def _load_market_books(self, market_id: str, t: Optional[float] = None) -> Dict[str, List[BookSnapshot]]:
        """market -> token -> capture_ts-sorted snapshots. Memory-bound:
          · PIN — a bare-`market_id` cache entry (no '|'), injected by backtests (e.g. a tenant sweep's
            wedge-avoidance guard + real-ladder T5 pins), is honored FIRST and is never date-scoped or
            evicted; its owner manages it. Production reads never set a bare key, so this is a no-op there.
          · DATE-BOUND — with an as-of `t` we glob ONLY the active-window dated dirs (t±window), never the
            cold multi-month tail (offline stubs that hang on read); t=None keeps the legacy read-all path
            for the offline callers that walk a market's full history.
          · LRU — date-scoped entries are bounded (self._book_cache_max markets); the balloon can't form.
            Re-reads on eviction are deterministic (perf, not correctness).
          · RSS GUARD — above budget we drop the date-scoped cache + stream (no-cache) and WARN: fail-soft
            on fidelity, never on the box. Uses CURRENT rss so it un-trips when memory recovers."""
        pinned = self._book_cache.get(market_id)                  # bare key = backtest-injected pin
        if pinned is not None:
            return pinned
        if t is None:
            tag, dated_dirs = "all", ["*"]                        # legacy: whole history, one glob
        else:
            ref = min(float(t), time.time() + 86400.0)            # clamp a future/sentinel t (runner's +inf
            tag = datetime.fromtimestamp(ref, tz=timezone.utc).date().isoformat()   # sentinel) to now so the
            dated_dirs = _dated_dirs_in_window(ref, self._book_window_days)          # window lands on real days
        key = f"{market_id}|{tag}"
        cached = self._book_cache.get(key)
        if cached is not None:
            self._book_cache.move_to_end(key)                     # LRU: mark most-recently-used
            return cached
        by_token: Dict[str, List[BookSnapshot]] = {}
        paths: List[str] = []
        for dd in dated_dirs:
            paths.extend(glob.glob(os.path.join(self.capture_dir, dd, market_id, "book_snapshots", "*.parquet")))
        # Live/recent read: the current book (staleness 30s) lives in the NEWEST few part-files; reading a busy
        # day's ~400 per-market part-files is what balloons RAM. Cap to the newest K (by mtime) for a recent
        # as-of; a backtest (t far in the past, or t=None) still reads the full set.
        recent = (t is not None) and (time.time() - float(t) < self._book_window_days * 86400.0 + 172800.0)
        if self._book_max_files and recent and len(paths) > self._book_max_files:
            paths.sort(key=lambda p: (os.path.getmtime(p) if os.path.exists(p) else 0.0), reverse=True)
            paths = paths[:self._book_max_files]
        for path in paths:
            try:
                rows = pq.read_table(path).to_pylist()
            except Exception:
                continue
            for r in rows:
                snap = BookSnapshot(**{k: v for k, v in r.items() if k in _BOOK_FIELDS})
                by_token.setdefault(snap.token_id, []).append(snap)
        for tok, snaps in by_token.items():
            snaps.sort(key=lambda s: s.capture_ts)
        if _proc_rss_mb() > self._book_rss_budget_mb:             # backstop: drop date-scoped entries (keep
            if not self._book_rss_warned:                         # pins), stream this one; CURRENT rss un-trips
                print(json.dumps({"store": "WARN book cache flushed",
                                  "rss_mb": round(_proc_rss_mb()), "budget_mb": round(self._book_rss_budget_mb),
                                  "action": "degrading to no-cache (stream per-market)"}), flush=True)
                self._book_rss_warned = True
            for k in [k for k in self._book_cache if "|" in k]:
                del self._book_cache[k]
            return by_token                                       # serve this market, don't retain it
        self._book_cache[key] = by_token
        evictable = [k for k in self._book_cache if "|" in k]     # pins (bare keys) are exempt from the cap
        while len(evictable) > self._book_cache_max:
            del self._book_cache[evictable.pop(0)]                # evict oldest date-scoped -> release books
        return by_token

    def universe(self) -> List[tuple]:
        """Discover captured (market_id, token_id, first_ts, last_ts, n_snaps) tuples.

        INCREMENTAL per-dated-dir index (v2):
          · in-memory cache honors universe_ttl_s too (a long-lived driver process must keep discovering
            newly listed markets — the frozen-snapshot blocker);
          · the TTL fast-path is only valid when the index COVERS every dated dir on disk (a fresh-but-
            partial index was previously served forever);
          · the ACTIVE window (today + yesterday, UTC) is always re-swept — nested part-file writes do not
            bump the dated dir's own mtime;
          · one unreadable chunk skips THAT chunk only (scanned_until not advanced, so its files retry) —
            a single bad parquet must never make a day unindexable;
          · index writes are fcntl-locked read-merge-write keyed on scanned_until, so concurrent writers
            (driver + backtests + a cold build) can't regress each other's entries.
        n_snaps is COUNTED PER SOURCE FILE and re-aggregated over the files present on disk each scan:
        so hour-*.parquet compaction (parts merged then deleted) and chunk-retry re-writes no longer
        double-count — a file's rows are counted once, and a removed file's contribution drops out. min/max
        ts — what as-of reads care about — are exact. Reads only 3 scalar columns per changed file.
        """
        now = time.time()
        if (getattr(self, "_universe_cache", None) is not None
                and now - getattr(self, "_universe_cache_ts", 0.0) < self.universe_ttl_s):
            return self._universe_cache
        idx = os.path.join(self.capture_dir, "_universe_index_v2.json")
        # date_dir -> {"mtime", "scanned_until", "files": {basename: {"mtime", "rows": {key: [min,max,cnt]}}}}
        # Counts are kept PER FILE so the dir total is re-summed over files present each scan — removal
        # (compaction) or re-write (chunk retry) of a file can't leave a stale count behind.
        state = {"dirs": {}}
        fresh = False
        try:
            fresh = (now - os.path.getmtime(idx)) < self.universe_ttl_s
            with open(idx) as f:
                state = json.load(f)
        except Exception:
            state, fresh = {"dirs": {}}, False

        def _locked_merge_write() -> None:
            """Read-merge-write under an fcntl lock: adopt the on-disk entry unless OURS scanned later."""
            try:
                lk = open(idx + ".lock", "w")
                fcntl.flock(lk, fcntl.LOCK_EX)
                try:
                    cur = {"dirs": {}}
                    try:
                        with open(idx) as f:
                            cur = json.load(f)
                    except Exception:
                        pass
                    for d2, e2 in state["dirs"].items():
                        c2 = cur["dirs"].get(d2)
                        if c2 is None or float(e2.get("scanned_until", 0)) >= float(c2.get("scanned_until", 0)):
                            cur["dirs"][d2] = e2
                    with open(idx + ".tmp", "w") as f:
                        json.dump(cur, f)
                    os.replace(idx + ".tmp", idx)
                    state["dirs"] = cur["dirs"]
                finally:
                    fcntl.flock(lk, fcntl.LOCK_UN)
                    lk.close()
            except Exception:
                pass

        if not os.path.isdir(self.capture_dir):
            return []                       # no capture dir yet (fresh DESK_DATA_DIR / clean tree):
                                            # an empty universe, the same clean-tree semantics every
                                            # caller already handles — never a FileNotFoundError crash
        disk_dirs = sorted(d for d in os.listdir(self.capture_dir)
                           if not d.startswith("_") and os.path.isdir(os.path.join(self.capture_dir, d)))
        # never index/scan the cold multi-month tail — those dated dirs can be offline dataless stubs that
        # HANG the process on read (ds.dataset materializes them). Bound to a retention window; the live
        # driver needs only the active window (lead<=6d). Backtests can raise DESK_UNIVERSE_RETENTION_DAYS.
        _ret_days = int(os.environ.get("DESK_UNIVERSE_RETENTION_DAYS", "45"))
        _scan_floor = time.strftime("%Y-%m-%d", time.gmtime(now - _ret_days * 86400.0))
        disk_dirs = [d for d in disk_dirs if d[:10] >= _scan_floor]
        active_floor = time.strftime("%Y-%m-%d", time.gmtime(now - 86400))
        covered = all(d in state["dirs"] for d in disk_dirs)
        if fresh and covered:
            need = [d for d in disk_dirs if d[:10] >= active_floor]
        elif fresh:
            need = [d for d in disk_dirs if d not in state["dirs"] or d[:10] >= active_floor]
        else:
            need = disk_dirs

        def _mt(path):
            try:
                return os.path.getmtime(path)
            except OSError:
                return None           # deleted between glob and stat (compactor race)

        for dd in need:
            dpath = os.path.join(self.capture_dir, dd)
            mtime = _mt(dpath)
            if mtime is None:
                continue
            ent = state["dirs"].get(dd)
            if ent is not None and ent.get("mtime") == mtime and dd[:10] < active_floor:
                continue              # settled dir, unchanged
            prev_files: Dict[str, dict] = dict((ent or {}).get("files", {}))
            since = float((ent or {}).get("scanned_until", 0)) - 120.0
            now_scan = time.time()
            on_disk = {os.path.relpath(f, dpath): f
                       for f in glob.glob(os.path.join(dpath, "*", "book_snapshots", "*.parquet"))}
            # keep counts for files still present + unchanged (mtime match); drop vanished files' counts —
            # this is what makes n_snaps idempotent under compaction (parts removed) and re-write (retry).
            new_files: Dict[str, dict] = {}
            to_scan: List[str] = []
            for rel, path in on_disk.items():
                fm = _mt(path)
                pe = prev_files.get(rel)
                if pe is not None and fm is not None and pe.get("mtime") == fm and fm <= since:
                    new_files[rel] = pe          # unchanged since last full scan — reuse its per-file counts
                else:
                    to_scan.append(path)         # new / modified / undated — (re)count from the file itself
            scan_complete = True
            # CHUNKED + use_threads=False: pyarrow's AsyncScanner deadlocks (FutureImpl::Wait, observed
            # at high fragment counts); the sync scanner in bounded chunks is deterministic.
            for ci in range(0, len(to_scan), 2000):
                chunk = to_scan[ci:ci + 2000]
                try:
                    # per-fragment tables (keyed on each fragment's OWN path, not zip order) so a file's
                    # row count is attributed to exactly that file — the unit we store and re-aggregate.
                    frags = ds.dataset(chunk, format="parquet").get_fragments()
                    tbls = [(fr.path, fr.to_table(columns=["market_id", "token_id", "capture_ts"]))
                            for fr in frags]
                except Exception:
                    scan_complete = False     # skip THIS chunk only; scanned_until stays put so it retries
                    continue
                for path, tbl in tbls:
                    rows: Dict[str, list] = {}
                    for m, tok, ts in zip(tbl.column("market_id").to_pylist(),
                                          tbl.column("token_id").to_pylist(),
                                          tbl.column("capture_ts").to_pylist()):
                        e = rows.get(f"{m}|{tok}")
                        if e is None:
                            rows[f"{m}|{tok}"] = [ts, ts, 1]
                        else:
                            e[0] = min(e[0], ts); e[1] = max(e[1], ts); e[2] += 1
                    new_files[os.path.relpath(path, dpath)] = {"mtime": _mt(path), "rows": rows}
            if not scan_complete:
                # a chunk failed: don't drop the files we couldn't rescan this cycle (they retry next time)
                for rel, pe in prev_files.items():
                    new_files.setdefault(rel, pe)
            state["dirs"][dd] = {
                "mtime": (mtime if scan_complete else 0.0),
                "scanned_until": (now_scan if scan_complete
                                  else float((ent or {}).get("scanned_until", 0))),
                "files": new_files}
            _locked_merge_write()     # partial state per dir — an interruption loses ≤1 dir of work

        merged: Dict[str, list] = {}
        for ent in state["dirs"].values():
            if "files" in ent:                       # new per-file shape: sum rows over files present
                aggs = (agg for fe in ent["files"].values() for agg in fe.get("rows", {}).items())
            else:                                    # legacy 'per' shape (pre item-6 index): use until rescanned
                aggs = ent.get("per", {}).items()
            for key, agg in aggs:
                e = merged.get(key)
                if e is None:
                    merged[key] = list(agg)
                else:
                    e[0] = min(e[0], agg[0]); e[1] = max(e[1], agg[1]); e[2] += agg[2]
        out = [tuple(key.split("|", 1)) + tuple(agg) for key, agg in merged.items()]
        self._universe_cache = out
        self._universe_cache_ts = now
        return out

    def live_universe(self) -> List[tuple]:
        """LIVE/forward market discovery: the Gamma-derived captured set from `_universe.json` (built by
        capture_select.py from Gamma /markets), NOT the full historical parquet scan `universe()`. universe()
        indexes first/last-ts + n_snaps across ~700k files/day — a BACKTEST need; a live fire only needs
        "which markets are open now + their current book". Returns (market_id, token_id) per captured token
        (the strategy then filters to its weather cohort + routes only those books, date-bounded). Light: a
        ~1MB JSON, zero parquet reads. TTL-cached like universe()."""
        now = time.time()
        if (getattr(self, "_live_universe_cache", None) is not None
                and now - getattr(self, "_live_universe_ts", 0.0) < self.universe_ttl_s):
            return self._live_universe_cache
        rows: List[tuple] = []
        try:
            with open(os.path.join(self.capture_dir, "_universe.json")) as f:
                toks = json.load(f).get("tokens", {})
            src = toks.values() if isinstance(toks, dict) else toks
            rows = [(r.get("market_id"), r.get("token_id")) for r in src
                    if r.get("market_id") and r.get("token_id")]
        except Exception:
            rows = []
        self._live_universe_cache = rows
        self._live_universe_ts = now
        return rows

    def book_at(self, market_id: str, token_id: str, t: int) -> Optional[BookSnapshot]:
        """as-of(t): latest recorded snapshot with capture_ts <= t. None if uncaptured."""
        snaps = self._load_market_books(market_id, t=t).get(token_id, [])
        if not snaps:
            return None
        i = bisect_right([s.capture_ts for s in snaps], t)
        return snaps[i - 1] if i > 0 else None

    def prints_between(self, market_id: str, token_id: str, t0: int, t1: int) -> List[Trade]:
        """Captured Data /trades prints in [t0, t1] — supplementary reality (no fee).
        Used by the evaluator to estimate latency / queue from volume-through."""
        out: List[Trade] = []
        cap_t = time.time() + 86400.0                                                # clamp sentinel/huge t1
        d0 = datetime.fromtimestamp(min(float(t0), cap_t), tz=timezone.utc).date() - timedelta(days=1)  # date-
        d1 = datetime.fromtimestamp(min(float(t1), cap_t), tz=timezone.utc).date() + timedelta(days=1)  # bound:
        dated_dirs, d = [], d0                                    # prints in [t0,t1] live only in those days
        while d <= d1:
            dated_dirs.append(d.isoformat()); d += timedelta(days=1)
        for dd in dated_dirs:
            pattern = os.path.join(self.capture_dir, dd, market_id, "trades", "*.parquet")
            for path in glob.glob(pattern):
                try:
                    rows = pq.read_table(path).to_pylist()
                except Exception:
                    continue
                for r in rows:
                    if r.get("token_id") != token_id:
                        continue
                    ts = r.get("ts", 0)
                    if t0 <= ts <= t1:
                        out.append(Trade(**{k: v for k, v in r.items() if k in _TRADE_FIELDS}))
        out.sort(key=lambda x: x.ts)
        return out

    # ── T1: mid fallback ──────────────────────────────────────────────────────
    def mid_at(self, market_id: str, token_id: str, t: int) -> Optional[float]:
        """Best mid at t: recorded T5 book, else CLOB /prices-history."""
        snap = self.book_at(market_id, token_id, t)
        if snap is not None:
            return snap.mid
        if self.sources is not None:
            return self.sources.mid_at(token_id, t)
        return None

    # ── T0: resolution (LOCAL resolutions.jsonl first, Gamma fallback) ──────────
    def _load_resolutions(self) -> Dict[tuple, Resolution]:
        """Parse the capture daemon's append-only resolutions.jsonl into
        {(market_id, token_id): Resolution}. Cached, invalidated on (mtime, size) change — the
        long-running driver holds ONE Store, so a forever-cache would hide every resolution the
        capture daemon appends after the driver's first settle pass (positions resolved after start
        could never settle until restart). This is the authoritative local record of resolved
        markets — they drop out of the live universe but persist here."""
        path = os.path.join(self.capture_dir, "resolutions.jsonl")
        try:
            st = os.stat(path)
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            stamp = None
        if getattr(self, "_res_cache", None) is not None and getattr(self, "_res_cache_stamp", ()) == stamp:
            return self._res_cache
        out: Dict[tuple, Resolution] = {}
        # Contract: resolutions.jsonl is APPEND-ONLY, one JSON object per line, written by the live
        # daemon. We read it lockless on purpose (read-only discovery); a mid-write partial last line
        # fails json.loads and is skipped — intentional, not a bug. A later rebuild picks it up.
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    # T0 truth is BINARY: accept only payout exactly 0.0 or 1.0. A None (merely-closed,
                    # not-yet-reported) or mid value (0.5 outcomePrices) coerced to a float here would
                    # settle positions confidently WRONG — skip the row; the Gamma/CLOB fallback (or a
                    # later clean row) still settles the market.
                    try:
                        payout = float(r.get("payout"))
                    except (TypeError, ValueError):
                        continue
                    if payout not in (0.0, 1.0):
                        continue
                    out[(r.get("market_id"), r.get("token_id"))] = Resolution(
                        market_id=r.get("market_id", ""), token_id=r.get("token_id", ""),
                        resolved_ts=int(r.get("resolved_ts", 0) or 0),
                        payout=payout,
                        outcome=r.get("outcome", ""), source=r.get("source", "GAMMA"))
        except FileNotFoundError:
            pass
        self._res_cache = out
        self._res_cache_stamp = stamp
        return out

    def resolution(self, market_id: str, token_id: str) -> Optional[Resolution]:
        """Resolved-market payout. Checks the LOCAL resolutions.jsonl first (works offline,
        covers markets that have left the live universe), then falls back to live Gamma."""
        local = self._load_resolutions().get((market_id, token_id))
        if local is not None:
            return local
        if self.sources is None:
            return None
        return self.sources.resolution(market_id, token_id)

    def resolved_markets(self) -> List[Resolution]:
        """All locally-recorded resolved (market, token) outcomes — the discoverable list of
        markets you can backtest end-to-end (captured books up to resolution + known payout)."""
        return list(self._load_resolutions().values())

    # ── fidelity router ─────────────────────────────────────────────────────────
    def route(self, market_id: str, token_id: str, t: int) -> DataAtT:
        """Pick the highest available tier; flag degradation/staleness explicitly."""
        d = DataAtT(market_id=market_id, token_id=token_id, t=t, tier="NONE")
        snap = self.book_at(market_id, token_id, t)
        if snap is not None:
            d.tier = "T5"
            d.book = snap
            d.mid = snap.mid
            d.as_of_ts = snap.capture_ts
            d.stale_secs = max(0.0, t - snap.capture_ts)
            if d.stale_secs > self.staleness_s:
                d.degraded = True
                d.warnings.append(
                    f"T5 book is {d.stale_secs:.0f}s stale (>{self.staleness_s:.0f}s) — fill is approximate")
            return d
        mid = self.mid_at(market_id, token_id, t)
        if mid is not None:
            d.tier = "T1"
            d.mid = mid
            d.degraded = True
            d.warnings.append("no recorded book@t — fell back to T1 mid; fill is an ESTIMATE, not a ladder-walk")
            return d
        d.degraded = True
        d.warnings.append("no data at t for (market,token) — cannot fill")
        return d
