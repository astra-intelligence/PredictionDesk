#!/usr/bin/env python3
"""build_ops.py — the /ops health page: one glance answers "what is running and is it actually working".

Renders dashboard/ops.html (same dark style as the dashboard). Sections, each row with a
red/amber/green chip:

  processes     supervisord XML-RPC (127.0.0.1:9100): name, state, uptime, pid, restarts (counted
                from the supervisord log). DEGRADES to a note when supervisord is down — every other
                section is file-based and still renders.
  capture       heartbeat age, polls/rows/tokens, parquets written last hour + newest parquet age
                (today+yesterday partitions scanned), universe rows + age, data-partition size,
                filesystem free.
  driver        heartbeat age + next-due, per-arm last fire {n_views, n_orders, filled} parsed from
                the driver's own stdout JSON lines, realized P&L (ledger) + open positions per arm.
  exit monitor  lastpass stamp age (ops/exitmonitor_loop.sh writes it after every pass). Paper note:
                with live disarmed each pass sweeps every positions file READ-ONLY (disabled rows
                included) and places nothing — chip judges the CADENCE only.

Chip thresholds: ONE dict (THRESHOLDS) feeds both the chip logic (chip_for) and the tooltip text
(threshold_note) — chip and annotation can never drift apart.
  calibration   per-tenant substrate age, spec-driven (adapters/<name>.ops.json): tenant lanes that
                FAIL CLOSED when their rolling-calib substrate goes stale (>7d = red) — market meta,
                error series, calibration window, spread model.
  om quota      the shared Open-Meteo daily meter (live_forecast_cache.json __meter__) vs the soft cap.
  paper p&l     realized today + total per arm (ledger/trades), open position count.
  live unlock   the two live gates (env flag + scoped PASS): scope + expiry when a PASS exists,
                "none — live locked" otherwise (mission-control-v1 stage C).
  actions       control actions history — the audit JSONL newest-first (flip + seed rows), with a
                DESK_TZ display toggle (storage stays UTC).

Usage: python3 build_ops.py [--out <path>]   (also wired to GET /ops on the dashboard server,
rebuilt at most once a minute). Needs no network except the localhost RPC probe (2.5s timeout).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, REPO)

from desk.runtime.paths import data_path  # noqa: E402  (env-aware: honors DESK_DATA_DIR)

DATA_HOME = os.environ.get("POLYEDGE_DATA_DIR") or os.path.join(os.path.expanduser("~"), "polyedge-data")
LOGS_DIR = os.path.join(DATA_HOME, "logs")
RPC_URL = os.environ.get("OPS_SUPERVISOR_URL", "http://127.0.0.1:9100/RPC2")
OM_CAP = int(os.environ.get("OM_DAILY_SOFT_CAP", "8500"))

GREEN, AMBER, RED, GREY = "ok", "warn", "bad", "na"


def _age(ts):
    return None if not ts else max(0.0, time.time() - float(ts))


def _fmt_age(s):
    if s is None:
        return "—"
    s = int(s)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 2 * 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d"


def _jload(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _tail_lines(path, n=400):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            return f.read().decode(errors="ignore").splitlines()[-n:]
    except Exception:
        return []


def chip_age(age_s, green_s, red_s):
    """Age chip: fresh under green_s, red past red_s, amber between; grey when unknown."""
    if age_s is None:
        return GREY
    return GREEN if age_s <= green_s else (RED if age_s > red_s else AMBER)


# ── chip thresholds — ONE source of truth for the chip logic AND the tooltip text ─────────────────
# (green_s, red_s) per age-judged chip: green ≤ green_s · red > red_s · amber between (chip_age).
# Every age chip goes through chip_for(key) and its annotation through threshold_note(key), so the
# note can never contradict the chip again (the old hand-written capture note claimed "stale >5min
# = red" while the chip actually went red at 30min). Values UNCHANGED from the previous call sites —
# this kills the drift, not the thresholds.
THRESHOLDS = {
    "capture_hb":   (300, 1800),             # capture heartbeat age (flushes every ~30s)
    "parquet":      (600, 3600),             # newest captured parquet age
    "universe":     (2 * 3600, 6 * 3600),    # _universe.json age (refreshes every ~30min)
    "driver_hb":    (300, 1800),             # driver heartbeat age (writes every ~30s)
    "exitmon_pass": (600, 1800),             # exit-monitor lastpass age (5-min cadence)
    "market_meta":  (36 * 3600, 3 * 86400),  # tenant market_meta build age
    # substrate error-series age — past red the rolling calib FAILS CLOSED (arms refuse to trade)
    "substrate_series": (3 * 86400, 7 * 86400),
    # OM quota %: amber at green-bound, red never reachable in practice (at the cap, cached
    # pricing continues — the red bound only exists to keep the (green, red) tuple shape)
    "quota_pct":    (70, 1000),
}


def chip_for(key, age_s):
    """The chip for an age-judged row, from THRESHOLDS — never ad-hoc green/red seconds."""
    g, r = THRESHOLDS[key]
    return chip_age(age_s, g, r)


def threshold_note(key, extra=""):
    """The tooltip's threshold sentence, RENDERED from THRESHOLDS — never hand-written."""
    g, r = THRESHOLDS[key]
    t = f"amber &gt;{_fmt_age(g)} · red &gt;{_fmt_age(r)}"
    return f"{t} — {extra}" if extra else t


