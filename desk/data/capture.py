"""ENTRYPOINT: run(cfg) -> CaptureStats.

Forward market-data collector. Polls CLOB /books (full ladders) + Data /trades, writes
hash-deduped BookSnapshot rows + Trade rows to partitioned zstd Parquet. Crash-safe
(atomic part files), durable (runs under a process supervisor), self-reporting (_heartbeat). No auth, no WS,
no reconstruction.

Run:  python3 -m desk.data.capture [config.json]
"""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests

from desk.data.capture_select import select_universe, write_universe, paced_map
from desk.types import CaptureConfig, CaptureStats

# ----- parquet schemas (stable contract with data/store.py) -----
_LEVEL = pa.list_(pa.list_(pa.float64()))
BOOK_SCHEMA = pa.schema([
    ("market_id", pa.string()), ("token_id", pa.string()),
    ("capture_ts", pa.int64()), ("exchange_ts", pa.int64()), ("hash", pa.string()),
    ("bids", _LEVEL), ("asks", _LEVEL),
    ("best_bid", pa.float64()), ("best_ask", pa.float64()), ("mid", pa.float64()),
    ("spread", pa.float64()), ("tick_size", pa.float64()), ("min_order_size", pa.float64()),
    ("neg_risk", pa.bool_()), ("last_trade_price", pa.float64()),
    ("fee_rate_base", pa.float64()), ("fidelity_tier", pa.string()),
])
TRADE_SCHEMA = pa.schema([
    ("market_id", pa.string()), ("token_id", pa.string()), ("ts", pa.int64()),
    ("price", pa.float64()), ("size", pa.float64()), ("side", pa.string()),
    ("tx_hash", pa.string()), ("wallet", pa.string()), ("outcome", pa.string()),
    ("fee_rate_bps", pa.float64()),
])


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _to_secs(ts) -> int:
    """Normalize a unix timestamp to seconds (CLOB book.timestamp is ms; trades are s)."""
    v = int(_f(ts))
    return v // 1000 if v > 10_000_000_000 else v


def _utc_date(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def _atomic_write_table(rows: List[dict], schema: pa.Schema, out_dir: str, stream: str, tag: str) -> int:
    """Write rows to out_dir/<date>/<market_id>/<stream>/part-<tag>.parquet atomically. Returns bytes."""
    by_part: Dict[tuple, List[dict]] = defaultdict(list)
    ts_key = "capture_ts" if stream == "book_snapshots" else "ts"
    for r in rows:
        by_part[(_utc_date(r[ts_key]), r["market_id"])].append(r)
    total = 0
    for (date, market_id), part_rows in by_part.items():
        d = os.path.join(out_dir, date, market_id, stream)
        os.makedirs(d, exist_ok=True)
        final = os.path.join(d, f"part-{tag}.parquet")
        tmp = final + ".tmp"
        table = pa.Table.from_pylist(part_rows, schema=schema)
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, final)
        total += os.path.getsize(final)
    return total


def _book_row(book: dict, market_id: str, capture_ts: int, fee_rate: Optional[float] = None) -> Optional[dict]:
    token_id = str(book.get("asset_id") or "")
    if not token_id:
        return None
    bids = sorted(([_f(l["price"]), _f(l["size"])] for l in book.get("bids", [])),
                  key=lambda x: x[0], reverse=True)
    asks = sorted(([_f(l["price"]), _f(l["size"])] for l in book.get("asks", [])),
                  key=lambda x: x[0])
    best_bid = bids[0][0] if bids else 0.0
    best_ask = asks[0][0] if asks else 0.0
    both = bool(bids) and bool(asks)
    ltp = book.get("last_trade_price")
    return {
        "market_id": market_id, "token_id": token_id,
        "capture_ts": capture_ts, "exchange_ts": _to_secs(book.get("timestamp")) or capture_ts,
        "hash": str(book.get("hash") or ""), "bids": bids, "asks": asks,
        "best_bid": best_bid, "best_ask": best_ask,
        "mid": round((best_bid + best_ask) / 2, 6) if both else 0.0,
        "spread": round(best_ask - best_bid, 6) if both else 0.0,
        "tick_size": _f(book.get("tick_size")), "min_order_size": _f(book.get("min_order_size")),
        "neg_risk": bool(book.get("neg_risk", False)),
        "last_trade_price": _f(ltp) if ltp is not None else None,
        "fee_rate_base": fee_rate,
        "fidelity_tier": "T5",
    }


