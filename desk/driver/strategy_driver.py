"""driver/strategy_driver.py — fire enabled strategies on schedule.

ENTRYPOINT: `python3 -m desk.driver.strategy_driver [--once]`. Mirrors capture.py: a loop with
a heartbeat + graceful shutdown; the process supervisor restarts it. A strategy fires either at fixed
release times (schedule.times_utc) or on an interval, per its schedule.

Each fire: generate_orders (position-aware, reads a persisted per-strategy position side-file) →
runner.run (paper) OR executor.run (live: LIVE_TRADING_ENABLED + PASS) → pnl_ledger.log_run →
update+persist positions. Backtest uses the SAME generate_orders.
"""
from __future__ import annotations

import argparse
import calendar
import inspect
import json
import os
import signal
import time
from typing import Dict, List

from desk.types import Order, ExecReport
from desk.data.store import Store
from desk.data.sources import Sources
from desk.driver.poslock import positions_lock
from desk.runtime.paths import data_path
from desk.signals.registry import load_registry, config_path
from desk.backtest.runner import run as paper_run, RunConfig
from desk.tracker.pnl_ledger import PnLLedger

# positions/resting/live_orders/_heartbeat side-files: env-resolved (DESK_DATA_DIR) with the
# historical in-package desk/driver/ location as the default target.
DRIVER_DIR = data_path("driver")
FIRE_DEADLINE_S = 1200          # a hung fire self-kills (SIGALRM) — the supervisor relaunches; no zombies
from desk.runtime.guard import DECLARED_FLAGS   # env whitelist logged per fire (contamination audit)
_stop = False

# Paper-lane fee/cost resolvers, loaded ONCE. Wire the SAME market→category resolver + exec-cost model
# the live/backtest paths use so paper ≡ backtest costs (base band); without them the paper path charges
# the default fee for every fill and fills T1-fallback orders at an optimistic mid.
_PAPER_RESOLVERS = None            # (category_of, cost_model) memo; (None, None) if metadata absent


def _paper_resolvers():
    """(category_of, cost_model) for the paper RunConfig, loaded once.
    category_of = the market→category resolver that drives the per-category fee (single default class here).
    cost_model = the ExecCostModel over spread_model.json (base band: median spread + p75 temporal — same as
    the backtests), so paper T1-fallback fills carry base-band spread+slippage instead of an optimistic mid.
    Fail-soft: any load failure ⇒ (None, None), i.e. the naive default — paper never crashes on missing
    metadata."""
    global _PAPER_RESOLVERS
    if _PAPER_RESOLVERS is None:
        try:
            from desk.sim.spread_model import ExecCostModel
            # tenant-provided market resolvers (manifest hook `resolvers`), composed by the registry:
            # (None, None) on a desk-only tree — the single default market class.
            from desk.signals.registry import market_resolvers
            peer_key, category_of = market_resolvers()
            cost_model = ExecCostModel.load(data_path("sim", "spread_model.json"),
                                            peer_key=peer_key, category_of=category_of)
            _PAPER_RESOLVERS = (category_of, cost_model)
        except Exception:
            _PAPER_RESOLVERS = (None, None)
    return _PAPER_RESOLVERS


def _on_signal(*_a):
    global _stop
    _stop = True


class _FireTimeout(Exception):
    """Per-fire SIGALRM deadline hit — raised so ONE slow fire is SKIPPED, not fatal.
    Without a handler, the deadline default-TERMINATES the whole process; the supervisor relaunches,
    the schedule resets, and the FIRST (heaviest) strategy re-fires and re-hangs — one slow strategy
    can starve every other indefinitely while the process crash-loops. Raising instead lets the
    per-fire try/except advance the schedule and continue to the next strategy; the process stays
    alive so settlement and reconciliation keep running. Live fires still run with the alarm
    disarmed (a kill between the exchange POST and local persistence would drop real exposure)."""


def _on_alarm(*_a):
    raise _FireTimeout("fire exceeded FIRE_DEADLINE_S")


def _positions_path(name: str) -> str:
    return os.path.join(DRIVER_DIR, f"positions_{name}.json")


def _load_positions(name: str) -> Dict[tuple, dict]:
    try:
        with open(_positions_path(name)) as f:
            return {tuple(k.split("|", 1)): v for k, v in json.load(f).items()}
    except Exception:
        return {}


def _save_positions(name: str, positions: Dict[tuple, dict]) -> None:
    obj = {f"{m}|{tk}": v for (m, tk), v in positions.items()}
    os.makedirs(DRIVER_DIR, exist_ok=True)      # a relocated DESK_DATA_DIR target may not exist yet
    tmp = _positions_path(name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, _positions_path(name))


def _resting_path(name: str) -> str:
    return os.path.join(DRIVER_DIR, f"resting_{name}.json")


def _load_resting(name: str) -> List[dict]:
    try:
        with open(_resting_path(name)) as f:
            return json.load(f)
    except Exception:
        return []


def _save_resting(name: str, rows: List[dict]) -> None:
    os.makedirs(DRIVER_DIR, exist_ok=True)
    tmp = _resting_path(name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f)
    os.replace(tmp, _resting_path(name))


# ── live-order tracking for reconciliation ───────────────────────────────────
# A LIVE fire records every posted order's EXCHANGE id + metadata here so a LATER cycle can re-poll it for
# fills the immediate post-response missed and cancel+drop maker quotes past expiry. PAPER never writes this
# file — paper fills are all-or-nothing in one engine call, nothing to reconcile.
def _live_orders_path(name: str) -> str:
    return os.path.join(DRIVER_DIR, f"live_orders_{name}.json")


def _load_live_orders(name: str) -> List[dict]:
    try:
        with open(_live_orders_path(name)) as f:
            return json.load(f)
    except Exception:
        return []


