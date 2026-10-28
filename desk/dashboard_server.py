"""desk/dashboard_server.py — the dashboard mini server.

Serves the generated static pages AND the dynamic JSON APIs that make them live:

    GET /                          → dashboard/index.html
    GET /<page>.html               → dashboard/<page>.html (index/backtest/forward/live/opportunities)
    GET /api/trades/<run_id>       → slim per-trade array for the dynamic window/range views
                                     [{d, ts, side, px, qty, pnl, sd, cap}, ...]
                                     (closed rows joined to their open rows by order_id)
    GET /api/opportunities         → LIVE opportunities rescan via the installed opportunities
                                     ADAPTER (adapters/CONTRACT.md; env DESK_OPP_ADAPTER picks one).
                                     60s result cache; returns the last scan INSTANTLY (labeled
                                     stale) when the adapter's quota probe reports exhaustion.
    GET /api/opportunities/cached  → the current base opportunities cache, enriched (zero quota) — the
                                     page-load hydration path.
    GET /api/completeness          → dashboard/data_completeness.json (completeness validator output)
    POST /api/rebuild              → regenerate all pages (build_dashboard) — returns build log tail.
                                     MUTATING, so (control plane v2 §4): POST-only (GET gets 405),
                                     requires header `X-Desk-Control: 1` + an allowlisted localhost
                                     Host (CSRF floor — forces a CORS preflight no cross-site form
                                     or <img src> prefetch can pass), and debounced: a second call
                                     within 15 min gets 429 carrying the running/last build's age.
    POST /api/strategy/<name>/pause   ┐ the CONTROL API (mission-control-v1 stage A): flip the row's
    POST /api/strategy/<name>/resume  │ `enabled` ONLY — flock + atomic config edit + loader verify
    POST /api/stack/pause-all         ┘ with rollback + fsync'd audit JSONL, via desk.control_plane.
                                     Same CSRF floor as /api/rebuild + a short debounce (429).
                                     Resuming a live-routed row needs body {"confirm_name": <row
                                     name>} (else 409). The API cannot set `live`, reseal, or touch
                                     LIVE_TRADING_ENABLED — live-arming stays out of the UI/API.
    GET /api/health                → the whole operational state as ONE JSON (30s server cache;
                                     control flips invalidate it): /ops collectors (processes,
                                     capture, driver, exits, substrate, quota, paper P&L) + per-arm
                                     rows {mode (4-state), since, open_positions, last_fire, today,
                                     effective params, rule (describe-hook line)} + the build_ops
                                     THRESHOLDS dict verbatim. Feeds the Version-A home AND the
                                     red-alert notifier loop.
    GET /api/events                → recent activity, newest-first (30s cache; control flips
                                     invalidate): driver-log fire lines + control-audit flips
                                     (seeds excluded — bootstrap bookkeeping, not activity) +
                                     supervisord spawned/exited process events. Log-derivable
                                     facts only; the home renders the wording.
    GET /api/week                  → last 7 UTC days from the trades ledger (30s cache):
                                     per-day realized aggregate + per-arm attribution — the
                                     home's weekly P&L strip.
    GET /                          → the Mission Control home (mission-control-v1 stage B):
                                     desk/templates/mission_control.html, hydrating from the
                                     APIs above. The classic overview stays at /index.html.
    GET /glossary                  → the plain-language glossary (stage C): one entry per
                                     build_ops.THRESHOLDS key rendered FROM the dict (chip and
                                     help text share one number source) + concept entries
                                     (corrected live-unlock/capture texts) + aka internal names.

Static snapshots in dashboard/ remain the frozen evidence; this server is the daily-use path.

    python3 -m desk.dashboard_server            # http://localhost:8788
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("DASH_PORT", "8788"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # repo root
DASH_DIR = os.path.join(ROOT, "dashboard")
RUNS_DIR = os.path.join(ROOT, "desk", "eval", "runs")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_opp_lock = threading.Lock()
_opp_cache = {"ts": 0.0, "data": None}
OPP_TTL_S = 60

# ── mutating-route guardrails (control plane v2 §4) ───────────────────────────
# CSRF floor for every MUTATING route (today: /api/rebuild; the full control API lands later):
# POST-only + required custom header + Host allowlist. The server binds 127.0.0.1, so a foreign Host
# means DNS rebinding or a stray origin; the custom header forces a CORS preflight no cross-site
# HTML form or simple fetch can pass.
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
REBUILD_DEBOUNCE_S = 900                 # /api/rebuild: at most one build per 15 min
_rebuild_lock = threading.Lock()         # guards the debounce state only — never held during a build
_rebuild = {"running_since": None, "last_start": 0.0}
# Control API (pause/resume/pause-all): same CSRF floor as /api/rebuild + a short debounce — a
# double-click must not race two config edits (the flock is the correctness lock; this is UX).
CONTROL_DEBOUNCE_S = 2.0
_control_lock = threading.Lock()
_control = {"last": 0.0}
CONTROL_ROUTE_RE = re.compile(r"^/api/strategy/([A-Za-z0-9_.-]+)/(pause|resume)$")
# GET /ops and GET /live.html stay read-only but shell out a refresh when the rendered page is
# stale; a non-blocking per-page lock bounds that to ONE subprocess at a time (a concurrent GET
# serves the last-built page instead of stacking rebuilds — thundering-herd guard).
_refresh_locks = {"ops": threading.Lock(), "live": threading.Lock()}


def _opp_adapter():
    """The installed OPPORTUNITIES adapter module (contract: .claude/skills/desk-dashboard/scripts/
    adapters/CONTRACT.md). env DESK_OPP_ADAPTER names it; default = the first of the known adapters
    that imports. None ⇒ no adapter installed — every opportunities endpoint degrades gracefully."""
    import importlib
    adir = os.path.join(ROOT, ".claude", "skills", "desk-dashboard", "scripts")
    sys.path.insert(0, adir)
    os.chdir(ROOT)
    env = os.environ.get("DESK_OPP_ADAPTER")
    try:
        names = [env] if env else sorted(
            f[:-3] for f in os.listdir(os.path.join(adir, "adapters"))
            if f.endswith(".py") and not f.startswith("_") and f != "ledger.py")
    except Exception:
        names = []
    for name in names:
        try:
            mod = importlib.import_module(f"adapters.{name}")
        except Exception:
            continue
        if hasattr(mod, "PAGE") and hasattr(mod, "scan"):   # the duck-typed contract
            return mod
    return None


def _slim_trades(run_id: str) -> list:
    """Join closed rows onto their open rows by order_id → one slim record per trade for the client-side
    window/range/caps recompute. cap = worst-case collateral per share (BUY: px, SELL: 1−px) × qty."""
    path = os.path.join(RUNS_DIR, run_id, "trades.jsonl")
    opens, closes = {}, {}
    with open(path) as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            (opens if r.get("status") in ("open", "resting") else closes)[r.get("order_id")] = r

    def _pb(c):
        """per-band pnl for the client band selector: opt=gross−fee, base=engine fill, adv=p90 stress"""
        if not c:
            return None
        ex = c.get("extras") or {}
        base = c.get("pnl_realized")
        g = ex.get("gross")
        pb = {"base": base,
              "opt": round(g - (c.get("fees") or 0.0), 4) if g is not None else None,
              "adv": ex.get("pnl_adverse")}
        return pb if any(v is not None for v in pb.values()) else None

    out = []
    for oid, o in opens.items():
        c = closes.pop(oid, None)
        px, qty = o.get("price") or 0.0, o.get("qty") or 0.0
        side = o.get("side") or "BUY"
        ts = o.get("ts") or 0
        cap = round(qty * (px if side == "BUY" else (1.0 - px)), 4)
        out.append({
            "d": time.strftime("%Y-%m-%d", time.gmtime(ts)),
            "ts": ts, "side": side, "px": px, "qty": qty,
            "pnl": (c or {}).get("pnl_realized"), "pb": _pb(c),
            "sd": time.strftime("%Y-%m-%d", time.gmtime((c or {}).get("ts") or ts)),
            "cap": cap,
        })
    for oid, c in closes.items():      # orphan settles (no logged entry event) still count as trades
        ets = (c.get("extras") or {}).get("entry_ts") or c.get("ts") or 0
        px = c.get("mid_entry") or 0.0
        qty = abs(c.get("qty") or 0.0)
        side = c.get("side") or ((c.get("extras") or {}).get("side")) or "BUY"
        out.append({
            "d": time.strftime("%Y-%m-%d", time.gmtime(ets)),
            "ts": ets, "side": side, "px": px, "qty": qty,
            "pnl": c.get("pnl_realized"), "pb": _pb(c),
            "sd": time.strftime("%Y-%m-%d", time.gmtime(c.get("ts") or ets)),
            "cap": round(qty * (px if side == "BUY" else (1.0 - px)), 4),
        })
    out.sort(key=lambda r: r["ts"])
    return out


def _opportunities() -> dict:
    """LIVE opportunities rescan for the Refresh button — 60s result cache. If the adapter declares a
    QUOTA_PROBE_URL, probe it first: with the provider's daily limit exhausted a live rescan would
    grind for minutes with no feedback, so return the last scan instantly (labeled stale) instead."""
    now = time.time()
    if _opp_cache["data"] is not None and now - _opp_cache["ts"] < OPP_TTL_S:
        return _opp_cache["data"]
    with _opp_lock:
        if _opp_cache["data"] is not None and time.time() - _opp_cache["ts"] < OPP_TTL_S:
            return _opp_cache["data"]
        D = _opp_adapter()
        if D is None:
            return {"rows": [], "as_of": 0, "error": "no opportunities adapter installed "
                                                     "(adapters/CONTRACT.md)"}
        probe_url = getattr(D, "QUOTA_PROBE_URL", None)
        body = ""
        if probe_url:
            try:
                import urllib.request as _rq
                with _rq.urlopen(probe_url, timeout=8) as r:
                    body = r.read(200).decode(errors="ignore")
            except Exception as e:
                body = str(e)
        if "limit exceeded" in body.lower():
            try:
                data = D.ensure_enriched(json.load(open(os.path.join(DASH_DIR, "opportunities.json"))))
            except Exception:
                data = {"rows": [], "as_of": 0}
            data["stale"] = True
            data["stale_reason"] = getattr(D, "QUOTA_RESET_HINT",
                                           "provider daily quota exhausted — live rescan impossible")
            _opp_cache.update(ts=time.time(), data=data)
            return data
        data = D.scan()                    # Refresh = live rescan (also refreshes the build cache)
        if data.get("dead_scan"):          # quota died mid-scan — last good scan beats dead rows
            try:
                good = D.ensure_enriched(json.load(open(os.path.join(DASH_DIR, "opportunities.json"))))
            except Exception:
                good = None
            if good and good.get("rows") and not good.get("dead_scan"):
                good["stale"] = True
                good["stale_reason"] = "live rescan came back quota-dead — showing the last good scan"
                data = good
            else:
                data["stale"] = True
                data.setdefault("stale_reason", "scan quota-dead and no good cache")
        _opp_cache.update(ts=time.time(), data=data)
        return data


# ── GET /api/health (mission-control-v1 stage A item 3) ───────────────────────
HEALTH_TTL_S = 30                        # one collection per 30s serves every poller (UI + notifier)
_health_lock = threading.Lock()
_health_cache = {"ts": 0.0, "data": None}
_BUILD_OPS = {"mod": None, "tried": False}
# /api/events + /api/week (stage B): same 30s-cache pattern; control flips invalidate events.
EVENTS_TTL_S = 30
_events_lock = threading.Lock()
_events_cache = {"ts": 0.0, "data": None}
WEEK_TTL_S = 30
_week_lock = threading.Lock()
_week_cache = {"ts": 0.0, "data": None}
HOME_TEMPLATE = os.path.join(ROOT, "desk", "templates", "mission_control.html")


def _build_ops():
    """The /ops collector module (.claude/skills/desk-dashboard/scripts/build_ops.py), file-loaded
    ONCE — /api/health reuses the exact collectors + THRESHOLDS the /ops page renders from, so the
    JSON and the page can never disagree. None when the skill isn't shipped (health then 503s)."""
    if _BUILD_OPS["mod"] is not None or _BUILD_OPS["tried"]:
        return _BUILD_OPS["mod"]
    _BUILD_OPS["tried"] = True
    path = os.path.join(ROOT, ".claude", "skills", "desk-dashboard", "scripts", "build_ops.py")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_dash_build_ops", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _BUILD_OPS["mod"] = mod
    except Exception as e:
        print(f"[dash] build_ops unavailable ({e!r}) — /api/health degraded to 503", flush=True)
    return _BUILD_OPS["mod"]