# ── processes (supervisord XML-RPC) ───────────────────────────────────────────────────────────────
def collect_procs():
    import xmlrpc.client
    out = {"up": False, "rows": [], "err": None}
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(2.5)
    try:
        srv = xmlrpc.client.ServerProxy(RPC_URL)
        infos = srv.supervisor.getAllProcessInfo()
        restarts = _restart_counts()
        for p in infos:
            up_s = (p["now"] - p["start"]) if p["statename"] == "RUNNING" else None
            out["rows"].append({
                "name": f"{p['group']}:{p['name']}" if p.get("group") and p["group"] != p["name"]
                        else p["name"],
                "state": p["statename"], "uptime_s": up_s, "pid": p.get("pid") or "",
                "restarts": restarts.get(p["name"], 0),
                "chip": GREEN if p["statename"] == "RUNNING"
                        else (AMBER if p["statename"] in ("STARTING", "BACKOFF") else RED)})
        out["up"] = True
    except Exception as e:
        out["err"] = repr(e)[:120]
    finally:
        socket.setdefaulttimeout(old)
    return out


def _restart_counts():
    """Exit events per program from the current supervisord log — an honest restart proxy
    (supervisord's RPC does not expose a restart counter)."""
    counts = {}
    for ln in _tail_lines(os.path.join(LOGS_DIR, "supervisord.log"), n=4000):
        m = re.search(r"exited: (\w+)", ln)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


# ── capture ───────────────────────────────────────────────────────────────────────────────────────
# Capture part files are write-once and NAMED with their flush epoch: part-<pid>-<epoch>-<seq>.parquet.
PART_EPOCH_RE = re.compile(r"^part-\d+-(\d{9,12})-\d+\.parquet$")


def collect_capture():
    """Name-parse scan (2026-10-27): the old walk stat()'ed EVERY parquet — with compaction behind
    (~1.8M part files over 2 days) one call took >2 minutes, wedging /ops rebuilds into their 90s
    subprocess timeout and any in-process caller (/api/health). Freshness now reads the flush epoch
    from the FILENAMES (scandir, no per-file stats; the epoch is the capture's own write stamp, so a
    late compaction can't masquerade as fresh data), and the exact 2d byte total is replaced by the
    2d parquet-file count (bytes needed the stats; the disk chip judges free space, which stays)."""
    cap_dir = data_path("data", "capture")
    hb = _jload(os.path.join(cap_dir, "_heartbeat"), {}) or {}
    uni_p = os.path.join(cap_dir, "_universe.json")
    uni = _jload(uni_p, {}) or {}
    now = time.time()
    cutoff = now - 3600
    newest_ts, n_hour, n_files = None, 0, 0

    def _leaf_dirs(part_dir):
        try:
            with os.scandir(part_dir) as markets:
                for mdir in markets:
                    if not mdir.is_dir():
                        continue
                    try:
                        with os.scandir(mdir.path) as subs:
                            for sub in subs:
                                if sub.is_dir():
                                    yield sub.path
                    except OSError:
                        continue
        except OSError:
            return

    for d in _recent_partitions(cap_dir, 2):
        for leaf in _leaf_dirs(d):
            try:
                with os.scandir(leaf) as files:
                    for e in files:
                        name = e.name
                        if not name.endswith(".parquet"):
                            continue
                        n_files += 1
                        m = PART_EPOCH_RE.match(name)
                        if not m:
                            continue                      # hour-*.parquet compactions: old rows by construction
                        ts = int(m.group(1))
                        if ts > now + 300:
                            continue                      # clock junk — never fake-fresh
                        if newest_ts is None or ts > newest_ts:
                            newest_ts = ts
                        if ts > cutoff:
                            n_hour += 1
            except OSError:
                continue
    try:
        free_gb = shutil.disk_usage(cap_dir).free / 1e9
    except Exception:
        free_gb = None
    return {"hb_age_s": _age(hb.get("ts")), "polls": hb.get("polls"), "book_rows": hb.get("book_rows"),
            "trade_rows": hb.get("trade_rows"), "tokens": hb.get("tokens"),
            "universe_rows": len(uni.get("tokens", {}) or {}),
            "universe_age_s": _age(os.path.getmtime(uni_p)) if os.path.exists(uni_p) else None,
            "newest_parquet_age_s": _age(newest_ts), "parquets_last_hour": n_hour,
            "parquet_files_2d": n_files, "disk_free_gb": free_gb}