def _save_live_orders(name: str, rows: List[dict]) -> None:
    os.makedirs(DRIVER_DIR, exist_ok=True)
    tmp = _live_orders_path(name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f)
    os.replace(tmp, _live_orders_path(name))


def _record_live_orders(name: str, orders: List[Order], reports, signals: Dict[tuple, dict],
                        config_hash: str, now: int) -> None:
    """Persist each LIVE order that has an exchange id and could still fill later — any resting/marketable
    order (tif≠IOC or an expiry is set) whose booked qty is below the order qty. IOC/taker orders that fully
    resolved in the post-response are NOT tracked (nothing left to reconcile). Booked qty = fills already
    counted this fire, so reconciliation appends only the NEW tail. Merges with existing open rows (a quote
    persisted last fire stays until MATCHED/CANCELED/expired)."""
    by_id = {r.order_id: r for r in reports}
    existing = {r["exchange_order_id"]: r for r in _load_live_orders(name) if r.get("exchange_order_id")}
    for o in orders:
        rep = by_id.get(o.order_id)
        xid = getattr(rep, "exchange_order_id", None) if rep is not None else None
        if not xid:
            continue                                   # no exchange id -> can't reconcile (nothing posted / IOC no-fill)
        booked = rep.filled_qty if rep is not None else 0.0
        can_rest = (o.tif != "IOC") or (o.expiry_ts is not None) or ((o.mode or "taker") in ("maker", "marketable"))
        if not can_rest and booked >= o.qty - 1e-9:
            continue                                   # taker order fully done in-fire -> nothing to poll later
        existing[str(xid)] = {
            "exchange_order_id": str(xid), "strategy_id": o.strategy_id,
            "market_id": o.market_id, "token_id": o.token_id, "side": o.side,
            "mode": o.mode or "taker", "submit_ts": int(o.submit_ts),
            "expiry_ts": int(o.expiry_ts) if o.expiry_ts else None,
            "limit_price": float(o.limit_price), "qty": float(o.qty),
            "booked_qty": round(float(booked), 6), "config_hash": config_hash,
            "slug": signals.get((o.market_id, o.token_id), {}).get("slug")}
    _save_live_orders(name, list(existing.values()))


def _revive_resting(name: str, positions: Dict[tuple, dict], now: int):
    """Persisted UNFILLED maker quotes from prior fires, re-submitted with their ORIGINAL submit_ts so the
    engine's maker model judges real prints over [submit, min(now, expiry)] — the window that was entirely in
    the future at placement time. Without this, a maker quote would live exactly one fire and forward maker
    fill-rate evidence could never accumulate.
    Returns (orders, signals): the quote's signal SNAPSHOT (fair/edge/decomp/slug, captured at placement)
    rides along so a later fill's TradeRow still joins pricing context.
    PAPER-ONLY: the caller must never execute revived orders through the LIVE executor — re-POSTing a still-
    resting exchange order every fire would stack real exposure unboundedly; live maker needs exchange
    open-order reconciliation, which does not exist yet.
    Requote/cancel semantics: a resting quote is NOT repriced while alive; it expires after maker_validity_s
    and the NEXT fire quotes fresh."""
    out, sigs = [], {}
    for r in _load_resting(name):
        if now >= int(r.get("expiry_ts") or 0):
            continue                              # expired -> reserved capital releases (row simply dropped)
        key = (r["market_id"], r["token_id"])
        if positions.get(key, {}).get("qty"):
            continue                              # position opened meanwhile (paper fills all-or-nothing;
                                                  # live partial-remainder handling = the reconciliation TODO)
        out.append(Order(order_id=r["order_id"], strategy_id=r["strategy_id"],
                         market_id=r["market_id"], token_id=r["token_id"], side=r["side"],
                         submit_ts=int(r["submit_ts"]), limit_price=float(r["limit_price"]),
                         qty=float(r["qty"]), tif="GTC", expiry_ts=int(r["expiry_ts"]), mode="maker"))
        if r.get("signal"):
            sigs[key] = r["signal"]
    return out, sigs


def _update_resting(name: str, orders: List[Order], reports, now: int,
                    signals: Dict[tuple, dict]) -> int:
    """Persist maker orders still (partially) unfilled and unexpired for the next fire's revive pass."""
    by_id = {r.order_id: r for r in reports}
    rows = []
    for o in orders:
        if (o.mode or "taker") != "maker" or not o.expiry_ts or now >= o.expiry_ts:
            continue
        rep = by_id.get(o.order_id)
        left = rep.unfilled_qty if rep is not None else o.qty
        if left and left > 1e-9:
            rows.append({"order_id": o.order_id, "strategy_id": o.strategy_id,
                         "market_id": o.market_id, "token_id": o.token_id, "side": o.side,
                         "submit_ts": o.submit_ts, "limit_price": o.limit_price,
                         "qty": round(left, 6), "expiry_ts": o.expiry_ts,
                         "signal": signals.get((o.market_id, o.token_id))})
    _save_resting(name, rows)
    return len(rows)


def _gate_fn(params: dict):
    from desk.signals.gate import threshold_gate, llm_gate
    return llm_gate if params.get("gate") == "llm" else threshold_gate


def _config_hash(spec) -> str:
    """The spec's SEALED registration hash, injected by registry.load_registry as params['_config_hash'].
    Never recomputed here — a local recompute (over runtime-merged params) can never match the registered
    row hash, which would silently break the TradeRow→registration provenance chain. Empty string =
    an unregistered builtin (no config row)."""
    return (spec.params or {}).get("_config_hash", "")


from desk.runtime.guard import code_ref as _code_ref, declared_env as _declared_env