def _driver_log_stats(bo):
    """Per-arm driver-log digest: last fire line, last error (only when newer than the last fire —
    collect_driver's semantics), and today's UTC aggregates {fires, evaluated, orders, shares,
    realized} — the strategy cards' today-counts. Realized joins per-arm from trades.jsonl on the
    same UTC day key."""
    import calendar as _cal
    midnight = _cal.timegm(time.strptime(time.strftime("%Y-%m-%d", time.gmtime()), "%Y-%m-%d"))
    fires, errors, today = {}, {}, {}

    def _day_row(name):
        return today.setdefault(name, {"fires": 0, "zero_fires": 0, "evaluated": 0, "orders": 0,
                                       "shares": 0.0, "realized": 0.0})

    for ln in bo._tail_lines(os.path.join(bo.LOGS_DIR, "strategydriver.out.log"), n=3000):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if not isinstance(r, dict) or "strategy" not in r:
            continue
        name = r["strategy"]
        if "n_orders" in r:
            fires[name] = r
            if (r.get("ts") or 0) >= midnight:
                t = _day_row(name)
                t["fires"] += 1
                if not r.get("n_views"):            # n_views 0 or null ⇒ "evaluated 0 markets"
                    t["zero_fires"] += 1
                t["evaluated"] += int(r.get("n_views") or 0)
                t["orders"] += int(r.get("n_orders") or 0)
                t["shares"] = round(t["shares"] + float(r.get("filled") or 0.0), 4)
        elif "error" in r:
            errors[name] = {"ts": r.get("ts"), "error": str(r.get("error"))[:200]}
    for name in list(errors):                  # an error older than the last good fire is history
        f = fires.get(name)
        if f and (errors[name].get("ts") or 0) <= (f.get("ts") or 0):
            errors.pop(name)
    from desk.runtime.paths import data_path
    for ln in bo._tail_lines(data_path("tracker", "trades.jsonl"), n=3000):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("pnl_realized") is None or (r.get("ts") or 0) < midnight:
            continue
        t = _day_row(r.get("strategy") or "")
        t["realized"] = round(t["realized"] + float(r["pnl_realized"]), 4)
    return fires, errors, today