def _recent_partitions(cap_dir, n=2):
    try:
        dates = sorted(d for d in os.listdir(cap_dir) if re.match(r"^\d{4}-\d{2}-\d{2}$", d))
    except Exception:
        return []
    return [os.path.join(cap_dir, d) for d in dates[-n:]]


# ── driver + arms ─────────────────────────────────────────────────────────────────────────────────
def _enabled_arms():
    cfgp = os.environ.get("DESK_STRATEGY_CONFIG")
    names = []
    if cfgp and os.path.exists(cfgp):
        for row in (_jload(cfgp, {}) or {}).get("strategies", []):
            if row.get("enabled"):
                names.append(row.get("name"))
    for p in glob.glob(os.path.join(data_path("driver"), "positions_*.json")):
        n = os.path.basename(p)[len("positions_"):-len(".json")]
        if n not in names:
            names.append(n)
    return names


def _mode_and_rule(name):
    """4-state mode chip + describe-hook rule line for an existing strategy row (ui-redesign v2
    quick fixes) — via desk.control_plane so the /ops page and /api/health can never disagree.
    Fail-soft: ops must render even when the registry refuses rows."""
    try:
        from desk import control_plane as CP
        row = CP.row_for(name)
        if row is None:
            return "OFF", None
        params = CP.effective_params(row)
        gates = CP.live_gates()
        pass_strats = set(((gates.get("pass") or {}).get("strategies")) or [])
        mode = CP.mode_of(bool(row.get("enabled")), bool(params.get("live")),
                          gates["env"], name in pass_strats)
        rule = CP.rule_line(name, row.get("strategy", name), params, CP.effective_schedule(row))
        return mode, rule
    except Exception:
        return None, None


def collect_driver():
    hb = _jload(data_path("driver", "_heartbeat"), {}) or {}
    ledger = _jload(data_path("tracker", "ledger.json"), {}) or {}
    bys = ledger.get("by_strategy", {})
    fires, errors = {}, {}
    for ln in _tail_lines(os.path.join(LOGS_DIR, "strategydriver.out.log"), n=600):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if not isinstance(r, dict) or "strategy" not in r:
            continue
        if "n_orders" in r:
            fires[r["strategy"]] = r
        elif "error" in r:
            errors[r["strategy"]] = r
    arms = []
    for name in _enabled_arms():
        pos = _jload(data_path("driver", f"positions_{name}.json"), {}) or {}
        f = fires.get(name)
        e = errors.get(name)
        mode, rule = _mode_and_rule(name)
        arms.append({"name": name, "fire": f, "err": (e if e and (not f or e["ts"] > f["ts"]) else None),
                     "open": len(pos), "realized": (bys.get(name) or {}).get("realized"),
                     "fees": (bys.get(name) or {}).get("fees"), "mode": mode, "rule": rule})
    nd = hb.get("next_due")
    return {"hb_age_s": _age(hb.get("ts")), "live": hb.get("live"),
            "next_due_in_s": (nd - time.time()) if nd else None, "arms": arms}


def collect_exitmon():
    p = os.path.join(LOGS_DIR, "exitmonitor.lastpass")
    try:
        ts = float(open(p).read().strip())
    except Exception:
        ts = None
    last = None
    for ln in reversed(_tail_lines(os.path.join(LOGS_DIR, "exitmonitor.out.log"), n=50)):
        if ln.strip().startswith("{"):
            last = ln.strip()[:160]
            break
    return {"lastpass_age_s": _age(ts), "last_line": last}


# ── calibration substrate (tenant-aware by FILE, degrades when the tenant is absent) ─────────────
def _series_freshness(path):
    """(newest_date, distinct days in the trailing 30) across a {city:{metric:{lead:{date:e}}}} series."""
    d = _jload(path)
    if not isinstance(d, dict):
        return None, 0
    dates = set()
    for city, mm in d.items():
        if str(city).startswith("_") or not isinstance(mm, dict):
            continue
        for metric in ("max", "min"):
            for dser in (mm.get(metric) or {}).values():
                dates.update(k for k in dser if re.match(r"^\d{4}-\d{2}-\d{2}$", str(k)))
    if not dates:
        return None, 0
    newest = max(dates)
    lo = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))
    return newest, len([x for x in dates if x >= lo])


def _date_age_days(datestr):
    if not datestr:
        return None
    try:
        return max(0.0, (time.time() - time.mktime(time.strptime(datestr, "%Y-%m-%d"))) / 86400.0)
    except Exception:
        return None


