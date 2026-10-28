"""live/executor.py — run the SAME orders live [gated].

run(orders, cfg) shares runner.run's signature → backtest and live are interchangeable.
It executes each order over a LiveFillSource (real CLOB) instead of SimFillSource, behind
hard safety gates: LIVE_TRADING_ENABLED=true AND a SCOPED evaluator PASS. Until both hold,
it refuses to run — real money is unlocked only after sim≡reality is proven FOR THE SCOPE
BEING TRADED (a stale taker-only PASS must never unlock a maker strategy in books it was
never tested on).
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import List, Optional

from desk.types import Order, BacktestResult
from desk.sim.engine import execute
from desk.sim.fill_source import LiveFillSource
from desk.sim.portfolio import Portfolio
from desk.live.clob_adapter import ClobAdapter
from desk.runtime.paths import data_path as _data_path
from desk.backtest.runner import RunConfig, _data_quality

# A bare marker file is NOT honored — only the structured scoped PASS.json below. Kept only for the
# error message (so an operator who dropped a bare PASS marker learns why it does nothing).
PASS_FILE = os.path.join(os.path.dirname(__file__), "..", "eval", "recon", "findings", "PASS")
# The STRUCTURED, TRACKED pass. Written by the recon-evaluator on a scoped PASS; committed.
# Shape: {"scope": ["taker"|"maker", ...], "strategies": [strategy_id, ...], "market_class": "<class>",
#         "n_fills": int, "dates": [iso, ...], "granted": iso-date, "expiry": iso-date}
PASS_JSON = os.path.join(os.path.dirname(__file__), "..", "eval", "recon", "PASS.json")
PASS_MAX_AGE_D = 30


def pass_scope() -> Optional[dict]:
    """The current scoped PASS, or None. FAIL-CLOSED: unparseable/expired/stale/missing-freshness ⇒ None.

    Both freshness fields are REQUIRED and must be well-formed dates — a PASS.json that omits (or
    blanks) `granted` or `expiry` has no verifiable validity window, so it can NEVER unlock live.
    (Previously a missing `expiry`/`granted` silently skipped its check, letting an undated PASS through.)
    """
    try:
        with open(PASS_JSON) as f:
            p = json.load(f)
        import calendar as _cal
        today = time.strftime("%Y-%m-%d")
        granted, expiry = p.get("granted"), p.get("expiry")
        if not granted or not expiry:                       # missing/blank ⇒ no validity window ⇒ fail-closed
            return None
        # both must parse as dates (blank strings and garbage raise → caught below → None)
        granted_ts = _cal.timegm(time.strptime(granted, "%Y-%m-%d"))   # UTC, not mktime/local
        time.strptime(expiry, "%Y-%m-%d")                              # validate shape; compared as ISO string
        if today > expiry:
            return None
        if time.time() - granted_ts > PASS_MAX_AGE_D * 86400:
            return None
        if not p.get("scope") or not p.get("strategies"):
            return None
        return p
    except Exception:
        return None


def _assert_gate(orders: List[Order], category_of=None) -> None:
    """Assert every live-trading gate. `category_of` is an injectable market-class resolver
    (market_id -> class); when None, every order is treated as the single default class "default",
    so PASS.json must be scoped to market_class "default"."""
    if os.environ.get("LIVE_TRADING_ENABLED", "false").lower() != "true":
        raise RuntimeError("live disabled: set LIVE_TRADING_ENABLED=true in .env")
    p = pass_scope()
    if p is None:
        marker = " (a bare PASS marker file exists but is not honored — only the structured scoped PASS.json)" \
            if os.path.exists(PASS_FILE) else ""
        raise RuntimeError(
            "live blocked: no valid SCOPED PASS (eval/recon/PASS.json — scope+strategies+expiry, "
            f"fresh ≤{PASS_MAX_AGE_D}d). Run the recon-evaluator for the scope you want to trade; "
            "maker scope additionally requires a maker-scope golden replay." + marker)
    # scope ⊇ the orders being sent: execution CAPABILITY and strategy must both be covered.
    # The capability is what the order CAN DO on the exchange, not its `mode` label: any order that can
    # REST (tif != IOC, or an expiry_ts is set, or mode is maker/marketable) posts a real resting quote
    # and so REQUIRES 'maker' in scope — a mislabeled 'taker' GTC order (Order.tif defaults to GTC) would
    # otherwise slip through a taker-only PASS and rest live. Taker == genuinely IOC-and-immediate only.
    def _needs_maker(o: Order) -> bool:
        return (o.tif != "IOC") or (o.expiry_ts is not None) or ((o.mode or "taker") in ("maker", "marketable"))
    caps = {("maker" if _needs_maker(o) else "taker") for o in orders}
    if not caps <= set(p["scope"]):
        raise RuntimeError(f"live blocked: PASS scope {p['scope']} does not cover order capabilities "
                           f"{sorted(caps)} — an order that can REST on the exchange (tif≠IOC / expiry set / "
                           f"maker|marketable mode) needs a maker-scope PASS, whatever its mode label")
    sids = {o.strategy_id for o in orders}
    if not sids <= set(p["strategies"]):
        raise RuntimeError(f"live blocked: PASS covers strategies {p['strategies']}, got {sorted(sids)}")
    # market_class: PASS.json is stamped for ONE class of book. Orders don't carry a class, so resolve each
    # order's class via the INJECTED resolver and require it match p['market_class'] for EVERY order.
    # Fail-closed: if a resolver was injected and returns None/unknown for ANY order, REFUSE — an
    # unclassifiable order can't be proven in-scope. When NO resolver is injected, every order is the single
    # default class "default", and PASS.json must be scoped to "default" (guards against a mis-scoped PASS).
    want_class = p.get("market_class")
    if not want_class:
        raise RuntimeError("live blocked: PASS.json has no market_class — cannot verify orders are in-class (fail-closed)")
    for o in orders:
        cat = category_of(o.market_id) if category_of is not None else "default"
        if cat is None or cat in ("unknown", "other"):
            raise RuntimeError(f"live blocked: order {o.order_id} market {o.market_id} resolved to class "
                               f"{cat!r} — unclassifiable/out-of-class, cannot prove in-scope (fail-closed)")
        if cat != want_class:
            raise RuntimeError(f"live blocked: order {o.order_id} is class {cat!r} but PASS.json is scoped to "
                               f"market_class {want_class!r} — refusing to trade an out-of-class book")
    # GEO gate (fail-closed): refuse if the bot's egress IP is a restricted/VPN country.
    from desk.live.geo import geo_gate
    g = geo_gate()
    if not g.ok:
        raise RuntimeError(f"live blocked by geo guard: egress {g.place or g.country} — {g.reason}. "
                           "Turn the VPN OFF (or split-tunnel) and re-run `python3 -m desk.live.geo` "
                           "until it shows your allowed location.")


LIVE_MAX_PER_MARKET_USD = float(os.environ.get("LIVE_MAX_PER_MARKET_USD", "5.0"))


# ── MAX_DAILY_USD: the daily spend cap (a real ledger, not a promise) ─────────────────────────────
# Bounds the WORST-CASE $ newly committed per UTC day across every strategy that reaches this
# executor. Persisted under the runtime data root (driver/daily_spend.json, re-rooted by
# DESK_DATA_DIR) so restarts/daemons share one meter; flock'd so driver + exit monitor can't race it.
# Reduce-only orders (a SELL of a held token, ≤ held size) are exempt — the cap must never wedge an
# exit. FAIL-CLOSED everywhere: a malformed cap refuses to trade; an unreadable/corrupt ledger
# refuses to trade (an unprovable spend total never unlocks more spending).


def max_daily_usd() -> Optional[float]:
    """The $/UTC-day spend cap from env MAX_DAILY_USD, or None = cap off (the default when unset,
    empty, 'none' or 'off'). Read PER CALL so launchers/tests that set it before use are honored.
    A set-but-unparseable value RAISES (refuse to trade on a cap we can't read); zero/negative
    clamps to 0.0 = block every non-reduce-only order."""
    raw = (os.environ.get("MAX_DAILY_USD") or "").strip()
    if not raw or raw.lower() in ("none", "off"):
        return None
    try:
        v = float(raw)
    except ValueError:
        raise RuntimeError(f"MAX_DAILY_USD={raw!r} is not a number — refusing live orders (fail-closed)")
    return max(0.0, v)


def _daily_ledger_path() -> str:
    return _data_path("driver", "daily_spend.json")


def _utc_day(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else time.time()))


def daily_spend(day: Optional[str] = None) -> float:
    """$ committed so far for `day` (default: today, UTC) per the ledger. -1.0 = ledger unreadable
    (callers treat that as unprovable → fail-closed)."""
    try:
        if not os.path.exists(_daily_ledger_path()):
            return 0.0
        with open(_daily_ledger_path()) as f:
            led = json.load(f)
        if not isinstance(led, dict):
            return -1.0
        return float(led.get(day or _utc_day(), 0.0))
    except Exception:
        return -1.0


def _reserve_daily_spend(want: float, cap: float) -> tuple:
    """Atomically reserve `want` $ of today's (UTC) budget under `cap`. Returns (ok, spent_before,
    day): ok=False ⇒ REFUSE the order (cap would be exceeded, or the ledger is unreadable/corrupt —
    fail-closed; spent_before=-1.0 marks the unreadable case). The reservation is written BEFORE the
    order is sent, so a crash mid-execution leaves the conservative worst-case booked, never an
    uncounted spend. Entries older than 14 days are pruned."""
    import fcntl
    path = _daily_ledger_path()
    day = _utc_day()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            led: dict = {}
            if os.path.exists(path):
                with open(path) as f:
                    led = json.load(f)                   # corrupt JSON raises → refuse (fail-closed)
            if not isinstance(led, dict):
                raise ValueError("daily spend ledger is not a dict")
            cutoff = _utc_day(time.time() - 14 * 86400)
            led = {k: float(v) for k, v in led.items() if isinstance(k, str) and k >= cutoff}
            spent = float(led.get(day, 0.0))
            if spent + want > cap + 1e-9:
                return False, spent, day
            led[day] = round(spent + want, 6)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(led, f)
            os.replace(tmp, path)
            return True, spent, day
    except Exception:
        return False, -1.0, day


def _settle_daily_spend(reserved: float, actual: float, day: str) -> None:
    """Shrink `day`'s reservation to what ACTUALLY committed (filled notional) — never grows, never
    below 0. Crash/IO-safe by construction: a lost settle leaves the conservative full reservation
    in place (over-counts, never under-counts)."""
    import fcntl
    release = max(0.0, reserved - max(0.0, actual))
    if release <= 0:
        return
    path = _daily_ledger_path()
    try:
        with open(path + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            with open(path) as f:
                led = json.load(f)
            if not isinstance(led, dict):
                return
            led[day] = round(max(0.0, float(led.get(day, 0.0)) - release), 6)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(led, f)
            os.replace(tmp, path)
    except Exception:
        return                                           # reservation stays — conservative


_ACCT_EXP = {"ts": 0.0, "by_mkt": None, "by_event": {}, "local_snap": {}, "local_event_snap": {}}   # 60s cache:
#   exchange exposure (per market + per city-event) + the local snapshots taken when it was (re)cached
_EVENT_SLUG_FNS = None   # memo: tenant event_slug_of hooks (resolved once per process)


def event_slug_of(market_slug: str) -> str:
    """EVENT key from a market slug — what groups the markets that settle on one underlying print, so
    the per-event cap can bind across them. The slug→event mapping is TENANT knowledge (manifest hook
    `event_slug_of`; the weather tenant strips the trailing temperature bucket to match data-api's
    eventSlug). Hooks are chained: the first one that CHANGES the slug wins. GENERIC FALLBACK: the slug
    unchanged — every market is its own event, so the cap still binds per-market (fail-closed, never
    silently pools/bypasses)."""
    global _EVENT_SLUG_FNS
    if _EVENT_SLUG_FNS is None:
        try:
            from desk.signals import registry as _R
            _EVENT_SLUG_FNS = [fn for _t, fn in _R.hooks_for("event_slug_of")]
        except Exception:
            _EVENT_SLUG_FNS = []
    s = market_slug or ""
    for fn in _EVENT_SLUG_FNS:
        try:
            out = fn(s)
        except Exception:
            continue
        if out and out != s:
            return out
    return s


def _local_exposure_by_event() -> dict:
    """Our LIVE positions summed per city-EVENT ($ committed), keyed by event_slug_of(position slug). The
    fail-closed fallback for the city cap when the data-api account read is unavailable — still bounds OUR
    strategies (parallel to _local_exposure_by_market for the per-market cap; manual trades are invisible here)."""
    import glob
    out: dict = {}
    for p in glob.glob(_data_path("driver", "positions_*.json")):
        try:
            for k, pos in json.load(open(p)).items():
                if "|" not in k or pos.get("source") != "live":
                    continue
                slug = pos.get("slug") or ""
                if not slug:
                    continue
                q = float(pos.get("qty") or 0.0); px = float(pos.get("entry_price") or 0.0)
                es = event_slug_of(slug)
                out[es] = out.get(es, 0.0) + (q * px if q > 0 else -q * (1.0 - px))
        except Exception:
            continue
    return out


def _local_exposure_by_market() -> dict:
    """Our LIVE positions summed per market ($ committed: long risks px·qty, short risks (1−px)·qty). FRESH —
    positions_*.json is rewritten the instant a fill books, before the next strategy fires — but it holds
    ONLY our strategies' fills, never the user's manual trades. Paper twins are excluded."""
    import glob
    out: dict = {}
    for p in glob.glob(_data_path("driver", "positions_*.json")):
        try:
            for k, pos in json.load(open(p)).items():
                if "|" not in k or pos.get("source") != "live":
                    continue
                mid = k.split("|", 1)[0]
                q = float(pos.get("qty") or 0.0)
                px = float(pos.get("entry_price") or 0.0)
                out[mid] = out.get(mid, 0.0) + (q * px if q > 0 else -q * (1.0 - px))
        except Exception:
            continue
    return out


def _account_exposure_by_market():
    """$ committed per market across the WHOLE WALLET per the exchange (data-api /positions) — includes the
    user's MANUAL trades + every strategy, not just our logs. Key = conditionId (== our market_id, verified).
    Cached 60s. When we (re)cache the exchange read we also snapshot the LOCAL sum, so the caller can add any
    of our fills booked SINCE this cache (a sibling strategy firing seconds later, before data-api reindexes).
    Returns None if unavailable (caller falls back to the local per-strategy sum)."""
    now = time.time()
    if _ACCT_EXP["by_mkt"] is None or now - _ACCT_EXP["ts"] > 60:
        try:
            import requests
            addr = os.environ.get("POLY_FUNDER_ADDRESS")
            if not addr:                                          # fail-CLOSED: no funder address ->
                _ACCT_EXP["by_mkt"] = None                        # a query with user=None returns [] which would
                return None                                       # read as '$0 held everywhere'. Use local instead.
            pos = requests.get("https://data-api.polymarket.com/positions",
                               params={"user": addr}, timeout=8).json()
            if not isinstance(pos, list):                         # a non-list body (error JSON / rate-limit /
                _ACCT_EXP["by_mkt"] = None                        # indexer hiccup) is NOT an authoritative '$0 held'
                return None                                       # -> don't cache it; fall back to local.
            bm = {}; bev = {}
            for p in pos:
                cond = p.get("conditionId")
                val = abs(float(p.get("initialValue") or 0.0))   # $ committed (premium paid / short leg)
                if cond and val > 0:
                    bm[cond] = bm.get(cond, 0.0) + val
                    es = p.get("eventSlug")                       # per-CITY-EVENT sum (all buckets of one city-day
                    if es:                                        # + every strategy + manual) — the city-cap input
                        bev[es] = bev.get(es, 0.0) + val
            _ACCT_EXP["by_mkt"], _ACCT_EXP["by_event"] = bm, bev
            _ACCT_EXP["local_snap"] = _local_exposure_by_market()
            _ACCT_EXP["local_event_snap"], _ACCT_EXP["ts"] = _local_exposure_by_event(), now
        except Exception:
            _ACCT_EXP["by_mkt"] = None
    return _ACCT_EXP["by_mkt"]


def _wallet_market_exposure(market_id: str) -> float:
    """Worst-case $ committed to this market — a HARD per-market cap input. Uses the REAL account (data-api —
    manual trades + every strategy) PLUS any of our fills booked since that (60s-cached, reindex-lagged) read,
    so two strategies firing seconds apart can't both slip a position past the cap (each sees the other's fill
    via the local delta). No double-count: the delta is only local growth SINCE the cached snapshot. Falls back
    to local-only if the account read is unavailable — a per-strategy budget does not bound per-MARKET wallet
    concentration when several strategies independently stack the same signal."""
    acct = _account_exposure_by_market()
    local = _local_exposure_by_market()
    if acct is not None:
        snap = (_ACCT_EXP.get("local_snap") or {}).get(market_id, 0.0)
        fresh = max(0.0, local.get(market_id, 0.0) - snap)     # our fills booked AFTER the exchange was cached
        return max(acct.get(market_id, 0.0) + fresh, local.get(market_id, 0.0))   # a VALID-but-empty
        # account ([] from an indexer hiccup) must never drop the cap below what we KNOW we hold locally
    return local.get(market_id, 0.0)                           # data-api down: local-only (still bounds our strategies)


def account_event_exposure(event_slug: str) -> float:
    """$ committed across the WHOLE WALLET to this city-EVENT (every bucket + every strategy + manual) — the
    per-city cap input. FAIL-CLOSED (per-market parity): when the data-api account read is unavailable, fall
    back to our LOCAL per-event sum (never a silent 0.0, which would let the city cap not bind). max(account +
    fresh_local_delta, local) so a VALID-but-empty account read can't drop the cap below what we KNOW we hold
    locally, and a sibling strategy's fill booked since the 60s cache is counted via the local delta."""
    acct = _account_exposure_by_market()                       # refreshes the shared cache (by_mkt AND by_event)
    local = _local_exposure_by_event()
    if acct is not None:
        bev = _ACCT_EXP.get("by_event") or {}
        snap = (_ACCT_EXP.get("local_event_snap") or {}).get(event_slug, 0.0)
        fresh = max(0.0, local.get(event_slug, 0.0) - snap)    # our fills booked AFTER the exchange was cached
        return max(bev.get(event_slug, 0.0) + fresh, local.get(event_slug, 0.0))
    return local.get(event_slug, 0.0)                          # data-api down: local-only (still bounds us)