def _arm_rows(bo) -> list:
    """One row per arm = config rows ∪ on-disk positions files (a retired arm's residue positions
    must stay visible until they resolve — the mock's sample_weather card). Serves everything the
    strategy cards/popovers show: 4-state mode, mode-since (audit log), effective rule params
    (loader merge, seal-verified; refused rows fall back to the raw row), today counts, last
    fire/error, open positions, realized."""
    import glob
    from desk import control_plane as CP
    from desk.signals import registry as R
    from desk.runtime.paths import data_path

    try:
        with open(R.config_path()) as f:
            cfg_rows = json.load(f).get("strategies", [])
    except Exception:
        cfg_rows = []
    by_name = {r.get("name"): r for r in cfg_rows}
    names = [r.get("name") for r in cfg_rows if r.get("name")]
    for p in sorted(glob.glob(os.path.join(data_path("driver"), "positions_*.json"))):
        n = os.path.basename(p)[len("positions_"):-len(".json")]
        if n not in names:
            names.append(n)
    eff = {}
    try:
        for sp in R.load_registry(include_disabled=True):
            eff[sp.name] = {"params": {k: v for k, v in (sp.params or {}).items()
                                       if k != "_config_hash"},
                            "schedule": sp.schedule}
    except Exception:
        pass
    fires, errors, today = _driver_log_stats(bo)
    since = CP.mode_since()
    gates = CP.live_gates()
    pass_strats = set(((gates.get("pass") or {}).get("strategies")) or [])
    try:
        with open(data_path("tracker", "ledger.json")) as f:
            by_strat = json.load(f).get("by_strategy", {})
    except Exception:
        by_strat = {}
    rows = []
    for name in names:
        row = by_name.get(name)
        if name in eff:
            params, schedule = eff[name]["params"], eff[name]["schedule"]
        elif row is not None:
            params, schedule = dict(row.get("params") or {}), row.get("schedule")
        else:
            params, schedule = {}, None
        enabled = bool(row.get("enabled")) if row else False
        live_routed = bool(params.get("live"))
        try:
            with open(os.path.join(data_path("driver"), f"positions_{name}.json")) as f:
                n_open = len(json.load(f))
        except Exception:
            n_open = 0
        f = fires.get(name)
        rows.append({
            "name": name,
            "strategy": (row or {}).get("strategy", name),
            "has_row": row is not None,
            "enabled": enabled,
            "live": live_routed,
            "mode": CP.mode_of(enabled, live_routed, gates["env"], name in pass_strats),
            "since": since.get(name),
            "open_positions": n_open,
            "last_fire": ({"ts": f.get("ts"), "n_views": f.get("n_views"),
                           "orders": f.get("n_orders"), "filled": f.get("filled")} if f else None),
            "last_error": errors.get(name),
            "today": today.get(name, {"fires": 0, "zero_fires": 0, "evaluated": 0, "orders": 0,
                                      "shares": 0.0, "realized": 0.0}),
            "realized_total": (by_strat.get(name) or {}).get("realized"),
            "schedule": schedule,
            "params": params,
            # the describe-hook rule line (v2): None for residue arms — no config row, no rule
            "rule": (CP.rule_line(name, (row or {}).get("strategy", name), params, schedule)
                     if row is not None else None),
        })
    return rows