def collect_calibration():
    """Tenant substrate rows are DATA, not code: a dashboard adapter may ship an
    `adapters/<name>.ops.json` spec ({tenant, base, rows: [{kind, label, path, …}]}, kinds:
    meta/series/calwindow/spread) and this generic interpreter renders freshness chips from it.
    The desk knows no tenant's file layout; a tree without a spec (or whose tenant base dir is
    absent) degrades to tenant:None and the page says so."""
    for sp_path in sorted(glob.glob(os.path.join(HERE, "adapters", "*.ops.json"))):
        spec = _jload(sp_path, {}) or {}
        base = os.path.join(REPO, *str(spec.get("base", "")).split("/"))
        if not spec.get("tenant") or not os.path.isdir(base):
            continue
        rows = []
        for r in spec.get("rows", []):
            kind, label = r.get("kind"), r.get("label") or r.get("path", "?")
            p = os.path.join(base, *str(r.get("path", "")).split("/")) if r.get("path") else None
            if kind == "meta":
                if p and os.path.exists(p):
                    n = len(_jload(p, {}) or {})
                    a = _age(os.path.getmtime(p))
                    rows.append({"label": label, "value": f"{n} markets · built {_fmt_age(a)} ago",
                                 "chip": RED if n == 0 else chip_for("market_meta", a),
                                 "kind": "meta", "age_s": a, "markets": n,
                                 "note": "empty ⇒ every fire zeroed" if n == 0
                                         else threshold_note("market_meta", "stale meta silently zeroes fires")})
                else:
                    rows.append({"label": label, "value": "MISSING", "chip": RED,
                                 "note": "outcome labels unavailable — fires are zeroed"})
            elif kind == "series":
                need = bool(r.get("need"))
                if not (p and os.path.exists(p)):
                    rows.append({"label": label, "value": "MISSING", "chip": RED if need else AMBER,
                                 "kind": "series", "note": "lane fails closed without it" if need else "optional"})
                    continue
                newest, days30 = _series_freshness(p)
                age_d = _date_age_days(newest)
                # chip + note from THRESHOLDS["substrate_series"] — single source, never ad-hoc day counts
                chip = chip_for("substrate_series", None if age_d is None else age_d * 86400.0)
                rows.append({"label": label, "value": f"newest {newest or '—'} · {days30}d/30",
                             "chip": chip, "kind": "series", "newest": newest, "days30": days30,
                             "note": threshold_note("substrate_series", "rolling calib fails closed")})
            elif kind == "calwindow":
                if p and os.path.exists(p):
                    meta = (_jload(p, {}) or {}).get("_meta", {})
                    age_d = _date_age_days(meta.get("end"))
                    rows.append({"label": label, "value": f"window {meta.get('start')}→{meta.get('end')}",
                                 "chip": GREY if age_d is None else (GREEN if age_d <= 7 else AMBER),
                                 "note": r.get("note", "")})
                else:
                    rows.append({"label": label, "value": "MISSING", "chip": AMBER,
                                 "note": r.get("missing_note") or r.get("note") or "optional"})
            elif kind == "spread":
                sp = data_path("sim", "spread_model.json")
                if os.path.exists(sp):
                    a = _age(os.path.getmtime(sp))
                    rows.append({"label": label, "value": f"built {_fmt_age(a)} ago",
                                 "chip": GREEN, "note": "paper fills carry base-band spread+slippage"})
                else:
                    rows.append({"label": label, "value": "MISSING", "chip": AMBER,
                                 "note": "paper falls back to default fee + optimistic mid on T1-fallback fills"})
        return {"tenant": spec["tenant"], "rows": rows}
    return {"tenant": None, "rows": []}


def collect_quota():
    """Meter TRUTH, not just today's count. The meter file only advances when om_meter_add runs, so a
    day where pricing never reaches the forecast layer leaves YESTERDAY's row behind — on 2026-10-27
    this read as "0% used" while the handover was staring at REFUSED lines (which were 10-26's, at the
    cap). Surface the raw meter row (meter_day/meter_used/capped_on) alongside today's derived usage so
    "idle today" and "capped yesterday" are distinguishable, and say so in the note."""
    m = (_jload(data_path("data", "live_forecast_cache.json"), {}) or {}).get("__meter__", {})
    day = time.strftime("%Y-%m-%d", time.gmtime())
    used = m.get("weighted", 0) if m.get("day") == day else 0
    pct = 100.0 * used / OM_CAP if OM_CAP else 0.0
    amber = THRESHOLDS["quota_pct"][0]       # chip judged from THRESHOLDS — never an ad-hoc 70
    note = "cap reached ⇒ cached pricing continues (never a ban)" if pct >= 95 else ""
    if m.get("day") and m.get("day") != day:
        note = (f"no OM usage recorded yet today — meter last advanced {m.get('day')} "
                f"({m.get('weighted', 0)}/{OM_CAP}"
                + (", CAPPED that day" if m.get("cap_logged") == m.get("day") else "") + ")")
    return {"day": day, "used": used, "cap": OM_CAP, "pct": pct,
            "meter_day": m.get("day"), "meter_used": m.get("weighted", 0),
            "capped_on": (m.get("cap_logged") or None),
            "chip": GREEN if pct < amber else AMBER,   # no red: at the cap, cached pricing continues
            "note": note}