def _stamp_positions(positions: Dict[tuple, dict], orders: List[Order],
                     signals: Dict[tuple, dict], config_hash: str, source: str) -> None:
    """Stamp slug + config_hash + SOURCE onto positions at trade time so the settle TradeRow (emitted much
    later, when the market resolves and the signal/decomp is long gone) carries the OPEN-time provenance —
    a live fill settled after LIVE_TRADING_ENABLED flips off must still be attributed live, and vice versa."""
    for o in orders:
        key = (o.market_id, o.token_id)
        sig = signals.get(key, {})
        pos = positions.get(key)
        if pos is not None:
            pos.setdefault("slug", sig.get("slug"))
            pos.setdefault("source", source)
            pos["config_hash"] = config_hash


def _live_enabled() -> bool:
    from desk.live import executor
    return (os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"
            and executor.pass_scope() is not None)   # scoped PASS; bare marker no longer honored


def _update_positions(positions: Dict[tuple, dict], orders: List[Order],
                      reports: List[ExecReport], signals: Dict[tuple, dict], t: int) -> List[dict]:
    """Net fills into persisted positions. Returns REALIZED-EXIT rows: each fire runs a fresh per-run
    Portfolio, so a closing fill would otherwise enter that portfolio as a brand-new position and its
    realized PnL never reach the ledger. Entry fees accrue on the position (settle nets them), and
    same-direction top-ups AVERAGE entry_price."""
    exits: List[dict] = []
    by_id = {o.order_id: o for o in orders}
    for rep in reports:
        o = by_id.get(rep.order_id)
        if o is None or rep.filled_qty <= 0:
            continue
        key = (o.market_id, o.token_id)
        pos = positions.get(key, {"qty": 0.0, "entry_price": 0.0, "entry_edge": 0.0,
                                  "entry_ts": t, "fee_paid": 0.0})
        signed = rep.filled_qty if o.side == "BUY" else -rep.filled_qty
        old = pos["qty"]
        if old == 0:
            sig = signals.get(key, {})
            pos.update(entry_price=rep.avg_fill_price, entry_edge=sig.get("edge", 0.0), entry_ts=t,
                       fee_paid=rep.fees, order_id=o.order_id)
        elif (old > 0) == (signed > 0):                       # top-up: average the entry
            tot = abs(old) + rep.filled_qty
            pos["entry_price"] = round((abs(old) * pos["entry_price"]
                                        + rep.filled_qty * rep.avg_fill_price) / tot, 6)
            pos["fee_paid"] = round(pos.get("fee_paid", 0.0) + rep.fees, 6)
        else:                                                 # reduces/closes/flips: realize the closed part
            closed = min(abs(old), rep.filled_qty)
            direction = 1.0 if old > 0 else -1.0
            entry_fee_share = pos.get("fee_paid", 0.0) * (closed / abs(old)) if abs(old) > 1e-12 else 0.0
            exit_fee_share = rep.fees * (closed / rep.filled_qty)
            pnl = round(direction * closed * (rep.avg_fill_price - pos["entry_price"])
                        - entry_fee_share - exit_fee_share, 4)
            exits.append({"market_id": o.market_id, "token_id": o.token_id, "qty": direction * closed,
                          "entry_price": pos["entry_price"], "exit_price": rep.avg_fill_price,
                          "pnl": pnl, "slug": pos.get("slug"), "config_hash": pos.get("config_hash", ""),
                          "source": pos.get("source"), "order_id": pos.get("order_id", ""),
                          "entry_ts": pos.get("entry_ts")})
            pos["fee_paid"] = round(pos.get("fee_paid", 0.0) - entry_fee_share, 6)
            if rep.filled_qty > abs(old) + 1e-12:             # flip: remainder opens the other way
                pos.update(entry_price=rep.avg_fill_price, entry_ts=t,
                           fee_paid=rep.fees - exit_fee_share)
        pos["qty"] = round(old + signed, 6)
        if abs(pos["qty"]) < 1e-9:
            positions.pop(key, None)
        else:
            positions[key] = pos
    return exits


_SETTLE_BACKOFF: Dict[tuple, float] = {}   # (m,tk) -> retry-after ts (negative-result memo)


MARKS_PATH = data_path("tracker", "marks.json")


def _write_marks(open_keys: List[tuple]) -> None:
    """Persist live MTM marks for still-open positions so the DASHBOARD render stays pure (no network):
    the driver, which already runs on live rails, fetches the exit-side quote and writes marks.json; the
    adapter computes each ROW's unrealized from the quotes.

    ONE book per unique (market,token) — positions across strategies share the quote (a per-position fetch
    would be slower AND wrong when two lanes hold different entries on the same market); CHUNKED flushes so
    a SIGALRM mid-pass keeps partial progress; prior marks carried forward on fetch failure (stale-with-age
    beats blank)."""
    from desk.data.market_feed import clob_book

    def _flush(mk: dict) -> None:
        try:
            os.makedirs(os.path.dirname(MARKS_PATH) or ".", exist_ok=True)
            tmp = MARKS_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(mk, f)
            os.replace(tmp, MARKS_PATH)
        except Exception:
            pass

    try:
        marks = json.load(open(MARKS_PATH))
    except Exception:
        marks = {}
    open_set = {f"{m}|{tk}" for (m, tk, *_r) in open_keys}
    marks = {k: v for k, v in marks.items() if k in open_set}   # closed positions drop off
    done = set()
    since_flush = 0
    for (m, tk, side, entry, qty) in open_keys:
        key = f"{m}|{tk}"
        if key in done:
            continue
        done.add(key)
        try:
            b = clob_book(tk)
        except Exception:
            b = None
        if not b or not b.get("bids") or not b.get("asks"):
            continue                                   # prior mark (if any) carries forward, ts shows age
        bid, ask = b["bids"][0][0], b["asks"][0][0]
        # legacy pnl_mtm kept for back-compat (computed for THIS position); the adapter now prefers
        # bid/ask and computes each row's own unrealized from its own entry/qty/side
        exit_px = bid if side == "BUY" else ask
        mtm = abs(qty) * ((exit_px - entry) if side == "BUY" else (entry - exit_px))
        marks[key] = {"bid": bid, "ask": ask, "live_mid": b.get("mid"),
                      "pnl_mtm": round(mtm, 4), "exit_px": exit_px,
                      "basis": "exit at live touch − taker fee (0 bps observed)",
                      "ts": int(time.time())}
        since_flush += 1
        if since_flush >= 25:
            _flush(marks)
            since_flush = 0
    _flush(marks)