def _collect_health() -> dict:
    """The one health snapshot: /ops collectors + live gates + per-arm rows + THRESHOLDS."""
    bo = _build_ops()
    if bo is None:
        raise RuntimeError("ops collectors unavailable "
                           "(.claude/skills/desk-dashboard/scripts/build_ops.py not shipped)")
    from desk import control_plane as CP
    drv = bo.collect_driver()
    gates = CP.live_gates()
    try:                                  # "fires take up to N min" — the driver's own deadline
        from desk.driver.strategy_driver import FIRE_DEADLINE_S as _fds
    except Exception:
        _fds = None
    return {
        "ts": int(time.time()),
        "procs": bo.collect_procs(),
        "capture": bo.collect_capture(),
        "driver": {**{k: drv.get(k) for k in ("hb_age_s", "live", "next_due_in_s")},
                   "fire_deadline_s": _fds},
        "exitmon": bo.collect_exitmon(),
        "calib": bo.collect_calibration(),
        "quota": bo.collect_quota(),
        "pnl": bo.collect_pnl(drv),
        "live_lock": {"env": gates["env"], "pass": gates["pass"]},
        "fire_in_progress": CP.fire_in_progress(),
        "arms": _arm_rows(bo),
        # the SAME dict build_ops chips/tooltips render from — UI thresholds can never drift
        "thresholds": {k: list(v) for k, v in bo.THRESHOLDS.items()},
    }


