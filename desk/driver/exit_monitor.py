"""driver/exit_monitor.py — TIGHT TP/SL exit monitor (runs ~every 5 min under the process supervisor).

DECOUPLED from the 2h entry scan: it SWEEPS every on-disk positions_*.json (the settle_positions
union pattern — control plane v2 §2), REGARDLESS of the row's `enabled` flag: pause means "no new
entries", NEVER "no risk management". Exit params (exit_tp/exit_sl, exit_order_builder hook) resolve
from the config row even when the row is DISABLED; a positions file with NO config row at all is
managed with the framework default TP/SL=none (loudly logged — settle-on-resolution stays the only
exit). For each placement-eligible arm it fetches the LIVE CLOB order book (best bid / best ask = the
REALIZABLE touch), and if the return-on-committed-capital crosses +exit_tp or −exit_sl it exits the
FULL position at the touch (IOC taker) through the SAME executor + ledger path the driver uses — so
trades.jsonl / positions / dashboard stay consistent.

ORDER PLACEMENT keeps the live-only gating exactly as before: exits post only when _live_enabled()
AND the row carries params.live — a paper arm's TP/SL runs INSIDE the strategy at fire time (the
monitor placing paper exits would double-exit), so with live disarmed (or for paper arms) the sweep
is READ-ONLY: held positions counted and logged, no books fetched, nothing posted.

NO universe scan (cheap, seconds). Fail-soft; the SHARED per-arm position lock (desk.driver.poslock,
same lock file as the driver's fire/settle/reconcile passes) so it can't race the driver (and the
exchange can't double-sell a position that's already closed). Posted exits are tracked via
_record_live_orders so the driver's reconcile pass books IOC fills the post-time snapshot missed.

    python3 -m desk.driver.exit_monitor
"""
from __future__ import annotations

import glob
import json
import os
import time
from types import SimpleNamespace

import requests

from desk.signals import registry as registry_mod
from desk.signals.registry import load_registry
from desk.driver import strategy_driver as _sd   # module ref: DRIVER_DIR read at CALL time (tests repoint it)
from desk.driver.exit_rules import tpsl_exit_order
from desk.driver.poslock import positions_lock
from desk.driver.strategy_driver import (_load_positions, _save_positions, _update_positions,
                                         _record_live_orders, _config_hash, _live_enabled)
from desk.runtime.guard import code_ref as _code_ref, declared_env as _declared_env
from desk.tracker.pnl_ledger import PnLLedger
from desk.live import executor
from desk.backtest.runner import RunConfig
from desk.data.store import Store
from desk.data.sources import Sources

CLOB = os.environ.get("CLOB_HOST") or "https://clob.polymarket.com"


def _live_touch(token: str):
    """(best_bid, best_ask) from the LIVE CLOB order book, or (None, None) fail-soft — the freshest
    realizable prices, so a 5-min monitor is actually tight (not the 5s-lagged captured book)."""
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token}, timeout=6)
        r.raise_for_status()
        d = r.json()
        bids = sorted((float(x["price"]) for x in d.get("bids", [])), reverse=True)
        asks = sorted(float(x["price"]) for x in d.get("asks", []))
        return (bids[0] if bids else None, asks[0] if asks else None)
    except Exception:
        return (None, None)


def _arm_realized(ledger, name: str):
    """Cumulative realized PnL for the arm from the ledger. A live exit is IOC — its fill reconciles ASYNC in
    the driver's reconcile_live_orders pass, so a single run's SYNCHRONOUS realized is usually 0 even when the
    SELL fills (the PnL books moments later via the EXIT settle). Logging this running total kills the
    misleading `realized:0` line that reads as 'the exit did nothing'."""
    try:
        return round(float((ledger.snapshot().by_strategy.get(name) or {}).get("realized", 0.0)), 4)
    except Exception:
        return None