def _held_live_token_qty(market_id: str, token_id: str) -> float:
    """Net LONG shares of this EXACT (market, token) currently held across our live positions — the basis for
    the STRUCTURAL reduce-only check (a SELL up to this size genuinely reduces risk; a mislabeled entry can't
    fake it). Aggregates across arms: both arms may hold the token, and the per-run snapshot bounds total sells
    to the held size (each arm's exit is sized to its own leg, so their sum ≤ aggregate held)."""
    import glob
    tot = 0.0
    key = f"{market_id}|{token_id}"
    for p in glob.glob(_data_path("driver", "positions_*.json")):
        try:
            for k, pos in json.load(open(p)).items():
                if k == key and pos.get("source") == "live":
                    tot += float(pos.get("qty") or 0.0)
        except Exception:
            continue
    return tot


def _empty_report(o: Order, warn: str) -> "ExecReport":
    from desk.types import ExecReport
    return ExecReport(order_id=o.order_id, strategy_id=o.strategy_id, fills=[], filled_qty=0.0,
                      avg_fill_price=0.0, fees=0.0, slippage_bps_vs_mid=None, unfilled_qty=o.qty,
                      d_position=0.0, d_cash=0.0, source="LIVE", fidelity_tier="T0",
                      degraded=True, warnings=[warn])