SUPERVISOR_EVENT_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+INFO\s+(spawned|exited):\s+'?([A-Za-z0-9_.:-]+)'?")


def _collect_events() -> dict:
    """Recent activity, newest-first: driver-log fire lines + control-audit flips + supervisord
    process events. Every fact is log-derivable (the home renders the wording); audit seeds are
    excluded — they are mode-since bookkeeping, not activity. Capped at 60 events."""
    bo = _build_ops()
    if bo is None:
        raise RuntimeError("ops collectors unavailable")
    from desk import control_plane as CP
    evs = []
    for ln in bo._tail_lines(os.path.join(bo.LOGS_DIR, "strategydriver.out.log"), n=2500):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if isinstance(r, dict) and "strategy" in r and "n_orders" in r:
            evs.append({"ts": int(r.get("ts") or 0), "kind": "fire", "arm": r["strategy"],
                        "n_views": r.get("n_views"), "orders": int(r.get("n_orders") or 0),
                        "filled": float(r.get("filled") or 0.0)})
    for r in CP.read_audit():
        if r.get("action") == "seed":
            continue
        evs.append({"ts": int(r.get("ts") or 0), "kind": "control", "action": r.get("action"),
                    "arm": r.get("arm"), "new": r.get("new"),
                    "effective_at": r.get("effective_at")})
    for ln in bo._tail_lines(os.path.join(bo.LOGS_DIR, "supervisord.log"), n=1500):
        m = SUPERVISOR_EVENT_RE.match(ln)
        if not m:
            continue
        try:   # supervisord stamps LOCAL time — convert with the host's own tz, like it logged
            ts = int(time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))
        except Exception:
            continue
        evs.append({"ts": ts, "kind": "process", "program": m.group(3),
                    "event": "started" if m.group(2) == "spawned" else "exited"})
    evs.sort(key=lambda e: -e["ts"])
    return {"ts": int(time.time()), "events": evs[:60]}