def _chunks(seq: List, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class Collector:
    def __init__(self, cfg: CaptureConfig, universe: Dict[str, dict]):
        self.cfg = cfg
        self.universe = universe                      # token_id -> meta
        self.token_ids = list(universe.keys())
        self.market_of = {t: u["market_id"] for t, u in universe.items()}
        self.conds = sorted({u["market_id"] for u in universe.values()})
        self.sess = requests.Session()
        self.last_hash: Dict[str, str] = {}
        self.fee_rate: Dict[str, float] = {}          # token_id -> base_fee (from /fee-rate)
        self.seen_trades: set = set()
        self.book_buf: List[dict] = []
        self.trade_buf: List[dict] = []
        self.trade_lock = threading.Lock()   # trade_buf written by aux thread, drained by main flush
        self.ledger_buf: List[dict] = []   # per-poll coverage records (gap vs quiet-market truth)
        self.refresh_on_start = False      # set by run() when started from a previous universe
        self.resolved_conds: set = set()   # market_ids already in resolutions.jsonl (dedup)
        rpath = os.path.join(cfg.out_dir, "resolutions.jsonl")
        if os.path.exists(rpath):
            try:
                for line in open(rpath):
                    self.resolved_conds.add(json.loads(line).get("market_id"))
            except (ValueError, OSError):
                pass
        self.stats = CaptureStats(tokens=len(self.token_ids), started_ts=int(time.time()))
        self.flush_idx = 0
        self.last_ok_poll = time.time()
        self._stop = False

    # ---- polling ----
    def _fetch_fee(self, tok: str) -> None:
        try:
            r = requests.get(f"{self.cfg.clob_url}/fee-rate", params={"token_id": tok}, timeout=15)
            r.raise_for_status()
            self.fee_rate[tok] = _f(r.json().get("base_fee"))
        except Exception:  # noqa: BLE001
            pass

    def refresh_fees(self, tokens: Optional[List[str]] = None) -> None:
        # /fee-rate is a constant per token (base_fee=1000) — fetch ONCE and cache, never re-poll.
        # Only fetch tokens not already cached (initial fill + newly-added markets). Paced + aux thread.
        toks = [t for t in (tokens if tokens is not None else self.token_ids) if t not in self.fee_rate]
        if toks:
            paced_map(self._fetch_fee, toks, per_10s=400, workers=16)

    def refresh_universe_now(self) -> None:
        """Hourly: drop resolved/closed markets (recording their resolution), backfill new markets in
        the same lanes. Keeps the dataset current as markets settle — no restart needed."""
        from desk.data.capture_select import refresh_universe, write_universe
        try:
            r = refresh_universe(self.cfg, self.universe)
        except Exception as e:  # noqa: BLE001 — never let a refresh crash the collector
            print(f"[capture] universe refresh failed (keeping current): {e}")
            return
        new_uni = r["universe"]
        if not new_uni:
            print("[capture] refresh returned empty universe — keeping current")
            return
        old = set(self.token_ids)
        self.universe = new_uni
        self.token_ids = list(new_uni.keys())
        self.market_of = {t: u["market_id"] for t, u in new_uni.items()}
        self.conds = sorted({u["market_id"] for u in new_uni.values()})
        new = set(self.token_ids)
        for t in old - new:                      # stop tracking dropped tokens
            self.last_hash.pop(t, None)
            self.fee_rate.pop(t, None)
        self.stats.tokens = len(self.token_ids)
        write_universe(self.cfg, new_uni)
        fresh_res = [row for row in r["resolutions"] if row["market_id"] not in self.resolved_conds]
        if fresh_res:                            # T0 truth: append-only resolution store (deduped)
            with open(os.path.join(self.cfg.out_dir, "resolutions.jsonl"), "a") as f:
                for row in fresh_res:
                    f.write(json.dumps(row) + "\n")
                    self.resolved_conds.add(row["market_id"])
        if new - old:
            self.refresh_fees(list(new - old))   # fees for newly-added tokens only
        print(f"[capture] universe refresh: +{len(r['added'])} −{len(r['removed'])} markets, "
              f"{len(r['resolved'])} resolved ({len(fresh_res)} new resolution rows)")

    def _fetch_books(self, batch: List[str]):
        try:
            r = requests.post(f"{self.cfg.clob_url}/books",
                              json=[{"token_id": t} for t in batch], timeout=12)
            r.raise_for_status()
            return r.json() or []
        except Exception:  # noqa: BLE001 — skip slow/throttled batch, retry next poll
            return None

    def poll_books(self) -> None:
        ts = int(time.time())
        got = False
        errs = 0
        tokens_ok = 0
        failed_markets: set = set()
        batches = list(_chunks(self.token_ids, 50))
        with ThreadPoolExecutor(max_workers=10) as ex:
            # zip keeps batch<->result alignment so we know WHICH markets a failed batch covered
            for batch, books in zip(batches, ex.map(self._fetch_books, batches)):
                if books is None:
                    errs += 1
                    failed_markets.update(self.market_of.get(t, "") for t in batch)
                    continue
                got = True
                tokens_ok += len(books)
                for book in books:
                    tok = str(book.get("asset_id"))
                    row = _book_row(book, self.market_of.get(tok, ""), ts, self.fee_rate.get(tok))
                    if not row or not row["market_id"]:
                        continue
                    if self.last_hash.get(row["token_id"]) == row["hash"]:
                        continue  # hash-dedup: book unchanged
                    self.last_hash[row["token_id"]] = row["hash"]
                    self.book_buf.append(row)
        self.stats.polls += 1
        # poll ledger: one record per poll → distinguishes downtime/partial-failure from quiet markets
        self.ledger_buf.append({
            "poll_ts": ts, "attempted": len(self.token_ids), "tokens_ok": tokens_ok,
            "batches_failed": errs, "failed_markets": sorted(m for m in failed_markets if m),
        })
        if errs:
            print(f"[capture] /books: {errs}/{len(batches)} batches errored (retry next poll)")
        if got:
            gap = time.time() - self.last_ok_poll
            self.stats.max_gap_s = round(max(self.stats.max_gap_s, gap), 2)
            self.last_ok_poll = time.time()

    def _fetch_trades(self, cond: str):
        try:
            r = requests.get(f"{self.cfg.data_url}/trades",
                             params={"market": cond, "limit": "100"}, timeout=8)
            r.raise_for_status()
            return cond, (r.json() or [])
        except Exception:  # noqa: BLE001 — supplementary feed; tolerate slow/timeout markets
            return cond, None

    def poll_trades(self) -> None:
        errs = 0
        rows: List[dict] = []
        # PACED under Data /trades 200/10s (~30s for 506 markets); aux thread, doesn't block books
        results = paced_map(self._fetch_trades, self.conds, per_10s=160, workers=12)
        for cond, trades in results:
            if trades is None:
                errs += 1
                continue
            for t in trades:
                key = (t.get("transactionHash"), t.get("asset"), t.get("side"),
                       t.get("size"), t.get("price"), t.get("timestamp"))
                if key in self.seen_trades:
                    continue
                self.seen_trades.add(key)
                rows.append({
                    "market_id": t.get("conditionId") or cond,
                    "token_id": str(t.get("asset") or ""),
                    "ts": _to_secs(t.get("timestamp")), "price": _f(t.get("price")),
                    "size": _f(t.get("size")), "side": str(t.get("side") or ""),
                    "tx_hash": str(t.get("transactionHash") or ""),
                    "wallet": str(t.get("proxyWallet") or ""),
                    "outcome": str(t.get("outcome") or ""),
                    "fee_rate_bps": self.fee_rate.get(str(t.get("asset") or "")),
                })
        with self.trade_lock:
            self.trade_buf.extend(rows)
        if errs:
            print(f"[capture] /trades: {errs}/{len(self.conds)} markets errored/timed-out (tolerated)")
        # bound memory: /trades only returns recent prints, so an old dedup cache is useless
        if len(self.seen_trades) > 500_000:
            self.seen_trades.clear()

    # ---- durability ----
    def flush(self) -> None:
        if self.book_buf:
            tag = f"{os.getpid()}-{int(time.time())}-{self.flush_idx}"
            self.stats.bytes += _atomic_write_table(self.book_buf, BOOK_SCHEMA,
                                                     self.cfg.out_dir, "book_snapshots", tag)
            self.stats.book_rows += len(self.book_buf)
            self.book_buf = []
        with self.trade_lock:
            tb, self.trade_buf = self.trade_buf, []
        if tb:
            tag = f"{os.getpid()}-{int(time.time())}-{self.flush_idx}"
            self.stats.bytes += _atomic_write_table(tb, TRADE_SCHEMA,
                                                     self.cfg.out_dir, "trades", tag)
            self.stats.trade_rows += len(tb)
        if self.ledger_buf:
            # append-only JSONL (one line per poll). Resilient: a transient write/permission error
            # keeps the buffer for the next flush instead of crashing the collector.
            try:
                with open(os.path.join(self.cfg.out_dir, "poll_ledger.jsonl"), "a") as f:
                    for e in self.ledger_buf:
                        f.write(json.dumps(e) + "\n")
                self.ledger_buf = []
            except OSError as e:
                print(f"[capture] ledger write deferred ({type(e).__name__}: {e})")
        self.flush_idx += 1
        self._heartbeat()
        self.stats.max_gap_s = 0.0  # windowed: heartbeat reports worst gap since last flush, not all-time

    def _heartbeat(self) -> None:
        path = os.path.join(self.cfg.out_dir, "_heartbeat")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": int(time.time()), "polls": self.stats.polls,
                       "book_rows": self.stats.book_rows, "trade_rows": self.stats.trade_rows,
                       "tokens": self.stats.tokens, "max_gap_s": self.stats.max_gap_s,
                       "pid": os.getpid()}, f)
        os.replace(tmp, path)

    def _write_stats(self) -> None:
        self.stats.ended_ts = int(time.time())
        with open(os.path.join(self.cfg.out_dir, "_stats.json"), "w") as f:
            json.dump(self.stats.to_dict(), f, indent=2)

    def stop(self, *_):
        self._stop = True

    def _aux_loop(self) -> None:
        """Auxiliary ops (trades/fees/universe-refresh) on a SEPARATE thread so they NEVER block book
        polling. Each can take minutes when an endpoint rate-limits overnight; books stay at full cadence.
        """
        last_trades = last_fees = 0.0   # fire immediately on start (fees populate before first trades)
        last_refresh = 0.0 if self.refresh_on_start else time.time()
        while not self._stop:
            now = time.time()
            try:
                if now - last_fees >= self.cfg.fee_refresh_secs:
                    self.refresh_fees(); last_fees = now
                if now - last_refresh >= self.cfg.refresh_universe_secs:
                    self.refresh_universe_now(); last_refresh = now
                if now - last_trades >= self.cfg.trades_poll_secs:
                    self.poll_trades(); last_trades = now
            except Exception as e:  # noqa: BLE001 — aux must never crash the collector
                print(f"[capture] aux error (continuing): {e}")
            for _ in range(20):     # ~2s, but wake promptly on stop
                if self._stop:
                    break
                time.sleep(0.1)

    # ---- main loop (BOOKS ONLY — never blocked by aux ops) ----
    def run(self) -> CaptureStats:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        os.makedirs(self.cfg.out_dir, exist_ok=True)
        print(f"[capture] {self.stats.tokens} tokens / {len(self.conds)} markets; "
              f"poll={self.cfg.poll_secs}s flush={self.cfg.flush_secs}s out={self.cfg.out_dir}")
        aux = threading.Thread(target=self._aux_loop, daemon=True)
        aux.start()
        last_flush = 0.0
        while not self._stop:
            t0 = time.time()
            try:                               # transient poll/write/permission errors must NOT crash
                self.poll_books()
                if t0 - last_flush >= self.cfg.flush_secs:
                    self.flush()
                    last_flush = t0
            except Exception as e:  # noqa: BLE001 — log + keep going (KeepAlive is only the backstop)
                print(f"[capture] loop error (continuing): {type(e).__name__}: {e}")
            if self.cfg.max_secs and (time.time() - self.stats.started_ts) >= self.cfg.max_secs:
                break
            elapsed = time.time() - t0
            if not self._stop:
                time.sleep(max(0.0, self.cfg.poll_secs - elapsed))
        self._stop = True
        aux.join(timeout=30)
        self.flush()
        self._write_stats()
        print(f"[capture] stopped. book_rows={self.stats.book_rows} trade_rows={self.stats.trade_rows} "
              f"bytes={self.stats.bytes} max_gap_s={self.stats.max_gap_s}")
        return self.stats


