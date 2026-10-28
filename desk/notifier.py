"""desk/notifier.py — the red-alert notifier (mission-control-v1 stage C; ui-redesign v2 §Added).

A 5-minute loop on GET /api/health that pushes to ntfy.sh (and optionally Telegram) when the desk
needs a human, and pushes a recovery line when a condition clears. One push per condition
TRANSITION, never per loop — a tiny state file ($DESK_DATA_DIR/notifier_state.json, default
~/polyedge-data) remembers what is already alerted across restarts. It also covers the
dashboard-dead blind spot no in-page strip can: the loop alerts when /api/health itself is
unreachable twice in a row.

Conditions (stable ids in the state file):
  capture_stale        capture heartbeat older than THRESHOLDS red (or missing)
  book_flow_stalled    capture heartbeat FRESH but book_rows unchanged across 2 consecutive checks
                       while the capture process is running — the Zscaler/proxy signature of
                       2026-10-27 (process healthy, data not flowing). Suppressed only when
                       supervisord reports capture deliberately stopped.
  driver_stale         driver heartbeat older than red (or missing)
  exitmon_stale        exit-monitor last pass older than red (or missing)
  substrate:<label>    a substrate error-series' newest day within 2 d of the fail-closed red age
                       (or a required series MISSING) — the arms brick when it crosses
  arm_error:<name>     an arm's last fire errored (newer than its last good fire). The error TEXT
                       stays out of the push (it can carry market/order detail) — read the log.
  pass_expiring        a live PASS exists and expires in under 3 days
  health_unreachable   GET /api/health failed twice in a row (dashboard dead / collectors broken)

Pushes carry condition + value + threshold + action; thresholds come from the health payload's
THRESHOLDS dict (never hardcoded here), action lines from desk.glossary — the push says what the
glossary page says. NEVER in a push: wallet values, order ids, strategy params, error payloads.

Delivery: POST https://ntfy.sh/$NTFY_TOPIC (no account, no secret in the repo). NTFY_TOPIC comes
from the environment or from <data>/config/notifier.env; when neither has one, a long random topic
is generated ONCE into that file — the topic is the only subscription key, treat it like a
password. Optional Telegram when TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are present in the env.

Run:  python3 -m desk.notifier          # the supervised loop (ops/supervisord.conf [program:notifier])
      python3 -m desk.notifier --once   # one check, print the verdict, exit
      python3 -m desk.notifier --test   # one synthetic push through the real channels, then exit
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import secrets
import time
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from desk.control_plane import control_data_root
from desk.glossary import action_for, fmt_span

INTERVAL_S = int(os.environ.get("NOTIFIER_INTERVAL_S", "300"))
HEALTH_URL = os.environ.get(
    "NOTIFIER_HEALTH_URL",
    f"http://127.0.0.1:{os.environ.get('DASH_PORT', '8788')}/api/health")
SUBSTRATE_WARN_S = 2 * 86400     # alert this close to the substrate fail-closed red bound
PASS_WARN_D = 3.0                # alert when a PASS has under this many days left
STALL_CHECKS = 2                 # book_rows unchanged across this many consecutive checks ⇒ stalled
FAILS_TO_ALERT = 2               # /api/health failures in a row before the blind-spot alert


# ── paths / env / state ───────────────────────────────────────────────────────────────────────────
def state_path() -> str:
    return os.path.join(control_data_root(), "notifier_state.json")


def env_file_path() -> str:
    return os.path.join(control_data_root(), "config", "notifier.env")


def load_env_file() -> None:
    """notifier.env KEY=VALUE lines become env DEFAULTS (a real env var always wins)."""
    try:
        with open(env_file_path()) as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


def ensure_ntfy_topic() -> str:
    """The ntfy topic: env > notifier.env > generate a long random one ONCE into notifier.env."""
    t = os.environ.get("NTFY_TOPIC")
    if t:
        return t
    t = "polyedge-" + secrets.token_hex(16)
    p = env_file_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a") as f:
        f.write(f"NTFY_TOPIC={t}\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    os.environ["NTFY_TOPIC"] = t
    print(f"[notifier] generated NTFY topic (subscribe to it in the ntfy app, keep it private) "
          f"-> {p}\n[notifier] NTFY_TOPIC={t}", flush=True)
    return t


def load_state() -> dict:
    try:
        with open(state_path()) as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save_state(state: dict) -> None:
    p = state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


# ── condition evaluation (pure on a health payload + counters — the eval gate feeds synthetics) ───
def _thr(health: dict, key: str) -> Optional[Tuple[float, float]]:
    t = (health.get("thresholds") or {}).get(key)
    try:
        return (float(t[0]), float(t[1])) if t else None
    except Exception:
        return None


def _capture_running(health: dict) -> bool:
    """False only when supervisord POSITIVELY says capture is not RUNNING (deliberate stop) —
    unknown/unreachable supervisord never suppresses the alert."""
    procs = health.get("procs") or {}
    if not procs.get("up"):
        return True
    for r in procs.get("rows") or []:
        if str(r.get("name", "")).split(":")[-1] == "capture":
            return r.get("state") == "RUNNING"
    return True


def update_flow_counters(health: dict, state: dict) -> None:
    """book_rows is the capture heartbeat's cumulative row counter: unchanged across checks while
    the heartbeat stays fresh = the poll loop runs but no data arrives (the proxy signature)."""
    rows = (health.get("capture") or {}).get("book_rows")
    if rows is None or rows != state.get("book_rows"):
        state["book_stall"] = 0
    else:
        state["book_stall"] = int(state.get("book_stall", 0)) + 1
    state["book_rows"] = rows


def _date_age_s(datestr: Optional[str], now: float) -> Optional[float]:
    if not datestr:
        return None
    try:
        return max(0.0, now - calendar.timegm(time.strptime(str(datestr), "%Y-%m-%d")))
    except Exception:
        return None


def _age_alert(out: Dict[str, str], health: dict, cid: str, key: str, age: Optional[float],
               what: str) -> None:
    t = _thr(health, key)
    if t is None:
        return
    red = t[1]
    if age is None or age > red:
        val = "missing" if age is None else f"{fmt_span(age)} old"
        out[cid] = f"{what} {val} (red > {fmt_span(red)}). Action: {action_for(key)}."


def active_conditions(health: dict, state: dict, now: Optional[float] = None) -> Dict[str, str]:
    """health payload + flow counters → {condition_id: push message}. Stateless beyond the
    book-flow counters already folded into `state` by update_flow_counters."""
    now = now if now is not None else time.time()
    out: Dict[str, str] = {}
    cap = health.get("capture") or {}
    _age_alert(out, health, "capture_stale", "capture_hb", cap.get("hb_age_s"),
               "Capture heartbeat")
    t = _thr(health, "capture_hb")
    hb = cap.get("hb_age_s")
    if (t is not None and hb is not None and hb <= t[1]
            and int(state.get("book_stall", 0)) >= STALL_CHECKS and _capture_running(health)):
        n = int(state["book_stall"])
        out["book_flow_stalled"] = (
            f"Book flow stalled: capture heartbeat fresh ({fmt_span(hb)}) but book_rows stuck at "
            f"{cap.get('book_rows')} across {n} consecutive checks (~{fmt_span(n * INTERVAL_S)}) — "
            f"the proxy/Zscaler signature: process healthy, data not flowing. "
            f"Action: check the network path/proxy, then restart capture.")
    _age_alert(out, health, "driver_stale", "driver_hb",
               (health.get("driver") or {}).get("hb_age_s"), "Driver heartbeat")
    _age_alert(out, health, "exitmon_stale", "exitmon_pass",
               (health.get("exitmon") or {}).get("lastpass_age_s"), "Exit-monitor last pass")
    ts = _thr(health, "substrate_series")
    if ts is not None:
        red = ts[1]
        for r in (health.get("calib") or {}).get("rows") or []:
            if r.get("kind") != "series":
                continue
            label = str(r.get("label") or "substrate")
            age = _date_age_s(r.get("newest"), now)
            if age is None:
                if r.get("chip") == "bad":
                    out[f"substrate:{label}"] = (
                        f"Substrate series '{label}' MISSING — its lane fails closed "
                        f"(gate: {fmt_span(red)}). Action: {action_for('substrate_series')}.")
            elif age >= red - SUBSTRATE_WARN_S:
                out[f"substrate:{label}"] = (
                    f"Substrate '{label}' newest {r.get('newest')} ({fmt_span(age)} old) — within "
                    f"{fmt_span(SUBSTRATE_WARN_S)} of the {fmt_span(red)} fail-closed gate. "
                    f"Action: {action_for('substrate_series')}.")
    for a in health.get("arms") or []:
        err = a.get("last_error")
        if err:
            ets = err.get("ts")
            when = (time.strftime("%m-%d %H:%M UTC", time.gmtime(ets)) if ets else "recently")
            out[f"arm_error:{a.get('name')}"] = (
                f"Arm {a.get('name')} last fire errored ({when}); error text withheld from this "
                f"push. Action: read strategydriver.out.log.")
    p = (health.get("live_lock") or {}).get("pass")
    if p and p.get("expiry"):
        try:
            exp = calendar.timegm(time.strptime(str(p["expiry"]), "%Y-%m-%d"))
        except Exception:
            exp = None
        if exp is not None:
            left = (exp - now) / 86400.0
            if left < PASS_WARN_D:
                scope = " · ".join(list(p.get("scope") or []) + list(p.get("strategies") or [])
                                   + ([p["market_class"]] if p.get("market_class") else []))
                out["pass_expiring"] = (
                    f"Live PASS ({scope}) expires {p['expiry']} — {max(left, 0.0):.1f} d left "
                    f"(< {PASS_WARN_D:g} d). Action: rerun the recon evaluator for the scope, or "
                    f"live re-locks at expiry.")
    return out


# ── transitions + delivery ────────────────────────────────────────────────────────────────────────
def check_once(state: dict, fetch: Callable[[], Optional[dict]],
               send: Callable[[str, str, str], None], now: Optional[float] = None) -> str:
    """One loop iteration: fetch → evaluate → push TRANSITIONS only → fold into state.
    Returns a one-line summary for the log. When health is unreachable, every other condition
    keeps its previous state (we cannot evaluate it) — only the blind-spot counter moves."""
    now = now if now is not None else time.time()
    prev: Dict[str, dict] = dict(state.get("active") or {})
    health = fetch()
    if health is None:
        state["health_fails"] = int(state.get("health_fails", 0)) + 1
        n = state["health_fails"]
        if n >= FAILS_TO_ALERT and "health_unreachable" not in prev:
            msg = (f"/api/health unreachable {n} checks in a row ({HEALTH_URL}) — dashboard dead "
                   f"or collectors broken; every other alert is blind until this clears. "
                   f"Action: check dashboard.out.log; restart desk:dashboard.")
            send("alert", "health_unreachable", msg)
            prev["health_unreachable"] = {"since": int(now), "msg": msg}
        state["active"] = prev
        return f"health unreachable ({n} in a row) · active={sorted(prev)}"
    state["health_fails"] = 0
    update_flow_counters(health, state)
    act = active_conditions(health, state, now)
    pushes = 0
    for cid in sorted(act):
        if cid not in prev:
            send("alert", cid, act[cid])
            pushes += 1
    for cid in sorted(prev):
        if cid not in act:
            since = prev[cid].get("since")
            dur = f" after {fmt_span(now - since)}" if since else ""
            send("recovered", cid, f"Recovered: {cid} cleared{dur}.")
            pushes += 1
    state["active"] = {cid: {"since": int(prev.get(cid, {}).get("since", now)), "msg": act[cid]}
                       for cid in act}
    return f"active={len(act)} {sorted(act) if act else '(healthy)'} · pushes={pushes}"


def fetch_health(url: Optional[str] = None, timeout: float = 45.0) -> Optional[dict]:
    """GET /api/health → dict, or None on any failure (non-200, error payload, timeout) —
    a 'collectors unavailable' 503 is as blind as a dead server."""
    try:
        with urllib.request.urlopen(url or HEALTH_URL, timeout=timeout) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read().decode())
        if not isinstance(data, dict) or data.get("error"):
            return None
        return data
    except Exception:
        return None


def push_ntfy(title: str, msg: str, priority: str, tags: str) -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=msg.encode(), method="POST",
        headers={"Title": title, "Priority": priority, "Tags": tags})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()
    return True


def push_telegram(title: str, msg: str) -> bool:
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return False
    body = json.dumps({"chat_id": chat, "text": f"{title}\n{msg}"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{tok}/sendMessage", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()
    return True


def send_push(kind: str, cid: str, msg: str) -> None:
    """Fan a transition out to every configured channel; delivery failure is logged, never fatal
    (the state still records the transition — a broken channel must not re-alert forever)."""
    alert = kind == "alert"
    title = ("PredictionDesk ALERT: " if alert else "PredictionDesk recovered: ") + cid
    sent: List[str] = []
    try:
        if push_ntfy(title, msg, "high" if alert else "default",
                     "rotating_light" if alert else "white_check_mark"):
            sent.append("ntfy")
    except Exception as e:
        print(f"[notifier] ntfy push failed: {e!r}", flush=True)
    try:
        if push_telegram(title, msg):
            sent.append("telegram")
    except Exception as e:
        print(f"[notifier] telegram push failed: {e!r}", flush=True)
    print(f"[notifier] {kind.upper()} {cid} -> {'+'.join(sent) or 'NO CHANNEL DELIVERED'} :: {msg}",
          flush=True)


# ── entrypoint ────────────────────────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="PredictionDesk red-alert notifier")
    ap.add_argument("--once", action="store_true", help="one check, print verdict, exit")
    ap.add_argument("--test", action="store_true",
                    help="send one synthetic test push through the real channels, then exit")
    ap.add_argument("--url", default=None, help="health endpoint override (fixtures/tests)")
    a = ap.parse_args(argv)
    load_env_file()
    topic = ensure_ntfy_topic()
    tg = "on" if (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")) \
        else "off"
    print(f"[notifier] topic ntfy.sh/{topic} · telegram {tg} · every {INTERVAL_S}s · "
          f"{a.url or HEALTH_URL}", flush=True)
    if a.test:
        send_push("alert", "test",
                  "Synthetic test push — notifier delivery check, no real condition. "
                  "If you can read this in the ntfy app, alerting works.")
        return 0
    state = load_state()
    while True:
        summary = check_once(state, lambda: fetch_health(a.url), send_push)
        save_state(state)
        print(f"[notifier] {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} · {summary}",
              flush=True)
        if a.once:
            return 0
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    import sys
    sys.exit(main())