def _collect_week() -> dict:
    """Last 7 UTC days from the trades ledger: per-day realized aggregate + per-arm attribution
    (the weekly strip + its popovers). Reads at most the last 8 MiB of trades.jsonl."""
    import calendar as _cal
    from desk.runtime.paths import data_path
    now = time.time()
    today_midnight = _cal.timegm(time.strptime(time.strftime("%Y-%m-%d", time.gmtime(now)),
                                               "%Y-%m-%d"))
    dates = [time.strftime("%Y-%m-%d", time.gmtime(today_midnight - i * 86400))
             for i in range(6, -1, -1)]
    agg = {d: {"date": d, "realized": 0.0, "closed": 0, "by_arm": {}} for d in dates}
    path = data_path("tracker", "trades.jsonl")
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8 * 1024 * 1024))
            lines = f.read().decode(errors="ignore").splitlines()
    except OSError:
        lines = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if not isinstance(r, dict) or r.get("pnl_realized") is None:
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(r.get("ts") or 0))
        row = agg.get(day)
        if row is None:
            continue
        pnl = float(r["pnl_realized"])
        arm = r.get("strategy") or "?"
        row["realized"] = round(row["realized"] + pnl, 4)
        row["closed"] += 1
        row["by_arm"][arm] = round(row["by_arm"].get(arm, 0.0) + pnl, 4)
    return {"ts": int(time.time()), "days": [agg[d] for d in dates]}