def settle_positions(store: Store, ledger: PnLLedger, names: List[str]) -> dict:
    """Resolution-settle every persisted open position (the shared runner/ledger settle path). GAMMA first,
    CLOB fallback (Sources.resolution chain), each settle tagged with its provenance in the ledger. NO
    price-pin heuristic: an unresolved market simply stays open.
    INVARIANT: pnl = qty*(payout - entry_price) MUST stay formula-identical to the backtest's resolution
    settle (runner mark_mode='resolution' -> portfolio) — change BOTH or backtest≢live silently."""
    out = {"settled": 0, "by_source": {}}
    now = time.time()
    _still_open: List[tuple] = []
    # Settle EVERY persisted position file, not just currently-registered specs — a de-registered spec's
    # open position is REAL exchange state and must still resolve to a closed TradeRow.
    import glob as _glob
    on_disk = [os.path.basename(p)[len("positions_"):-len(".json")]
               for p in _glob.glob(os.path.join(DRIVER_DIR, "positions_*.json"))]
    for name in sorted(set(names) | set(on_disk)):
      # positions read-modify-write under the shared per-arm lock (same lock file the exit monitor
      # holds): an exit-close landing mid-settle must not be overwritten by this pass's stale copy.
      with positions_lock(name):
        positions = _load_positions(name)
        changed = False
        for key in list(positions.keys()):
            m, tk = key
            pos = positions[key]
            q = float(pos.get("qty") or 0.0)
            if abs(q) < 1e-9:
                positions.pop(key, None); changed = True
                continue
            if now < _SETTLE_BACKOFF.get(key, 0):
                _still_open.append((m, tk, "BUY" if q > 0 else "SELL",
                                    float(pos.get("entry_price") or 0.0), q))
                continue                          # recently confirmed unresolved — don't re-hit Gamma/CLOB
                                                  # (but it still gets a live MTM mark above)
            try:
                res = store.resolution(m, tk)
            except Exception:
                res = None
            if res is None:
                _SETTLE_BACKOFF[key] = now + 4 * 3600.0
                _still_open.append((m, tk, "BUY" if q > 0 else "SELL",
                                    float(pos.get("entry_price") or 0.0), q))
                continue
            entry_px = float(pos.get("entry_price") or 0.0)
            # NET of the persisted entry fee (maker fee is 0).
            pnl = round(q * (res.payout - entry_px) - float(pos.get("fee_paid") or 0.0), 4)
            # ledger aggregate + a status=closed TradeRow (market/token → the dashboard's settle event).
            ledger.log_settle(name, pnl, as_of=int(time.time()), source=res.source or "GAMMA",
                              config_hash=str(pos.get("config_hash") or ""),
                              market_id=m, token_id=tk, slug=pos.get("slug"),
                              qty=q, payout=res.payout, entry_price=entry_px,
                              extras={"source": pos.get("source") or ("live" if _live_enabled() else "paper"),
                                      "side": "BUY" if q > 0 else "SELL",
                                      "order_id": str(pos.get("order_id") or ""),
                                      "entry_ts": pos.get("entry_ts")})
            out["settled"] += 1
            out["by_source"][res.source] = out["by_source"].get(res.source, 0) + 1
            positions.pop(key, None); changed = True
        if changed:
            _save_positions(name, positions)
    _write_marks(_still_open)
    return out


RECON_MAX_ORDERS_PER_CYCLE = 25    # bound the reconciliation poll (one /trades + /order HTTP call each)