def collect_pass():
    """Live-unlock gates for the System page (mission-control-v1 stage C). v2 glossary fix: the
    PASS shows SCOPE + EXPIRY, never a bare countdown — a PASS is scoped per capability/strategy/
    market-class and the env flag is a separate gate."""
    try:
        from desk import control_plane as CP
        g = CP.live_gates()
        return {"env": bool(g.get("env")), "pass": g.get("pass"), "err": None}
    except Exception as e:
        return {"env": False, "pass": None, "err": repr(e)[:120]}


def collect_audit(limit=40):
    """Control actions history: the audit JSONL newest-first (ts, action, arm, prior→new,
    effective_at, source; seeds labeled). Read-only; missing file ⇒ empty history."""
    try:
        from desk import control_plane as CP
        rows = CP.read_audit()
    except Exception:
        rows = []
    rows = list(reversed(rows))
    return {"total": len(rows), "rows": rows[:limit]}


def collect_pnl(driver):
    # "today" = the UTC day (timegm), matching the OM quota meter's day key — the old local-midnight
    # mktime made "realized today" and quota disagree on day boundaries (bug logged in
    # context/ui-mock-build-spec.md; the real build standardizes on UTC).
    import calendar as _cal
    midnight = _cal.timegm(time.strptime(time.strftime("%Y-%m-%d", time.gmtime()), "%Y-%m-%d"))
    realized_today = 0.0
    n_today = 0
    trades_p = data_path("tracker", "trades.jsonl")
    for ln in _tail_lines(trades_p, n=3000):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("pnl_realized") is not None and (r.get("ts") or 0) >= midnight:
            realized_today += float(r["pnl_realized"])
            n_today += 1
    open_total = sum(a["open"] for a in driver["arms"])
    return {"realized_today": round(realized_today, 4), "closed_today": n_today,
            "open_total": open_total}


def collect():
    procs = collect_procs()
    cap = collect_capture()
    drv = collect_driver()
    return {"ts": int(time.time()), "procs": procs, "capture": cap, "driver": drv,
            "exitmon": collect_exitmon(), "calib": collect_calibration(),
            "quota": collect_quota(), "pnl": collect_pnl(drv),
            "pass_gates": collect_pass(), "audit": collect_audit()}


# ── render ────────────────────────────────────────────────────────────────────────────────────────
CSS = """
:root{--bg:#0c0e12;--bg2:#11141a;--line:#262d39;--ink:#e9e4d6;--ink-dim:#9aa1ad;--warm:#e0a44a;
--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
body{background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Helvetica,sans-serif;
max-width:980px;margin:0 auto;padding:40px 24px}
h1{font-weight:700;letter-spacing:-.5px;margin-bottom:2px}h1 .em{color:var(--warm)}
.sub{color:var(--ink-dim);margin:0 0 26px;font-size:13px;font-family:var(--mono)}
section{border:1px solid var(--line);border-radius:10px;background:var(--bg2);padding:16px 20px;margin:14px 0}
h2{margin:0 0 10px;font-size:16px}
table{border-collapse:collapse;width:100%;font-family:var(--mono);font-size:12.5px}
td,th{padding:4px 10px 4px 0;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:none}th{color:var(--ink-dim);font-weight:500}
.chip{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px;vertical-align:baseline}
.ok{background:#2b6}.warn{background:#e0a44a}.bad{background:#c44}.na{background:#555}
.mode{display:inline-block;border:1px solid;border-radius:999px;padding:0 8px;font-size:10px;letter-spacing:.06em}
.m-paper{color:#2596D1;border-color:#2596D1}.m-live{color:#E8554D;border-color:#E8554D}
.m-locked{color:#E8A020;border-color:#E8A020}.m-off{color:#5A6472;border-color:#5A6472}
.dim{color:var(--ink-dim)}.pos{color:#2b6}.neg{color:#c44}
.note{color:var(--ink-dim);font-size:11.5px}
footer{color:var(--ink-dim);font-size:12px;margin-top:28px;font-family:var(--mono)}
a{color:var(--warm)}
"""


def _c(state):
    return f'<span class="chip {state}"></span>'


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_ts(ts):
    return "—" if not ts else time.strftime("%m-%d %H:%M", time.gmtime(float(ts))) + " UTC"