def run(cfg: CaptureConfig) -> CaptureStats:
    # Start from the PREVIOUS universe if present, so markets that resolved while we were down are still
    # in the set and get their resolution recorded by the early refresh. Else fresh-select.
    universe, from_prev = {}, False
    prev_path = os.path.join(cfg.out_dir, "_universe.json")
    if os.path.exists(prev_path):
        try:
            universe = json.load(open(prev_path)).get("tokens", {})
            from_prev = bool(universe)
        except (ValueError, OSError):
            universe = {}
    if not universe:
        universe = select_universe(cfg)
    if not universe:
        raise SystemExit("[capture] empty universe — check capture_config.json tags/slugs/filters")
    write_universe(cfg, universe)
    c = Collector(cfg, universe)
    c.refresh_on_start = from_prev   # refresh early to record downtime resolutions + roll to current
    return c.run()


if __name__ == "__main__":
    from desk.runtime.guard import rotate_log
    from desk.runtime.paths import data_path, repo_path, resolve_data_rel
    for _lg in ("capture.out", "capture.err"):
        rotate_log(data_path("data", "capture", _lg))
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else repo_path("desk", "capture_config.json")
    _cfg = CaptureConfig.from_json(json.load(open(cfg_path)))
    _cfg.out_dir = resolve_data_rel(_cfg.out_dir)   # a relative config out_dir must never follow cwd
    run(_cfg)