def reconcile_live_orders(store: Store, ledger: PnLLedger, names: List[str],
                          max_orders: int = RECON_MAX_ORDERS_PER_CYCLE) -> dict:
    """LIVE fill reconciliation + maker expiry sweep.

    Fill reconciliation — the live path only ever records the ONE `get_trades` snapshot taken the instant an
    order posts; a GTC/maker order (or a marketable limit that partially crossed) keeps filling afterwards and
    those fills would be LOST. Here every tracked live order is re-polled; fills booked beyond what the fire
    already counted are appended as TradeRows (ledger.log_run) and netted into the persisted position — the
    SAME engine-shaped ExecReport path a fire uses, so a reconciled fill is indistinguishable from an at-fire
    fill downstream.

    Expiry sweep — a live MAKER order past its expiry_ts is CANCELLED on the exchange (adapter.cancel) and its
    tracking row dropped, so a resting quote can't live forever (paper's expires via _revive_resting; live has
    no other path).

    Runs whenever TRACKED live orders exist and creds are available — NOT gated on the live-enabled
    flag: disarming live (or a PASS expiring) must not abandon real resting exchange orders
    unpolled/unbooked/uncancelled. ORDER PLACEMENT stays gated (fire_strategy routes to paper; the
    executor and adapter refuse); polling is read-only, and the expiry-sweep cancel keeps its own
    LIVE_TRADING_ENABLED gate inside ClobAdapter.cancel (a disarmed bot's cancel is refused there,
    counted as an error, and retried next cycle). If the adapter can't connect (creds unset) it
    raises, we catch per-order and leave the row for next cycle — nothing is booked, nothing
    crashes. Bounded to `max_orders` polls per cycle."""
    from desk.types import ExecReport, BacktestResult
    out = {"reconciled_fills": 0, "expired_cancelled": 0, "polled": 0, "errors": 0}
    live_now = _live_enabled() and os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"
    if not live_now and not (os.environ.get("POLY_PRIVATE_KEY") and os.environ.get("POLY_FUNDER_ADDRESS")):
        return out                                     # disarmed AND no creds -> cannot poll the exchange
    import glob as _glob
    on_disk = [os.path.basename(p)[len("live_orders_"):-len(".json")]
               for p in _glob.glob(os.path.join(DRIVER_DIR, "live_orders_*.json"))]
    adapter = None
    now = int(time.time())
    budget = max_orders
    for name in sorted(set(names) | set(on_disk)):
      # live_orders + positions read-modify-write under the shared per-arm lock — the exit monitor
      # writes BOTH files (its exits record via _record_live_orders) from its own process.
      with positions_lock(name):
        rows = _load_live_orders(name)
        if not rows:
            continue
        keep: List[dict] = []
        new_reports: List[ExecReport] = []
        new_orders: List[Order] = []
        new_signals: Dict[tuple, dict] = {}
        changed = False
        for r in rows:
            xid = r.get("exchange_order_id")
            if not xid or budget <= 0:
                keep.append(r)                         # out of budget this cycle -> revisit next
                continue
            if adapter is None:
                try:
                    from desk.live.clob_adapter import ClobAdapter
                    adapter = ClobAdapter()
                except Exception:
                    return out                         # adapter unavailable -> leave every row, no-op
            budget -= 1
            out["polled"] += 1
            try:
                of = adapter.poll_fills_for_order(xid, token_id=r.get("token_id", ""),
                                                  known_qty=float(r.get("booked_qty") or 0.0))
            except Exception:
                out["errors"] += 1
                keep.append(r)                         # transient (creds/network) -> retry next cycle
                continue
            booked = float(r.get("booked_qty") or 0.0)
            total = of.filled_qty
            new_qty = round(total - booked, 6)
            if new_qty > 1e-9:                          # fills the fire missed -> book the tail
                # weight the NEW tail's avg price/fee by the fills that make it up (the exchange returns ALL
                # fills of the order; the tail = total − already-booked, priced at the newest fills' avg).
                tail_fills = _tail_fills(of.fills, booked)
                filled = round(sum(f.qty for f in tail_fills), 6)
                notional = sum(f.price * f.qty for f in tail_fills)
                fees = round(sum(f.fee for f in tail_fills), 6)
                avg = round(notional / filled, 6) if filled > 1e-9 else 0.0
                oid = f"{r.get('strategy_id', name)}-recon-{xid[:10]}-{now}"
                o = Order(order_id=oid, strategy_id=r.get("strategy_id", name),
                          market_id=r["market_id"], token_id=r["token_id"], side=r["side"],
                          submit_ts=int(r.get("submit_ts") or now), limit_price=float(r.get("limit_price") or 0.0),
                          qty=filled, tif="GTC", mode=r.get("mode", "maker"))
                # slippage_bps_vs_mid=None: the send-time book mid of a fill that landed AFTER the fire is
                # unknowable at reconciliation, so we report unmeasurable, not a fake 0.0.
                # fidelity_tier="LIVE": these are real exchange fills, not a sim ladder-walk.
                rep = ExecReport(order_id=oid, strategy_id=o.strategy_id, fills=tail_fills,
                                 filled_qty=filled, avg_fill_price=avg, fees=fees, slippage_bps_vs_mid=None,
                                 unfilled_qty=0.0,
                                 d_position=(filled if r["side"] == "BUY" else -filled),
                                 d_cash=(-(notional + fees) if r["side"] == "BUY" else (notional - fees)),
                                 source="LIVE", fidelity_tier="LIVE", degraded=of.degraded,
                                 warnings=["reconciled fill; send-time mid unknowable — slippage unmeasurable"]
                                 + of.warnings,
                                 exchange_order_id=str(xid))
                new_orders.append(o)
                new_reports.append(rep)
                new_signals[(r["market_id"], r["token_id"])] = {"slug": r.get("slug")}
                r["booked_qty"] = round(total, 6)
                out["reconciled_fills"] += 1
                changed = True
            done = (of.status in ("MATCHED", "CANCELED", "CANCELLED")
                    or float(r.get("booked_qty") or 0.0) >= float(r.get("qty") or 0.0) - 1e-9)
            expired = bool(r.get("expiry_ts")) and now >= int(r["expiry_ts"]) and not done
            if expired:                                # maker quote past expiry -> cancel + drop
                if (r.get("mode") or "taker") in ("maker", "marketable"):
                    try:
                        adapter.cancel(xid)
                        out["expired_cancelled"] += 1
                    except Exception:
                        out["errors"] += 1
                        keep.append(r)                 # cancel failed -> keep tracking, retry next cycle
                        changed = True
                        continue
                done = True
            if done:
                changed = True
                continue                               # retired: not kept
            keep.append(r)
        if new_reports:                                # emit TradeRows + net into positions
            positions = _load_positions(name)          # loaded HERE, under the lock, right before the
            t = now                                    # read-modify-write — never a stale pre-poll copy
            run_meta = {"run_id": f"{name}-recon-{t}", "source": "live", "strategy": name,
                        "config_hash": rows[0].get("config_hash", "") if rows else "",
                        "orders_by_id": {o.order_id: o for o in new_orders},
                        "code_ref": _code_ref(), "env": _declared_env()}
            result = BacktestResult(exec_reports=new_reports, exec_pnl=0.0, resolution_pnl=0.0)
            ledger.log_run(result, as_of=t, run_meta=run_meta, signals=new_signals)
            exits = _update_positions(positions, new_orders, new_reports, new_signals, t)
            for x in exits:
                ledger.log_settle(name, x["pnl"], as_of=t, source="EXIT", run_id=run_meta["run_id"],
                                  config_hash=x["config_hash"] or run_meta["config_hash"],
                                  market_id=x["market_id"], token_id=x["token_id"], slug=x.get("slug"),
                                  qty=x["qty"], payout=x["exit_price"], entry_price=x["entry_price"],
                                  extras={"source": "live", "kind": "exit",
                                          "order_id": str(x.get("order_id") or ""),
                                          "entry_ts": x.get("entry_ts")})
            _stamp_positions(positions, new_orders, new_signals, run_meta["config_hash"], "live")
            _save_positions(name, positions)
        if changed:
            _save_live_orders(name, keep)
    return out