# DESK_TZ display toggle (storage stays UTC; [data-ts] cells re-render client-side; the pref
# survives the page's 60s auto-refresh via localStorage). Plain string — no f-string braces.
_TZ_SCRIPT = """
<script>
(function(){
  var TZ='__DESK_TZ__';
  function p(n){return String(n).padStart(2,'0')}
  function fmt(ts,mode){var d=new Date(ts*1000);
    if(mode!=='local')return p(d.getUTCMonth()+1)+'-'+p(d.getUTCDate())+' '+p(d.getUTCHours())+':'+p(d.getUTCMinutes())+' UTC';
    try{return d.toLocaleString('en-GB',{timeZone:TZ,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).replace(',','')+' '+TZ.split('/').pop()}
    catch(e){return fmt(ts,'utc')}}
  function render(){var mode=localStorage.getItem('desk_tz_mode')||'utc';
    document.querySelectorAll('[data-ts]').forEach(function(el){var ts=parseFloat(el.getAttribute('data-ts'));
      if(isFinite(ts))el.textContent=fmt(ts,mode)});
    var b=document.getElementById('tzbtn');if(b)b.textContent='times: '+(mode==='utc'?'UTC':TZ)+' \\u21c4'}
  var b=document.getElementById('tzbtn');
  if(b)b.addEventListener('click',function(ev){ev.preventDefault();
    localStorage.setItem('desk_tz_mode',(localStorage.getItem('desk_tz_mode')||'utc')==='utc'?'local':'utc');render()});
  render();
})();
</script>"""