def run_once() -> dict:
    """ONE sweep over every arm with on-disk positions (plus every enabled arm): count held
    positions, resolve exit params from the config row (DISABLED rows included — pause ≠ unmanaged),
    then evaluate + place TP/SL exits only where the live-only placement gate allows (see module
    docstring). Returns the per-arm summary dict the ops page tails."""
    live_ok = _live_enabled()
    ledger = PnLLedger()
    now = int(time.time())
    specs = {s.name: s for s in load_registry(include_disabled=True)}
    # The settle_positions union pattern: every on-disk positions_*.json is REAL (possibly exchange)
    # state and must be swept even when its row is disabled or gone; enabled arms without a file yet
    # are reported too (held 0) so the operator sees the monitor watching them.
    on_disk = [os.path.basename(p)[len("positions_"):-len(".json")]
               for p in glob.glob(os.path.join(_sd.DRIVER_DIR, "positions_*.json"))]
    enabled = {n for n, s in specs.items() if s.enabled}
    out: dict = {"live_enabled": live_ok}
    store = None                                   # built lazily — only a placing pass needs it
    for name in sorted(set(on_disk) | enabled):
        spec = specs.get(name)
        p = (spec.params or {}) if spec is not None else {}
        tp, sl = p.get("exit_tp"), p.get("exit_sl")
        if spec is None:
            print(json.dumps({"exit_monitor": "NO CONFIG ROW for on-disk positions — managing with "
                                              "framework default TP/SL=none (settle-on-resolution "
                                              "remains the only exit)", "arm": name, "ts": now}),
                  flush=True)
        # FRAMEWORK exit math by default (desk-only trees run without any tenant); a tenant may
        # override per arm via its manifest hook "exit_order_builder" (same signature).
        exit_builder = registry_mod.hook_for(name, "exit_order_builder") or tpsl_exit_order
        # Live-only placement gate — EXACTLY the pre-sweep conditions: global live enablement
        # (env flag + scoped PASS) AND the row's own params.live. `enabled` is deliberately absent.
        placeable = bool(live_ok and p.get("live") and (tp is not None or sl is not None))
        try:
            # SHARED per-arm lock (desk.driver.poslock — the same lock file the driver's
            # fire/settle/reconcile passes hold), so neither process can overwrite the other's
            # position read-modify-write.
            with positions_lock(name):
                positions = _load_positions(name)
                held = sum(1 for v in positions.values() if abs(float(v.get("qty") or 0.0)) > 1e-9)
                info = {"held": held, "enabled": (spec.enabled if spec is not None else None),
                        "tp": tp, "sl": sl}
                if spec is None:
                    info["row"] = "MISSING — framework default TP/SL=none"
                if not placeable:
                    if not live_ok:
                        info["managed"] = "swept read-only (live disarmed — no order placement)"
                    elif not p.get("live"):
                        info["managed"] = ("paper arm — TP/SL runs inside the strategy at fire time; "
                                           "a PAUSED paper arm's positions ride to settlement")
                    else:
                        info["managed"] = "no exit params — settle-on-resolution only"
                    info["arm_realized_total"] = _arm_realized(ledger, name)
                    out[name] = info
                    continue
                if store is None:
                    store = Store(sources=Sources())
                exits = []
                exit_sigs = {}                     # (m,tk) -> {"slug"}: TradeRows + order tracking keep the join
                for (m, tk), pos in list(positions.items()):
                    if abs(float(pos.get("qty") or 0.0)) < 1e-9:
                        continue
                    bid, ask = _live_touch(tk)
                    if bid is None or ask is None:
                        continue
                    rd = SimpleNamespace(book=SimpleNamespace(best_bid=bid, best_ask=ask))
                    o = exit_builder(pos, rd, m, tk, name, now, tp, sl)   # realizable-touch TP/SL, full qty
                    if o is not None:
                        exits.append(o)
                        exit_sigs[(m, tk)] = {"slug": pos.get("slug")}
                if not exits:
                    out[name] = {**info, "exits": 0,
                                 "arm_realized_total": _arm_realized(ledger, name)}
                    continue
                # category_of wired (B6): the PASS market_class scope checks exits too — a
                # weather-scoped PASS covers weather exits, refuses unclassifiable books.
                result = executor.run(exits, RunConfig(store=store),
                                      category_of=registry_mod.market_resolvers()[1])   # real IOC exits (reduce-only)
                run_meta = {"run_id": f"{name}-exitmon-{now}", "source": "live", "strategy": name,
                            "config_hash": _config_hash(spec),
                            "orders_by_id": {o.order_id: o for o in exits},
                            "code_ref": _code_ref(), "env": _declared_env()}
                ledger.log_run(result, as_of=now, run_meta=run_meta, signals=exit_sigs)  # TradeRows -> dashboard
                # Track the posted exits the SAME way a driver fire does, so reconcile_live_orders can book
                # an IOC exit the one post-time get_trades snapshot missed — without this, a sell that landed
                # on-exchange but lagged the snapshot leaves the local position open and it later settles WRONG.
                _record_live_orders(name, exits, result.exec_reports, exit_sigs, run_meta["config_hash"], now)
                realized = _update_positions(positions, exits, result.exec_reports, exit_sigs, now)
                for x in realized:
                    # full settle-row shape (qty/payout/entry_price + extras), identical to the driver's EXIT
                    # settle rows — the dashboard renders qty/price from these fields.
                    ledger.log_settle(name, x["pnl"], as_of=now, source="EXIT", run_id=run_meta["run_id"],
                                      config_hash=x.get("config_hash") or run_meta["config_hash"],
                                      market_id=x.get("market_id"), token_id=x.get("token_id"), slug=x.get("slug"),
                                      qty=x["qty"], payout=x["exit_price"], entry_price=x["entry_price"],
                                      extras={"source": x.get("source") or "live", "kind": "exit",
                                              "order_id": str(x.get("order_id") or ""),
                                              "entry_ts": x.get("entry_ts")})
                _save_positions(name, positions)
            out[name] = {"exits_submitted": len(exits), "realized_sync": len(realized),
                         "pnl_sync": round(sum(x["pnl"] for x in realized), 4),
                         "arm_realized_total": _arm_realized(ledger, name),
                         "note": "IOC fills reconcile async (driver.reconcile_live_orders); pnl books via EXIT settle"}
        except Exception as e:
            out[name] = {"error": repr(e)[:180]}
    return out


if __name__ == "__main__":
    import signal
    # per-RUN deadline: if a network call hangs mid-run, SELF-KILL so the supervisor's next 5-min interval
    # starts a fresh run — the interval won't overlap a stuck run, so without this a single hang would freeze
    # all TP/SL checking until manual intervention. On reconnect the next interval just works.
    def _deadline(*_a):
        raise SystemExit("exit_monitor: run exceeded 180s (network hang?) — the supervisor retries next interval")
    signal.signal(signal.SIGALRM, _deadline)
    signal.alarm(180)
    print(json.dumps({"exit_monitor_run": run_once(), "ts": int(time.time())}), flush=True)
    signal.alarm(0)