def _tail_fills(fills: List["Fill"], booked_qty: float) -> List["Fill"]:  # noqa: F821
    """The fills making up the NEW (unbooked) tail: the exchange returns ALL fills of an order, but we've
    already booked `booked_qty`. Skip fills (oldest-first) until `booked_qty` is consumed, then return the
    rest — so a fill straddling the boundary contributes only its unbooked remainder."""
    tail, skip = [], booked_qty
    for f in sorted(fills, key=lambda x: x.ts):
        if skip <= 1e-9:
            tail.append(f)
            continue
        if f.qty <= skip + 1e-9:
            skip -= f.qty
            continue
        # partial: this fill spans the booked boundary -> keep only the unbooked remainder
        from desk.types import Fill as _Fill
        tail.append(_Fill(ts=f.ts, price=f.price, qty=round(f.qty - skip, 6), fee=round(f.fee * (f.qty - skip) / f.qty, 6)))
        skip = 0.0
    return tail


def _convert_shorts_to_buy_no(orders: List[Order], signals: Dict[tuple, dict], params: dict) -> List[Order]:
    """LIVE-only negRisk short expression. You CANNOT sell a YES token you don't hold, so a short-to-open
    SELL-YES is rewritten to BUY the complementary NO token @ (1-limit) — same edge/payoff — and RE-SIZED to
    ~min_notional collateral at the NO price (a short risks ~$1 like a BUY, not the qty*(1-price) blob).
    Done HERE (driver level, BEFORE executor.run + _update_positions) so the driver's orders, position
    tracking, ledger join AND reconciliation all record the executed BUY-NO leg — an adapter-level rewrite
    is invisible to position tracking and would book "short YES @ NO-price", settling wrong. A SELL that
    CLOSES a held long is left as-is. Fail-soft per order: any lookup failure => the SELL is left unchanged
    (fails cleanly at the exchange, never mis-recorded). Signals re-keyed to the NO token so the converted
    fill's TradeRow still joins fair/edge/decomp/slug."""
    from dataclasses import replace
    if not any(o.side == "SELL" for o in orders):
        return orders
    from desk.live.clob_adapter import ClobAdapter
    min_notional = float((params or {}).get("min_notional", 0.0))
    budget = (params or {}).get("budget")               # arm collateral cap; None = uncapped
    committed = 0.0                                     # running converted BUY-NO collateral this fire
    adapter = None
    out: List[Order] = []
    for o in orders:
        if o.side != "SELL":
            out.append(o); continue
        try:
            if adapter is None:
                adapter = ClobAdapter()
            client = adapter._connect()
            bal = adapter._conditional_balance(client, o.token_id)
            if bal is None or bal >= o.qty - 1e-9:      # UNKNOWN (fail-closed, don't convert an exit) OR
                out.append(o); continue                 # closing a held long — leave the SELL as-is
            no_token = adapter._complement_token(client, o.market_id, o.token_id)
            if not no_token:
                out.append(o); continue                 # complement unresolved — leave (fails cleanly)
            no_limit = max(0.01, min(0.99, round(1.0 - o.limit_price, 2)))
            qty = o.qty
            if min_notional > 0 and no_limit > 0:       # size to ~min_notional collateral at NO price (min-size)
                qty = int((min_notional / no_limit) * 100 + 1) / 100.0
            cost = qty * no_limit                       # BUY-NO collateral = premium paid
            if budget is not None and committed + cost > float(budget) + 1e-9:
                out.append(o); continue                 # arm budget exhausted — leave the SELL (fails cleanly)
            committed += cost
            out.append(replace(o, side="BUY", token_id=no_token, limit_price=no_limit, qty=round(qty, 4)))
            sig = signals.get((o.market_id, o.token_id))
            if sig is not None:
                signals[(o.market_id, no_token)] = sig  # re-join fair/edge/decomp/slug on the NO leg
        except Exception:
            out.append(o)                               # fail-soft: leave the SELL (never mis-record)
    return out