def render(d):
    H = []
    H.append(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<meta http-equiv="refresh" content="60"><title>desk ops</title><style>{CSS}</style></head><body>
<div style="margin-bottom:6px"><a href="/" style="color:#8aa">← overview</a> · <a href="/index.html" style="color:#8aa">classic</a> · <a href="/glossary" style="color:#8aa">glossary</a></div><h1>The <span class="em">Desk</span> — ops</h1>
<div class="sub">paper stack · generated {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(d['ts']))}
 · auto-refresh 60s · <a href="/">dashboard</a></div>""")

    # processes
    p = d["procs"]
    H.append("<section><h2>Processes (supervisord)</h2>")
    if p["up"]:
        H.append("<table><tr><th></th><th>program</th><th>state</th><th>uptime</th><th>pid</th>"
                 "<th>restarts</th></tr>")
        for r in p["rows"]:
            H.append(f"<tr><td>{_c(r['chip'])}</td><td>{r['name']}</td><td>{r['state']}</td>"
                     f"<td>{_fmt_age(r['uptime_s'])}</td><td>{r['pid']}</td><td>{r['restarts']}</td></tr>")
        H.append("</table>")
    else:
        H.append(f"<p>{_c(RED)} supervisord unreachable at 127.0.0.1:9100 — <span class='dim'>{p['err']}</span>"
                 "<br><span class='note'>file-based health below still renders; start with `make ops-start`"
                 "</span></p>")
    H.append("</section>")

    # capture
    c = d["capture"]
    cap_chip = chip_for("capture_hb", c["hb_age_s"])
    H.append("<section><h2>Capture</h2><table>")
    H.append(f"<tr><td>{_c(cap_chip)}</td><td>heartbeat</td><td>{_fmt_age(c['hb_age_s'])} ago</td>"
             f"<td class='note'>{threshold_note('capture_hb', 'flushes every 30s')}</td></tr>")
    H.append(f"<tr><td>{_c(chip_for('parquet', c['newest_parquet_age_s']))}</td><td>newest parquet</td>"
             f"<td>{_fmt_age(c['newest_parquet_age_s'])} ago · {c['parquets_last_hour']} files last hour</td>"
             f"<td class='note'>{threshold_note('parquet', 'today+yesterday partitions')}</td></tr>")
    H.append(f"<tr><td>{_c(chip_for('universe', c['universe_age_s']))}</td><td>universe</td>"
             f"<td>{c['universe_rows']} tokens · {_fmt_age(c['universe_age_s'])} ago</td>"
             f"<td class='note'>{threshold_note('universe', 'refreshes every 30min')}</td></tr>")
    run = (f"polls {c['polls']} · book rows {c['book_rows']} · trade rows {c['trade_rows']} · "
           f"tokens {c['tokens']}") if c["polls"] is not None else "no run stats yet"
    H.append(f"<tr><td>{_c(GREY)}</td><td>run stats</td><td>{run}</td><td></td></tr>")
    disk_chip = (GREY if c["disk_free_gb"] is None else
                 (GREEN if c["disk_free_gb"] > 20 else (AMBER if c["disk_free_gb"] > 5 else RED)))
    sz = f"{c['parquet_files_2d']} parquet files (2d partitions)"
    free = "—" if c["disk_free_gb"] is None else f"{c['disk_free_gb']:.1f} GB free"
    H.append(f"<tr><td>{_c(disk_chip)}</td><td>disk</td><td>{sz} · {free}</td><td></td></tr>")
    H.append("</table></section>")

    # driver
    v = d["driver"]
    H.append("<section><h2>Strategy driver</h2><table>")
    H.append(f"<tr><td>{_c(chip_for('driver_hb', v['hb_age_s']))}</td><td>heartbeat</td>"
             f"<td>{_fmt_age(v['hb_age_s'])} ago · live={v['live']}</td>"
             f"<td class='note'>{threshold_note('driver_hb', 'writes every 30s')}</td></tr>")
    nd = v["next_due_in_s"]
    H.append(f"<tr><td>{_c(GREY)}</td><td>next fire due</td>"
             f"<td>{'in ' + _fmt_age(nd) if nd and nd > 0 else ('overdue' if nd is not None else '—')}</td>"
             f"<td class='note'>release windows 00:30/06:30/12:30/18:30 UTC</td></tr>")
    H.append("</table><table><tr><th></th><th>arm</th><th>mode</th><th>last fire</th><th>n_views</th>"
             "<th>n_orders</th><th>filled</th><th>open</th><th>realized $</th></tr>")
    for a in v["arms"]:
        f = a["fire"]
        if a["err"]:
            chip, last, views, orders, filled = RED, f"ERROR {_fmt_age(_age(a['err']['ts']))} ago", "—", "—", "—"
            note = a["err"]["error"][:60]
        elif f:
            fa = _age(f["ts"])
            chip = GREEN if fa <= 8 * 3600 else AMBER   # 4 windows/day ⇒ ≤6h10m between fires when healthy
            last, views, orders, filled = f"{_fmt_age(fa)} ago", f.get("n_views", "—"), f["n_orders"], f["filled"]
            note = ""
        else:
            chip, last, views, orders, filled, note = GREY, "no fire logged yet", "—", "—", "—", ""
        rl = a["realized"]
        rl_s = "—" if rl is None else f"<span class='{'pos' if rl >= 0 else 'neg'}'>{rl:+.2f}</span>"
        m = a.get("mode")
        m_cls = {"PAPER": "m-paper", "LIVE": "m-live", "LIVE-LOCKED": "m-locked",
                 "OFF": "m-off"}.get(m, "m-off")
        m_s = f"<span class='mode {m_cls}'>{m}</span>" if m else "—"
        H.append(f"<tr><td>{_c(chip)}</td><td>{a['name']}</td><td>{m_s}</td><td>{last}</td>"
                 f"<td>{views}</td><td>{orders}</td><td>{filled}</td><td>{a['open']}</td><td>{rl_s} "
                 f"<span class='note'>{note}</span></td></tr>")
        if a.get("rule"):
            H.append(f"<tr><td></td><td colspan=8 class='note'>{a['rule']}</td></tr>")
    H.append("</table><div class='note'>n_views = candidates PRICED in the pass — n_views&gt;0 with 0 orders"
             " means “evaluated, no edge”; n_views absent/0 outside fire windows is normal; an arm stuck at"
             " “no fire logged yet” past a release window means its substrate gate failed closed.</div></section>")

    # exit monitor
    e = d["exitmon"]
    H.append("<section><h2>Exit monitor</h2><table>")
    em_note = threshold_note("exitmon_pass", "5-min cadence; paper mode sweeps every positions file"
                             " read-only, places nothing (paper TP/SL exits run inside the strategy"
                             " at fire time; pause ≠ unmanaged — disabled rows still swept)")
    H.append(f"<tr><td>{_c(chip_for('exitmon_pass', e['lastpass_age_s']))}</td><td>last pass</td>"
             f"<td>{_fmt_age(e['lastpass_age_s'])} ago</td>"
             f"<td class='note'>{em_note}</td></tr>")
    if e["last_line"]:
        H.append(f"<tr><td>{_c(GREY)}</td><td>last output</td><td colspan=2 class='note'>{e['last_line']}</td></tr>")
    H.append("</table></section>")

    # calibration
    cal = d["calib"]
    H.append("<section><h2>Calibration substrate</h2>")
    if cal["tenant"] is None:
        H.append("<p class='dim'>no tenant substrate found (desk-only tree) — n/a</p>")
    else:
        H.append("<table>")
        for r in cal["rows"]:
            H.append(f"<tr><td>{_c(r['chip'])}</td><td>{r['label']}</td><td>{r['value']}</td>"
                     f"<td class='note'>{r['note']}</td></tr>")
        H.append("</table>")
    H.append("</section>")

    # quota + pnl
    q, pnl = d["quota"], d["pnl"]
    H.append("<section><h2>Open-Meteo quota · paper P&amp;L</h2><table>")
    H.append(f"<tr><td>{_c(q['chip'])}</td><td>OM meter {q['day']}</td>"
             f"<td>{q['used']} / {q['cap']} weighted ({q['pct']:.0f}%)</td>"
             f"<td class='note'>{q['note']}</td></tr>")
    rt = pnl["realized_today"]
    H.append(f"<tr><td>{_c(GREY)}</td><td>realized today</td>"
             f"<td><span class='{'pos' if rt >= 0 else 'neg'}'>{rt:+.2f}</span>"
             f" over {pnl['closed_today']} closed rows</td>"
             f"<td class='note'>open positions: {pnl['open_total']}</td></tr>")
    H.append("</table></section>")

    # live unlock (PASS) — scope + expiry, never a bare countdown (mission-control-v1 stage C)
    pg = d.get("pass_gates") or {"env": False, "pass": None}
    env_on = bool(pg.get("env"))
    H.append("<section id='pass'><h2>Live unlock (PASS)</h2><table>")
    H.append(f"<tr><td>{_c(RED if env_on else GREY)}</td><td>env flag</td>"
             f"<td>LIVE_TRADING_ENABLED {'set' if env_on else 'absent'}</td>"
             f"<td class='note'>desk-wide gate 1 of 2 — never settable from any UI or API</td></tr>")
    p_ = pg.get("pass")
    if p_:
        segs = [str(x) for x in list(p_.get("scope") or []) + list(p_.get("strategies") or [])]
        if p_.get("market_class"):
            segs.append(str(p_["market_class"]))
        try:
            import calendar as _cal
            left_d = (_cal.timegm(time.strptime(str(p_.get("expiry")), "%Y-%m-%d"))
                      - time.time()) / 86400.0
            left, pchip = f"{max(left_d, 0.0):.0f}d left", (GREEN if left_d > 3 else AMBER)
        except Exception:
            left, pchip = "expiry unparsed", AMBER
        H.append(f"<tr><td>{_c(pchip)}</td><td>scoped PASS</td>"
                 f"<td>PASS: {_esc(' · '.join(segs))} · {left}</td>"
                 f"<td class='note'>granted {_esc(p_.get('granted'))} · expires "
                 f"{_esc(p_.get('expiry'))} · {p_.get('n_fills', '—')} fills — gate 2 of 2; "
                 f"unlocks ONLY the named capability/strategies/market class</td></tr>")
    else:
        H.append(f"<tr><td>{_c(GREY)}</td><td>scoped PASS</td><td>none — live locked</td>"
                 f"<td class='note'>live stays paper until the recon evaluator grants a scoped "
                 f"PASS (capability · strategies · market class · expiry) — gate 2 of 2</td></tr>")
    H.append("</table><div class='note'>live-arming is manual by design — the control API can "
             "only flip <code>enabled</code>, never <code>live</code>, a seal, or the env flag."
             "</div></section>")

    # control actions history — the audit JSONL, newest first (seeds labeled)
    au = d.get("audit") or {"total": 0, "rows": []}
    H.append("<section id='audit'><h2>Control actions history</h2>")
    H.append("<div class='note' style='margin-bottom:6px'>newest first · every pause/resume flip "
             "+ the bootstrap seeds (control_audit.jsonl, append-only) · "
             "<a id='tzbtn' href='#'>times: UTC &#x21c4;</a></div>")
    if au["rows"]:
        H.append("<table><tr><th>when</th><th>action</th><th>arm</th><th>prior→new</th>"
                 "<th>effective at</th><th>source</th></tr>")

        def _oo(v):
            return "—" if v is None else ("on" if v else "off")

        for r in au["rows"]:
            seed = bool(r.get("seed"))
            act = ("seed <span class='note'>(bootstrap)</span>" if seed
                   else _esc(r.get("action") or "—"))
            eff = r.get("effective_at")
            eff_s = f"<span data-ts='{int(eff)}'>{_fmt_ts(eff)}</span>" if eff else "—"
            src = "bootstrap" if seed else _esc(r.get("source_ip") or "—")
            ua = (r.get("ua") or "")[:32]
            if ua and not seed:
                src += f" <span class='note'>{_esc(ua)}</span>"
            H.append(f"<tr><td><span data-ts='{int(r.get('ts') or 0)}'>{_fmt_ts(r.get('ts'))}"
                     f"</span></td><td>{act}</td><td>{_esc(r.get('arm') or '—')}</td>"
                     f"<td>{_oo(r.get('prior'))} → {_oo(r.get('new'))}</td>"
                     f"<td>{eff_s}</td><td>{src}</td></tr>")
        H.append("</table>")
        more = au["total"] - len(au["rows"])
        H.append(f"<div class='note'>{au['total']} entries"
                 + (f" · showing newest {len(au['rows'])}" if more > 0 else "") + "</div>")
    else:
        H.append("<p class='dim'>no control actions logged yet</p>")
    H.append("</section>")

    H.append("<footer>stop/start: make ops-stop · make ops-start — supervisor web UI "
             "<a href='http://127.0.0.1:9100'>127.0.0.1:9100</a> · "
             "<a href='/glossary'>glossary</a></footer>"
             + _TZ_SCRIPT.replace("__DESK_TZ__", os.environ.get("DESK_TZ", "Asia/Dubai"))
             + "</body></html>")
    return "\n".join(H)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(REPO, "dashboard", "ops.html"))
    a = ap.parse_args(argv)
    d = collect()
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w") as f:
        f.write(render(d))
    os.replace(tmp, a.out)
    print(f"[ops] {a.out} · supervisord {'UP' if d['procs']['up'] else 'DOWN'} · "
          f"capture hb {_fmt_age(d['capture']['hb_age_s'])} · driver hb {_fmt_age(d['driver']['hb_age_s'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