def run(orders: List[Order], cfg: Optional[RunConfig] = None, category_of=None) -> BacktestResult:
    """Same signature as backtest.runner.run — but real fills via the live CLOB. `category_of` is the
    injectable market-class resolver used for the PASS market_class scope check (None → single default
    class "default")."""
    _assert_gate(orders, category_of=category_of)
    daily_cap = max_daily_usd()        # raises on a malformed value — the whole run is refused (fail-closed)
    cfg = cfg or RunConfig()
    adapter = ClobAdapter()
    adapter.sync_balance_allowance()   # refresh the CLOB's server-side balance cache first — else orders
                                       # can reject 'not enough balance: balance: 0' despite on-chain funds
    src = LiveFillSource(adapter)
    pf = Portfolio(cfg.starting_cash)
    reports = []
    equity = []
    halted = None
    for o in sorted(orders, key=lambda x: x.submit_ts):
        if halted:
            reports.append(_empty_report(o, f"skipped: {halted}"))
            continue
        want = o.qty * (o.limit_price if o.side == "BUY" else 1.0 - o.limit_price)   # side-correct risk:
        # a BUY risks the premium qty*price; a SELL/short posts the qty*(1-price) leg — a symmetric max()
        # bound would over-reject a converted BUY-NO that shorts a high-priced YES.
        # STRUCTURAL reduce-only exemption (NOT a name tag): a mislabeled entry called "*-exit-*" must never
        # bypass the concentration cap. An order genuinely reduces risk iff it SELLS a token we already hold,
        # up to the held size. Polymarket holdings are positive token counts and you can only sell what you
        # hold, so a BUY always increases exposure; a SELL of ≤ held is reduce-only.
        held_tok = _held_live_token_qty(o.market_id, o.token_id)
        is_reduce_only = o.side == "SELL" and held_tok > 0 and o.qty <= held_tok + 1e-9
        held = _wallet_market_exposure(o.market_id)  # reduce-only orders are NEVER blocked by the cap
        if not is_reduce_only and held + want > LIVE_MAX_PER_MARKET_USD:
            reports.append(_empty_report(
                o, f"refused: wallet already holds ${held:.2f} worst-case in this market; "
                   f"+${want:.2f} would exceed LIVE_MAX_PER_MARKET_USD ${LIVE_MAX_PER_MARKET_USD} "
                   f"(cross-arm concentration cap)"))
            continue
        # DAILY spend cap (MAX_DAILY_USD): reserve the worst-case commitment BEFORE sending; shrink to
        # the actual filled notional after. Reduce-only exits are exempt (never wedge an exit).
        day_key = None
        if daily_cap is not None and not is_reduce_only:
            ok_day, spent_day, day_key = _reserve_daily_spend(want, daily_cap)
            if not ok_day:
                why = ("daily spend ledger unreadable/corrupt — fail-closed"
                       if spent_day < 0 else f"${spent_day:.2f} already committed today (UTC)")
                reports.append(_empty_report(
                    o, f"refused: +${want:.2f} would exceed MAX_DAILY_USD ${daily_cap} — {why}"))
                continue
        try:
            report = execute(o, src)        # identical engine path; source="LIVE"
        except Exception as ex:
            # NEVER let a rejected order discard fills that already EXECUTED on-exchange earlier in
            # this pass: a balance-400 on order N crashing the pass would leave order N-1's real fill
            # unbooked → an invisible position that the strategy re-buys on the next fire.
            reports.append(_empty_report(o, f"order failed: {ex!r}"))
            if "not enough balance" in str(ex).lower():
                halted = "wallet balance exhausted earlier in this pass"
            continue                       # NOTE: the daily reservation deliberately STAYS booked here —
            # the order may have partially executed on-exchange before the exception (conservative).
        if day_key is not None:            # shrink the worst-case reservation to the actual commitment
            actual = report.filled_qty * (report.avg_fill_price if o.side == "BUY"
                                          else max(0.0, 1.0 - report.avg_fill_price))
            _settle_daily_spend(want, actual, day_key)
        pf.apply(o, report)
        reports.append(report)
        equity.append([o.submit_ts, round(pf.equity(), 4)])
    requested = sum(o.qty for o in orders) or 1.0
    return BacktestResult(
        equity_curve=equity,
        total_pnl=round(pf.total_pnl(), 4),
        exec_pnl=round(pf.exec_pnl, 4),
        resolution_pnl=round(pf.resolution_pnl, 4),
        fill_ratio=round(sum(r.filled_qty for r in reports) / requested, 4),
        exec_reports=reports,
        data_quality=_data_quality(reports),
    )
