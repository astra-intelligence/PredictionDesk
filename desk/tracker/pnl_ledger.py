"""tracker/pnl_ledger.py — PERSISTENT cross-run P&L + attribution AND the ONE trade-event log.

Two outputs, one writer:
  • ledger.json  — cross-run AGGREGATES per strategy_id (realized/unrealized/fees, settle
    counts by source). sim/portfolio is per-run/in-memory; this accrues across runs.
  • trades.jsonl — append-only TradeRow EVENT log, one row per order event (open/resting/
    closed) for backtest·paper·live. The dashboard's single contract. decomp + slug are
    logged AT TRADE TIME (Gamma drops resolved markets → a closed trade can't be repriced
    later); every row carries run/config/env so a contaminated span is machine-identifiable.

Concurrent writers (the driver fires strategies serially, but backtests + the driver may
append at once): each jsonl append takes a kernel fcntl.flock on the file — advisory but
CROSS-PROCESS. log_run/log_settle take OPTIONAL kwargs so every existing caller keeps
working; only when run_meta/signals are passed do TradeRows emit.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from desk.runtime.paths import data_path
from desk.types import BacktestResult, ExecReport, LedgerSnapshot, TradeRow

# env-resolved (DESK_DATA_DIR) with the historical in-package tracker/ location as the default target
DEFAULT_PATH = data_path("tracker", "ledger.json")
DEFAULT_TRADES_PATH = data_path("tracker", "trades.jsonl")

Key = Tuple[str, str]

_HEX_ID = re.compile(r"^0x[0-9a-fA-F]{40,}$")   # conditionId masquerading as a slug → dead polymarket URL
_SLUG_INDEXES = (                                # market_id → slug lookups, best-first (absolute paths)
    (data_path("data", "capture", "_universe.json"), "universe"),
)
_slug_cache: Optional[Dict[str, str]] = None
_slug_cache_key: tuple = ()


def _mtime(p: str) -> float:
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def _slug_index() -> Dict[str, str]:
    """Lazy market_id→slug map from local indexes (never network). Strategies SHOULD emit the real
    slug in signals; this is the log-time backstop so a TradeRow never ships a 0x id as its slug.
    mtime-keyed: the driver is long-lived and the capture universe keeps growing."""
    global _slug_cache, _slug_cache_key
    key = tuple(_mtime(p) for p, _ in _SLUG_INDEXES)
    if key != _slug_cache_key:
        _slug_cache, _slug_cache_key = None, key
    if _slug_cache is None:
        m: Dict[str, str] = {}
        for p, kind in _SLUG_INDEXES:
            try:
                with open(p) as f:
                    d = json.load(f)
                rows = d.get("tokens", {}).values() if kind == "universe" else \
                    [{"market_id": k, "slug": (v or {}).get("slug")} for k, v in d.items()]
                for r in rows:
                    s = r.get("slug")
                    if s and not _HEX_ID.match(s):
                        m.setdefault(r["market_id"], s)
            except Exception:
                pass
        _slug_cache = m
    return _slug_cache


def _real_slug(slug: Optional[str], market_id: Optional[str]) -> Optional[str]:
    """Signal slug if it's a real slug; else resolve market_id via local indexes; else None
    (→ url None → dashboard renders '—', never a dead link)."""
    if slug and not _HEX_ID.match(slug):
        return slug
    return _slug_index().get(market_id or "")


def _slug_url(slug: Optional[str]) -> Optional[str]:
    # /market/<slug> 302s to the canonical /event/... page; /market/<0xconditionId> 404s
    return f"https://polymarket.com/market/{slug}" if slug else None


class PnLLedger:
    def __init__(self, path: str = DEFAULT_PATH, trades_path: Optional[str] = None):
        self.path = path
        # Trades log lives next to the ledger json (so a temp-dir ledger gets a temp-dir trades log too).
        self.trades_path = trades_path or os.path.join(os.path.dirname(path) or ".", "trades.jsonl")
        self.state: Dict = {"by_strategy": {}, "runs_logged": 0, "as_of": 0}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.state = json.load(f)
            except Exception:
                pass

    def _strat(self, sid: str) -> Dict:
        return self.state["by_strategy"].setdefault(
            sid, {"realized": 0.0, "unrealized": 0.0, "fees": 0.0})

    # ── TradeRow event log ────────────────────────────────────────────────────
    def _append_trade(self, row: TradeRow) -> None:
        """Append one TradeRow as a jsonl line under a kernel fcntl.flock (advisory, CROSS-PROCESS —
        serializes the driver vs concurrent backtest appenders; do not remove as 'in-process only')."""
        os.makedirs(os.path.dirname(self.trades_path) or ".", exist_ok=True)
        with open(self.trades_path, "a") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(json.dumps(row.to_dict(), separators=(",", ":")) + "\n")
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _emit_open_rows(self, reports: List[ExecReport], run_meta: dict,
                        signals: Dict[Key, dict]) -> int:
        """One TradeRow per ExecReport with a fill (status=open; a maker order that rested
        unfilled → status=resting). fair/edge/decomp/slug are joined from `signals` keyed
        (market_id, token_id). We recover (market_id, token_id, side, mode, limit) from the
        signal + report; the driver passes them via run_meta['orders_by_id'] when available."""
        orders_by_id = run_meta.get("orders_by_id", {})
        run_id = run_meta.get("run_id", "")
        source = run_meta.get("source", "backtest")
        strategy = run_meta.get("strategy", "")
        cfg_hash = run_meta.get("config_hash", "")
        n = 0
        for r in reports:
            o = orders_by_id.get(r.order_id)
            mkt = getattr(o, "market_id", None) or run_meta.get("market_of", {}).get(r.order_id, "")
            tok = getattr(o, "token_id", None) or run_meta.get("token_of", {}).get(r.order_id, "")
            sig = signals.get((mkt, tok), {}) if signals else {}
            filled = r.filled_qty
            resting = (getattr(o, "mode", None) == "maker") and filled <= 0 and r.unfilled_qty > 0
            if filled <= 0 and not resting:
                continue                         # no fill, not a resting maker → nothing happened
            status = "resting" if resting else "open"
            qty = r.unfilled_qty if resting else filled
            slug = _real_slug(sig.get("slug"), mkt)
            row = TradeRow(
                run_id=run_id, source=source, strategy=strategy, config_hash=cfg_hash,
                order_id=r.order_id, market_id=mkt, token_id=tok,
                slug=slug, url=_slug_url(slug),
                ts=run_meta.get("as_of", int(time.time())),
                side=getattr(o, "side", "") or "",
                qty=round(qty, 6), price=round(r.avg_fill_price, 6),
                limit_price=getattr(o, "limit_price", None),
                mode=getattr(o, "mode", "taker") or "taker",
                status=status, fees=round(r.fees, 6),
                edge=sig.get("edge"), fair=sig.get("fair"), mid_entry=sig.get("mid"),
                live_mid=sig.get("mid"),
                pnl_realized=None, pnl_mtm=None,
                queue_ahead=getattr(r, "queue_ahead", None),   # maker queue position at submit
                fidelity_tier=r.fidelity_tier, degraded=r.degraded,
                decomp=sig.get("decomp"),
                extras={"lead": sig.get("lead"), "engine": sig.get("engine"),
                        "city": sig.get("city"), "target_date": sig.get("target_date"),
                        "gap": sig.get("gap"), "slippage_bps_vs_mid": r.slippage_bps_vs_mid,
                        "cost_est_c": r.cost_est_c, "est_confidence": r.est_confidence,
                        "code_ref": run_meta.get("code_ref"), "env": run_meta.get("env")})
            self._append_trade(row)
            n += 1
        return n

    def log_run(self, result: BacktestResult, as_of: int,
                run_meta: Optional[dict] = None,
                signals: Optional[Dict[Key, dict]] = None) -> LedgerSnapshot:
        """Accrue the run's aggregates (unchanged) AND — when run_meta+signals are supplied —
        emit one open/resting TradeRow per ExecReport into trades.jsonl."""
        reports: List[ExecReport] = result.exec_reports
        # fees: exact per-strategy from reports. realized: attribute the run's realized
        # P&L by each strategy's share of |d_cash| (single-strategy => full attribution).
        fees_by: Dict[str, float] = {}
        cash_by: Dict[str, float] = {}
        for r in reports:
            fees_by[r.strategy_id] = fees_by.get(r.strategy_id, 0.0) + r.fees
            cash_by[r.strategy_id] = cash_by.get(r.strategy_id, 0.0) + abs(r.d_cash)
        realized_run = result.exec_pnl + result.resolution_pnl
        total_cash = sum(cash_by.values()) or 1.0
        with self._locked():                      # cross-process read-modify-write
            self._reload()
            for sid in set(list(fees_by) + list(cash_by)):
                s = self._strat(sid)
                s["fees"] = round(s["fees"] + fees_by.get(sid, 0.0), 4)
                share = cash_by.get(sid, 0.0) / total_cash
                s["realized"] = round(s["realized"] + realized_run * share, 4)
            self.state["runs_logged"] += 1
            self.state["as_of"] = as_of
            self._persist()
        if run_meta is not None and signals is not None:
            rm = dict(run_meta)
            rm.setdefault("as_of", as_of)
            self._emit_open_rows(reports, rm, signals)
        return self.snapshot()

    def log_settle(self, strategy_id: str, pnl: float, as_of: int, source: str = "GAMMA",
                   run_id: str = "", config_hash: str = "",
                   market_id: Optional[str] = None, token_id: Optional[str] = None,
                   slug: Optional[str] = None, qty: Optional[float] = None,
                   payout: Optional[float] = None, entry_price: Optional[float] = None,
                   extras: Optional[dict] = None) -> None:
        """Resolution settlement of a persisted position (driver settle pass). `source` = the resolution
        provenance (GAMMA | CLOB, from Resolution.source). Gamma/CLOB are REAL settles; there is no proxy
        settle path — a position settles only on an explicit resolution, never a price pin. Counters per
        source let verdict readers confirm the Gamma-settled N.

        When market_id/token_id are supplied it ALSO emits a status=closed TradeRow with pnl_realized +
        the settle source in extras (append-only; existing callers that omit them keep the old behavior)."""
        with self._locked():                      # cross-process read-modify-write
            self._reload()
            s = self._strat(strategy_id)
            s["realized"] = round(s["realized"] + pnl, 4)
            by_src = s.setdefault("settles_by_source", {})
            by_src[source] = by_src.get(source, 0) + 1
            self.state["as_of"] = as_of
            self._persist()
        if market_id is not None and token_id is not None:
            ex = dict(extras or {})
            ex["settle_source"] = source
            slug = _real_slug(slug, market_id)
            row = TradeRow(
                run_id=run_id, source=ex.get("source", "paper"), strategy=strategy_id,
                config_hash=config_hash, order_id=ex.get("order_id", ""),
                market_id=market_id, token_id=token_id,
                slug=slug, url=_slug_url(slug),
                ts=as_of, side=ex.get("side", "") or "",
                qty=round(qty, 6) if qty is not None else 0.0,
                price=round(payout, 6) if payout is not None else 0.0,
                limit_price=None, mode=ex.get("mode", "taker"),
                status="closed", fees=0.0,
                edge=None, fair=None,
                mid_entry=round(entry_price, 6) if entry_price is not None else None,
                live_mid=None, pnl_realized=round(pnl, 6), pnl_mtm=None,
                queue_ahead=None, fidelity_tier=None, degraded=False, decomp=None,
                extras=ex)
            self._append_trade(row)

    def _locked(self):
        """fcntl lock guarding ledger.json read-modify-write across PROCESSES (driver + backtests both
        accrue; unlocked non-atomic writes could interleave/corrupt and stale in-memory state could clobber
        another process's accruals). Usage: with self._locked(): self._reload(); ...mutate...; self._persist()"""
        import contextlib

        @contextlib.contextmanager
        def _cm():
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            lk = open(self.path + ".lock", "w")
            try:
                fcntl.flock(lk, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)
                lk.close()
        return _cm()

    def _reload(self) -> None:
        """Re-read on-disk state under the lock so this process's mutation applies to CURRENT aggregates."""
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self.state = json.load(f)
            except Exception:
                pass

    def _persist(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:                 # atomic replace: a crash mid-write can't corrupt
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.path)

    def snapshot(self) -> LedgerSnapshot:
        return LedgerSnapshot(
            as_of=self.state["as_of"],
            by_strategy=self.state["by_strategy"],
            runs_logged=self.state["runs_logged"])