def fire_strategy(spec, store: Store, ledger: PnLLedger, live: bool) -> dict:
    t = int(time.time())
    name = spec.name
    print(json.dumps({"ts": t, "strategy": name, "env": _declared_env()}),
          flush=True)                              # per-fire effective-flags log (env-contamination audit)
    # generate position-aware delta orders
    positions = _load_positions(name)
    resting_rows = _load_resting(name)
    signals: Dict[tuple, dict] = {}
    # A strategy declares what context it wants via its signature: the driver offers the full
    # tenant context (as_of_ts, gate_fn, positions, params, out_signals, resting) plus the row's
    # own params, and passes ONLY what the function accepts — a minimal strategy can be
    # generate_orders(store) and still fire.
    offered = {"as_of_ts": t, "gate_fn": _gate_fn(spec.params), "positions": positions,
               "params": spec.params, "out_signals": signals, "resting": resting_rows,
               "strategy_id": name,                     # the row name — per-arm attribution in tenant logs
               "universe_rows": store.live_universe()}  # LIGHT Gamma-derived select set (no parquet scan);
    # strategies that accept universe_rows iterate this instead of the full historical store.universe().
    offered.update({k: v for k, v in (spec.params or {}).items() if k not in offered})
    gen_sig = inspect.signature(spec.generate_orders)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in gen_sig.parameters.values()):
        gen_kwargs = offered
    else:
        gen_kwargs = {k: v for k, v in offered.items() if k in gen_sig.parameters}
    orders = spec.generate_orders(store, **gen_kwargs)
    revived, revived_sigs = ([], {}) if live else _revive_resting(name, positions, t)
    if revived:                                    # prior maker quotes re-judged over the now-past window
        held = {(o.market_id, o.token_id) for o in revived}
        orders = revived + [o for o in orders if (o.market_id, o.token_id) not in held]
        for k, v in revived_sigs.items():
            signals.setdefault(k, v)               # fill TradeRows re-join the placement-time context
    n_views = signals.pop("__n_views__", None)     # observability: candidates PRICED this pass
    # execute paper or live. PER-SPEC live routing: even with LIVE_TRADING_ENABLED + a scoped PASS,
    # only specs whose params carry "live": true reach the executor — everything else stays paper
    # (defense-in-depth ABOVE the PASS strategies allowlist; a config row can't trade real money without
    # BOTH the row opting in AND the PASS covering it). LIVE fires run with SIGALRM DISARMED: a deadline
    # kill between the exchange POST and local persistence would leave real exposure with no local record —
    # the live path is bounded by its own HTTP timeouts instead; the (fast, local-only) persistence tail
    # runs unarmed too — the outer loop re-arms on the next fire.
    live = bool(live and (spec.params or {}).get("live"))
    if live:
        orders = _convert_shorts_to_buy_no(orders, signals, spec.params)  # negRisk short -> BUY-NO (min-sized);
        # done here so executor.run + _update_positions + reconciliation all use the executed BUY-NO leg
        from desk.live import executor
        signal.alarm(0)
        # category_of WIRED into the live gate (B6): with a tenant resolver present, every order's
        # market class is checked against the PASS scope (weather-scoped PASS actually scopes;
        # unclassifiable -> refused). Desk-only: None -> the single "default" class.
        _cat_of, _ = _paper_resolvers()
        result = executor.run(orders, RunConfig(store=store), category_of=_cat_of)
    else:
        _cat_of, _cost_model = _paper_resolvers()   # items 2+4: per-category fee + base-band exec-cost
        result = paper_run(orders, RunConfig(store=store, mark_mode="market",
                                             category_of=_cat_of, cost_model=_cost_model))
    # 4) ledger (aggregate + one TradeRow per fill) + position persistence.
    # run_meta pins each row to run_id/source/config_hash/env so a contaminated span is
    # machine-identifiable; signals carry fair/edge/decomp/slug for the trade-time join.
    run_meta = {
        "run_id": f"{name}-{t}",
        "source": "live" if live else "paper",
        "strategy": name,
        "config_hash": _config_hash(spec),
        "orders_by_id": {o.order_id: o for o in orders},
        "code_ref": _code_ref(),
        "env": _declared_env(),
    }
    ledger.log_run(result, as_of=t, run_meta=run_meta, signals=signals)
    n_resting = _update_resting(name, orders, result.exec_reports, t, signals)
    # Position persistence under the shared cross-process lock, applied to a FRESH re-read: a fire can
    # run up to FIRE_DEADLINE_S while the exit monitor closes positions in parallel — netting this
    # fire's fills into the STALE snapshot taken at fire start would overwrite those closes (the closed
    # position resurrects, the monitor re-sells it next pass, realized PnL misbooks). Orders were
    # generated off the start-of-fire snapshot (inherent to a long fire); the PERSISTED state is what
    # must stay true. live_orders is written in the same critical section (the exit monitor writes it too).
    with positions_lock(name):
        positions = _load_positions(name)
        exits = _update_positions(positions, orders, result.exec_reports, signals, t)
        _stamp_positions(positions, orders, signals, run_meta["config_hash"], run_meta["source"])
        _save_positions(name, positions)
        if live:                                       # track posted live orders for the reconciliation pass
            _record_live_orders(name, orders, result.exec_reports, signals, run_meta["config_hash"], t)
    for x in exits:                                    # realized EXITS reach the ledger
        ledger.log_settle(name, x["pnl"], as_of=t, source="EXIT",
                          run_id=run_meta["run_id"], config_hash=x["config_hash"] or run_meta["config_hash"],
                          market_id=x["market_id"], token_id=x["token_id"], slug=x.get("slug"),
                          qty=x["qty"], payout=x["exit_price"], entry_price=x["entry_price"],
                          extras={"source": x.get("source") or run_meta["source"], "kind": "exit",
                                  "order_id": str(x.get("order_id") or ""),
                                  "entry_ts": x.get("entry_ts")})
    return {"ts": t, "strategy": name, "live": live, "n_views": n_views, "n_orders": len(orders),
            "filled": sum(r.filled_qty for r in result.exec_reports), "resting": n_resting,
            "open_positions": len(positions), "exec_pnl": result.exec_pnl}