def _cached_json(handler, cache, lock, ttl, collect):
    """The one 30s-cache pattern every poll endpoint uses (health/events/week)."""
    now = time.time()
    if cache["data"] is not None and now - cache["ts"] < ttl:
        return handler._json(cache["data"])
    with lock:
        if cache["data"] is not None and time.time() - cache["ts"] < ttl:
            return handler._json(cache["data"])
        try:
            data = collect()
        except Exception as e:
            return handler._json({"error": f"collectors unavailable: {e!r}"}, 503)
        cache.update(ts=time.time(), data=data)
    return handler._json(data)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DASH_DIR, **kw)

    def end_headers(self):                            # pages regenerate constantly — never let the browser
        self.send_header("Cache-Control", "no-store") # serve a stale copy (user saw 0-trade lanes from cache)
        super().end_headers()

    def log_message(self, fmt, *args):                    # quiet: one line per request to stdout
        sys.stdout.write("[dash] %s %s\n" % (self.address_string(), fmt % args))

    def _html(self, body: str, code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _control_gate(self) -> bool:
        """CSRF floor for mutating routes: True = refused (response already sent)."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in ALLOWED_HOSTS:
            self._json({"error": f"host {host!r} not allowed", "allowed": sorted(ALLOWED_HOSTS)}, 403)
            return True
        if (self.headers.get("X-Desk-Control") or "").strip() != "1":
            self._json({"error": "missing control header — send X-Desk-Control: 1"}, 403)
            return True
        return False

    def _control_debounce(self) -> bool:
        """Control-action debounce (double-click guard): True = refused (429 already sent). The
        slot is consumed up front like /api/rebuild's — the flock in control_plane is the
        correctness lock; this only stops accidental rapid-fire mutations."""
        now = time.time()
        with _control_lock:
            since = now - _control["last"]
            if since < CONTROL_DEBOUNCE_S:
                wait = max(1, int(CONTROL_DEBOUNCE_S - since) or 1)
                self._json({"error": "debounced — a control action just ran",
                            "retry_after_s": wait}, 429, headers={"Retry-After": str(wait)})
                return True
            _control["last"] = now
        return False

    def _body_json(self) -> dict:
        """The (small) JSON request body, or {} — never raises, never reads more than 64 KiB."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            obj = json.loads(self.rfile.read(min(n, 65536)).decode() or "{}")
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def do_GET(self):
        try:
            p0 = self.path.split("?")[0]
            if p0 in ("/", ""):
                # the Mission Control home (stage B). Fail-soft: a tree without the template
                # (or a pre-home bookmark) still gets the classic overview.
                try:
                    with open(HOME_TEMPLATE, "rb") as f:
                        body = f.read()
                except OSError:
                    self.path = "/index.html"
                    return super().do_GET()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if p0 in ("/glossary", "/glossary.html"):
                # stage C: rendered on the fly FROM build_ops.THRESHOLDS — the glossary's numbers
                # are definitionally the chips' numbers (no second copy to drift).
                bo = _build_ops()
                if bo is None:
                    return self._json({"error": "ops collectors unavailable — glossary needs "
                                                "build_ops.THRESHOLDS"}, 503)
                from desk import glossary as G
                return self._html(G.render_glossary(bo.THRESHOLDS))
            if p0 == "/api/health":
                return _cached_json(self, _health_cache, _health_lock, HEALTH_TTL_S, _collect_health)
            if p0 == "/api/events":
                return _cached_json(self, _events_cache, _events_lock, EVENTS_TTL_S, _collect_events)
            if p0 == "/api/week":
                return _cached_json(self, _week_cache, _week_lock, WEEK_TTL_S, _collect_week)
            if self.path.startswith("/api/trades/"):
                run_id = self.path[len("/api/trades/"):].split("?")[0]
                if not RUN_ID_RE.match(run_id):
                    return self._json({"error": "bad run_id"}, 400)
                try:
                    return self._json({"run_id": run_id, "trades": _slim_trades(run_id)})
                except FileNotFoundError:
                    return self._json({"error": "unknown run_id"}, 404)
            if self.path.startswith("/api/opportunities/cached"):
                # page-load hydration: the CURRENT base opportunities cache, enriched, zero quota — a
                # reload must never show OLDER data than the user's last successful Refresh.
                D = _opp_adapter()
                if D is None:
                    return self._json({"rows": [], "as_of": 0,
                                       "error": "no opportunities adapter installed"})
                try:
                    data = D.ensure_enriched(json.load(open(os.path.join(DASH_DIR, "opportunities.json"))))
                except Exception:
                    data = {"rows": [], "as_of": 0}
                return self._json(data)
            if self.path.startswith("/api/opportunities"):
                return self._json(_opportunities())
            if self.path.startswith("/api/completeness"):
                p = os.path.join(DASH_DIR, "data_completeness.json")
                if os.path.exists(p):
                    return self._json(json.load(open(p)))
                return self._json({"error": "not built yet"}, 404)
            if self.path.startswith("/api/rebuild"):
                # MUTATING — POST-only since 2026-10-26 (control plane v2 §4): as a GET, any <img src>,
                # link prefetch or cross-site navigation could trigger a full multi-minute rebuild.
                return self._json({"error": "POST only — curl -X POST -H 'X-Desk-Control: 1' "
                                            f"http://127.0.0.1:{PORT}/api/rebuild"}, 405,
                                  headers={"Allow": "POST"})
            p = self.path.split("?")[0]
            if CONTROL_ROUTE_RE.match(p) or p == "/api/stack/pause-all":
                # MUTATING control routes — POST-only, same reasoning as /api/rebuild.
                return self._json({"error": "POST only — send X-Desk-Control: 1 from an "
                                            "allowlisted localhost origin"}, 405,
                                  headers={"Allow": "POST"})
            if self.path.split("?")[0] in ("/ops", "/ops.html"):
                try:                   # ops health page: regenerate at most ~1/min, serve the static render.
                    page = os.path.join(DASH_DIR, "ops.html")
                    fresh = os.path.exists(page) and (time.time() - os.path.getmtime(page) < 55)
                    # non-blocking lock: concurrent stale GETs serve the last render, never stack builds
                    if not fresh and _refresh_locks["ops"].acquire(blocking=False):
                        try:           # build_ops degrades gracefully (supervisord down ⇒ file-based rows)
                            subprocess.run([sys.executable,
                                            os.path.join(ROOT, ".claude", "skills", "desk-dashboard",
                                                         "scripts", "build_ops.py")],
                                           capture_output=True, text=True, timeout=90, cwd=ROOT)
                        finally:
                            _refresh_locks["ops"].release()
                except Exception:
                    pass               # fail-soft: serve the last-built ops.html if the refresh errors
                self.path = "/ops.html"
                return super().do_GET()
            if self.path.split("?")[0] in ("/live.html", "/live"):
                try:                       # LOAD-TIME re-mark: refetch live books for open positions + re-render
                    page = os.path.join(ROOT, "dashboard", "live.html")   # debounce keys off the RENDERED page,
                    # not marks.json — the driver's settle pass also writes marks without re-rendering, and on
                    # a first-ever visit the page doesn't exist yet; both must trigger a refresh.
                    fresh = os.path.exists(page) and (time.time() - os.path.getmtime(page) < 20)
                    # non-blocking lock on top of the 20s debounce: concurrent stale GETs reuse the
                    # just-rendered page instead of re-hammering the book API with parallel refreshes
                    if not fresh and _refresh_locks["live"].acquire(blocking=False):
                        try:
                            subprocess.run(
                                [sys.executable,
                                 os.path.join(ROOT, ".claude", "skills", "desk-dashboard", "scripts", "refresh_live.py")],
                                capture_output=True, text=True, timeout=30, cwd=ROOT)
                        finally:
                            _refresh_locks["live"].release()
                except Exception:
                    pass                   # fail-soft: serve the last-built live.html if the refresh errors
            return super().do_GET()
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": repr(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            p = self.path.split("?")[0]
            m = CONTROL_ROUTE_RE.match(p)
            if m or p == "/api/stack/pause-all":
                # ── the control API (mission-control-v1 stage A) ──────────────────────
                if self._control_gate():
                    return                             # 403 already sent (CSRF floor)
                if self._control_debounce():
                    return                             # 429 already sent
                from desk import control_plane as CP
                src_ip, ua = self.client_address[0], self.headers.get("User-Agent")
                try:
                    if m:
                        name, verb = m.group(1), m.group(2)
                        enable = (verb == "resume")
                        body = self._body_json()
                        row = CP.row_for(name)
                        if enable and row is not None and CP.is_live_routed(row):
                            # typed-name confirm (v2 §5): resuming a LIVE-routed arm restores
                            # configured real-money exposure — never one accidental click.
                            if body.get("confirm_name") != name:
                                return self._json(
                                    {"error": "row is live-routed — resume needs typed "
                                              "confirmation", "expect": {"confirm_name": name}},
                                    409)
                        out = CP.flip_enabled([name], enable, verb,
                                              source_ip=src_ip, ua=ua)
                    else:
                        out = CP.pause_all(source_ip=src_ip, ua=ua)
                    _health_cache.update(ts=0.0)       # a flip must show on the next health poll
                    _events_cache.update(ts=0.0)       # ... and its audit line in the next feed
                    return self._json(out)
                except CP.ControlError as e:
                    return self._json(e.payload, e.status)
            if p == "/api/rebuild":
                if self._control_gate():
                    return                             # 403 already sent (CSRF floor)
                now = time.time()
                with _rebuild_lock:                    # debounce: one build per 15 min, never two at once
                    if _rebuild["running_since"] is not None:
                        age = int(now - _rebuild["running_since"])
                        wait = max(1, REBUILD_DEBOUNCE_S - age)
                        return self._json({"error": "rebuild already running",
                                           "running_for_s": age, "retry_after_s": wait}, 429,
                                          headers={"Retry-After": str(wait)})
                    since_last = now - _rebuild["last_start"]
                    if since_last < REBUILD_DEBOUNCE_S:
                        wait = int(REBUILD_DEBOUNCE_S - since_last) or 1
                        return self._json({"error": "debounced — a build ran recently",
                                           "last_build_s_ago": int(since_last),
                                           "retry_after_s": wait}, 429,
                                          headers={"Retry-After": str(wait)})
                    _rebuild["running_since"] = now
                    _rebuild["last_start"] = now
                try:
                    r = subprocess.run([sys.executable,
                                        os.path.join(ROOT, ".claude", "skills", "desk-dashboard",
                                                     "scripts", "build_dashboard.py")],
                                       capture_output=True, text=True, timeout=900, cwd=ROOT)
                    return self._json({"ok": r.returncode == 0, "tail": (r.stdout or r.stderr)[-1200:]})
                finally:
                    with _rebuild_lock:
                        _rebuild["running_since"] = None
            return self._json({"error": "unknown endpoint"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": repr(e)}, 500)
            except Exception:
                pass


def main():
    os.chdir(ROOT)
    try:
        from desk import control_plane as CP
        n = CP.seed_audit()                # day-one mode-since (idempotent; no-op once history exists)
        if n:
            print(f"[dash] control audit seeded: {n} row(s) -> {CP.audit_path()}", flush=True)
    except Exception as e:                 # the dashboard must come up even if seeding can't
        print(f"[dash] control audit seed skipped: {e!r}", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[dash] serving {DASH_DIR} + APIs on http://localhost:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