def _reload_specs(specs: List, due: Dict[str, float], now: float) -> List:
    """Per-loop registry reload — the control plane's PRIMARY pause mechanism (ui-redesign v2 §1):
    re-run load_registry (cheap: config JSON read + sha256 seal re-verify; tenant modules are already
    imported) at the top of every loop iteration so a config `enabled` flip takes effect at the next
    30s tick WITHOUT a driver restart. Schedule state is preserved BY NAME in the due-map:
      • a row that disappears (disabled / unsealed / refused / removed) stops firing and is logged
        ONCE (its due entry is popped — open positions still settle + exit-manage via the on-disk
        union passes);
      • a row that (re)appears mid-run is scheduled at its NEXT window, never fired immediately —
        an off-window fire on resume would surprise the operator, and this also makes a transient
        config glitch harmless (drop + re-add costs nothing for release_times schedules);
      • a TORN/mid-edit config read keeps the LAST GOOD registry firing (load_registry would
        return "defaults only, all disabled" on malformed JSON — dropping every arm over a
        transient partial write must not skip a due fire window). Process START is unchanged:
        everything fires immediately (due=0)."""
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path) as f:
                json.load(f)
        except Exception as e:
            print(json.dumps({"ts": int(now), "registry_reload":
                              "config unreadable (mid-edit?) — keeping last good registry",
                              "error": repr(e)[:120]}), flush=True)
            return specs
    new_specs = load_registry()
    new_names = {s.name for s in new_specs}
    for gone in sorted(set(due) - new_names):
        due.pop(gone, None)
        print(json.dumps({"ts": int(now), "strategy": gone, "registry":
                          "row dropped (disabled/unsealed/removed) — stops firing at the next window; "
                          "open positions still settle and the exit monitor still manages them"}),
              flush=True)
    for s in new_specs:
        if s.name not in due:
            due[s.name] = _next_due(s, now)
            print(json.dumps({"ts": int(now), "strategy": s.name, "registry":
                              "row added/re-enabled — first fire at its next window",
                              "next_due": int(due[s.name])}), flush=True)
    return new_specs


def _next_due(spec, now: float) -> float:
    sched = spec.schedule or {}
    if sched.get("type") == "release_times":
        best = None
        for hm in sched.get("times_utc", ["02:15", "08:15", "14:15", "20:15"]):
            hh, mm = (int(x) for x in hm.split(":"))
            g = time.gmtime(now)
            ts = calendar.timegm((g.tm_year, g.tm_mon, g.tm_mday, hh, mm, 0, 0, 0, 0))
            if ts <= now:
                ts += 86400
            best = ts if best is None else min(best, ts)
        return best
    return now + float(sched.get("secs", 21600))


def _heartbeat(info: dict) -> None:
    os.makedirs(DRIVER_DIR, exist_ok=True)
    path = os.path.join(DRIVER_DIR, "_heartbeat")
    tmp = path + ".tmp"                 # atomic: a kill mid-write must never leave a torn/empty
    with open(tmp, "w") as f:           # heartbeat that ops tooling misreads as a dead driver
        json.dump(info, f)
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="fire all due strategies once and exit")
    args = ap.parse_args()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGALRM, _on_alarm)   # per-fire deadline SKIPS the fire (was: killed the process)
    from desk.runtime.guard import job_guard
    job_guard("strategydriver", deadline_s=0)   # singleton: a second driver instance exits 0;
                                                # deadlines are PER-FIRE alarms, not process-level
    store = Store(sources=Sources())   # sources REQUIRED: without it Store.resolution stops at the
                                       # local jsonl and the Gamma/CLOB settle chain is unreachable
    ledger = PnLLedger()
    specs = load_registry()
    due = {s.name: 0.0 for s in specs}     # fire immediately on first loop

    names = [s.name for s in specs]
    if args.once:
        for s in specs:
            try:
                signal.alarm(FIRE_DEADLINE_S)                  # per-fire deadline SKIPS a hung fire
                print(json.dumps(fire_strategy(s, store, ledger, _live_enabled())), flush=True)
            except Exception as e:                             # incl. _FireTimeout — one slow spec can't abort --once
                print(json.dumps({"ts": int(time.time()), "strategy": s.name, "error": repr(e)}), flush=True)
            finally:
                signal.alarm(0)
        rec = reconcile_live_orders(store, ledger, names)     # book late live fills + expire makers
        if rec.get("polled"):
            print(json.dumps({"reconcile": rec}), flush=True)
        print(json.dumps({"settle": settle_positions(store, ledger, names)}), flush=True)
        _heartbeat({"ts": int(time.time()), "live": _live_enabled(), "once": True})
        return

    next_settle = 0.0
    while not _stop:
        now = time.time()
        # Per-loop registry reload: config flips (pause/resume) land at the next tick, no restart.
        specs = _reload_specs(specs, due, now)
        names = [s.name for s in specs]
        for s in specs:
            if now >= due[s.name]:
                try:
                    signal.alarm(FIRE_DEADLINE_S)              # hung fire self-kills; the supervisor restarts
                    info = fire_strategy(s, store, ledger, _live_enabled())
                    signal.alarm(0)
                    print(json.dumps(info), flush=True)
                except Exception as e:
                    signal.alarm(0)
                    print(json.dumps({"ts": int(now), "strategy": s.name, "error": repr(e)}), flush=True)
                due[s.name] = _next_due(s, time.time())
        if now >= next_settle:                                 # Gamma lookups per open position — pace it
            next_settle = now + 1800.0
            try:                                               # book late live fills + expire makers
                signal.alarm(FIRE_DEADLINE_S)
                rec = reconcile_live_orders(store, ledger, names)
                signal.alarm(0)
                if rec.get("reconciled_fills") or rec.get("expired_cancelled"):
                    print(json.dumps({"reconcile": rec}), flush=True)
            except Exception as e:
                signal.alarm(0)
                print(json.dumps({"reconcile_error": repr(e)}), flush=True)
            try:
                signal.alarm(FIRE_DEADLINE_S)
                st = settle_positions(store, ledger, names)
                signal.alarm(0)
                if st["settled"]:
                    print(json.dumps({"settle": st}), flush=True)
            except Exception as e:
                signal.alarm(0)
                print(json.dumps({"settle_error": repr(e)}), flush=True)
        # next_due None when EVERY row is paused/dropped — min() on an empty due-map would crash
        # the driver the moment the last arm is disabled (reload can legitimately empty it).
        _heartbeat({"ts": int(time.time()), "live": _live_enabled(),
                    "next_due": (min(due.values()) if due else None)})
        time.sleep(30.0)


if __name__ == "__main__":
    main()
