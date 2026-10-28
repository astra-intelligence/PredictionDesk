"""desk/control_plane.py — pause/resume mechanics for the dashboard control API (mission-control-v1).

The ONE write path the control API has into the system: flip a config row's `enabled` field.
Design per context/ui-redesign-2026-10-26.md v2 REVISION §Control plane (and the OpenSpec change
openspec/changes/mission-control-v1/):

  • flip_enabled() — flock + atomic replace of the DESK_STRATEGY_CONFIG file, flipping `enabled`
    ONLY. Structurally unable to escalate, three ways: (1) the edit assigns row["enabled"] and
    nothing else; (2) a pre-write JSON diff must show exactly the targeted strategies[i].enabled
    paths or the write is refused; (3) after the write, the registry loader must still accept
    EVERY config row or the original bytes are rolled back (fail-closed). This module has NO code
    that writes `live`, `config_hash`, LIVE_TRADING_ENABLED, or any other field/env — and no
    reseal exists anywhere in the API (an HTTP reseal would launder tampering; `enabled` is
    outside the seal, so none is needed). Live-arming stays the manual two-gate ritual.
  • Audit JSONL — append-only, fsync'd per line, at $DESK_DATA_DIR/audit/control_audit.jsonl
    (DESK_DATA_DIR default ~/polyedge-data, the ops data home — NOT runtime.paths.data_root(),
    whose default is the in-repo desk/ tree; the audit log lives with logs/config outside any git
    tree). One line per applied flip: {ts, action, arm, field:"enabled", prior, new, source_ip,
    ua, effective_at, row_before, row_after}. No line for a no-op flip — a no-op must not reset
    the mode-since clock. A flip that cannot be audited is rolled back (no unaudited mutation).
  • seed_audit() — day-one mode-since: when the audit file is missing/empty, one
    {action:"seed", seed:true} entry per config row, effective_at = best-known mode start
    (earliest driver-log line ts, else config file mtime). Idempotent. Runs at dashboard-server
    start; also `python3 -m desk.control_plane seed`.
  • next_fire_ts()/fire_in_progress() — "effective at the next fire window": flips land at the
    driver's next per-loop registry reload, but their visible effect is the next fire window,
    computed from the row's schedule exactly like strategy_driver._next_due (the eval gate pins
    the two against each other). The heartbeat only writes BETWEEN loop iterations, so a stale-
    but-not-dead heartbeat (75s..30min) means a fire/settle pass is in flight.
  • mode_of()/live_gates()/mode_since() — the v2 4-state mode chip derivation (OFF / PAPER /
    LIVE / LIVE-LOCKED) and the per-arm "mode since" read from the audit log, shared by
    /api/health and the UI so chips and API can never disagree.
"""
from __future__ import annotations

import calendar
import fcntl
import json
import os
import time
from typing import Dict, List, Optional

from desk.signals import registry as R

AUDIT_FILENAME = "control_audit.jsonl"
FIRE_BUSY_MIN_S = 75.0       # heartbeat older than ~2.5 idle loops ⇒ the loop is blocked in a fire
FIRE_BUSY_MAX_S = 1800.0     # ... but older than the driver_hb red threshold ⇒ dead driver, not a fire


class ControlError(Exception):
    """A refused control action: .status (HTTP) + .payload (JSON body). Raised, never half-applied."""

    def __init__(self, status: int, payload: dict):
        super().__init__(str(payload.get("error", "control error")))
        self.status = status
        self.payload = payload


# ── paths ──────────────────────────────────────────────────────────────────────
def control_data_root() -> str:
    """$DESK_DATA_DIR when set, else ~/polyedge-data (matches the supervisord/build_ops data home)."""
    return os.environ.get("DESK_DATA_DIR") or os.path.join(os.path.expanduser("~"), "polyedge-data")


def audit_path() -> str:
    return os.path.join(control_data_root(), "audit", AUDIT_FILENAME)


def _driver_log_path() -> str:
    return os.path.join(control_data_root(), "logs", "strategydriver.out.log")


# ── audit (append-only, fsync'd) ───────────────────────────────────────────────
def append_audit(entry: dict) -> None:
    p = audit_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_audit() -> List[dict]:
    out: List[dict] = []
    try:
        with open(audit_path()) as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except OSError:
        pass
    return out


def mode_since() -> Dict[str, int]:
    """arm -> effective_at of its LAST audit entry (flip or seed) — the mode-since source."""
    out: Dict[str, int] = {}
    for r in read_audit():
        arm, eff = r.get("arm"), r.get("effective_at")
        if arm and eff is not None:
            out[arm] = int(eff)
    return out


def _earliest_driver_log_ts() -> Optional[int]:
    """First parseable JSON `ts` in the driver log — the best-known stack bring-up moment.
    (The current log file only; rotation bounds how far back "best-known" reaches.)"""
    try:
        with open(_driver_log_path()) as f:
            for i, ln in enumerate(f):
                if i > 500:
                    break
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if isinstance(r, dict) and r.get("ts"):
                    return int(r["ts"])
    except OSError:
        pass
    return None


def seed_audit(config_path: Optional[str] = None) -> int:
    """Day-one mode-since: when the audit file is missing/EMPTY, write one seed entry per config
    row (effective_at = earliest driver-log line ts, else config mtime). Idempotent — a non-empty
    audit file means history exists and seeding would lie. Returns entries written."""
    try:
        if os.path.getsize(audit_path()) > 0:
            return 0
    except OSError:
        pass
    path = config_path or R.config_path()
    try:
        with open(path) as f:
            rows = json.load(f).get("strategies", [])
    except Exception:
        return 0
    if not rows:
        return 0
    start = _earliest_driver_log_ts()
    if start is None:
        try:
            start = int(os.path.getmtime(path))
        except OSError:
            start = int(time.time())
    now = int(time.time())
    n = 0
    for row in rows:
        append_audit({"ts": now, "action": "seed", "seed": True, "arm": row.get("name"),
                      "field": "enabled", "prior": None, "new": bool(row.get("enabled")),
                      "source_ip": None, "ua": None, "effective_at": int(start),
                      "row_before": None, "row_after": row})
        n += 1
    return n


# ── schedule / mode helpers ────────────────────────────────────────────────────
def next_fire_ts(schedule: Optional[dict], now: float) -> int:
    """The row's next fire window. MIRRORS strategy_driver._next_due (same branches, same
    defaults) but takes a plain schedule dict so the control plane needs no driver import;
    run_eval's control_api section pins the two against each other so they cannot drift."""
    sched = schedule or {}
    if sched.get("type") == "release_times":
        best = None
        for hm in sched.get("times_utc", ["02:15", "08:15", "14:15", "20:15"]):
            hh, mm = (int(x) for x in hm.split(":"))
            g = time.gmtime(now)
            ts = calendar.timegm((g.tm_year, g.tm_mon, g.tm_mday, hh, mm, 0, 0, 0, 0))
            if ts <= now:
                ts += 86400
            best = ts if best is None else min(best, ts)
        return int(best)
    return int(now + float(sched.get("secs", 21600)))


def _utc(ts: Optional[int]) -> Optional[str]:
    return None if ts is None else time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def fire_in_progress() -> bool:
    """Heuristic (v2 §5): the driver heartbeat writes only BETWEEN loop iterations, and a fire can
    legally run FIRE_DEADLINE_S=1200s — so a heartbeat aged 75s..30min means a fire/settle pass is
    in flight (older means the driver is dead, which the driver_hb chip reports instead)."""
    from desk.runtime.paths import data_path
    try:
        with open(data_path("driver", "_heartbeat")) as f:
            hb = json.load(f)
        age = time.time() - float(hb.get("ts") or 0)
    except Exception:
        return False
    return FIRE_BUSY_MIN_S < age <= FIRE_BUSY_MAX_S


def row_for(name: str, config_path: Optional[str] = None) -> Optional[dict]:
    try:
        with open(config_path or R.config_path()) as f:
            rows = json.load(f).get("strategies", [])
    except Exception:
        return None
    return next((r for r in rows if r.get("name") == name), None)


def effective_params(row: dict) -> dict:
    """Base defaults merged UNDER row params — the loader's merge, so live-routing is judged on
    what would actually run. Falls back to the raw row params when the base is unavailable."""
    try:
        base = R.base_specs().get(row.get("strategy", row.get("name")))
        if base is not None:
            return {**(base.params or {}), **(row.get("params") or {})}
    except Exception:
        pass
    return dict(row.get("params") or {})


def effective_schedule(row: dict) -> Optional[dict]:
    if row.get("schedule"):
        return row["schedule"]
    try:
        base = R.base_specs().get(row.get("strategy", row.get("name")))
        return dict(base.schedule) if base is not None and base.schedule else None
    except Exception:
        return None


def is_live_routed(row: dict) -> bool:
    return bool(effective_params(row).get("live"))


def live_gates() -> dict:
    """The two live gates as the UI must show them: the env flag and the scoped PASS (or None).
    Read-only — this module can report the gates, never move them."""
    env = os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"
    try:
        from desk.live.executor import pass_scope
        p = pass_scope()
    except Exception:
        p = None
    return {"env": env, "pass": p}


def mode_of(enabled: bool, live_routed: bool, env_live: bool, pass_covers_arm: bool) -> str:
    """v2 'Mode chips — 4 states': disabled ⇒ OFF (whatever the routing); enabled paper ⇒ PAPER;
    enabled live-routed ⇒ LIVE only when BOTH gates hold for this arm, else LIVE-LOCKED."""
    if not enabled:
        return "OFF"
    if not live_routed:
        return "PAPER"
    return "LIVE" if (env_live and pass_covers_arm) else "LIVE-LOCKED"


# ── rule lines (v2 'describe hook': tenant manifest hook describe(params) -> str) ──
def _fmt_span(secs: float) -> str:
    s = int(secs)
    if s % 3600 == 0 and s >= 3600:
        return f"{s // 3600} h"
    if s % 60 == 0 and s >= 60:
        return f"{s // 60} m"
    return f"{s} s"


def schedule_text(schedule: Optional[dict]) -> Optional[str]:
    """The framework-owned half of a rule line (schedules are framework semantics, so tenants
    never restate them): 'Fires 00:30/06:30/12:30/18:30 UTC' or 'Every 6 h'."""
    if not schedule:
        return None
    if schedule.get("type") == "release_times":
        times = schedule.get("times_utc") or ["02:15", "08:15", "14:15", "20:15"]
        return "Fires " + "/".join(times) + " UTC"
    return "Every " + _fmt_span(float(schedule.get("secs", 21600)))


def describe_fallback(params: dict) -> str:
    """Generic rule line for arms whose tenant ships no describe hook: a labeled line of the
    params the FRAMEWORK itself reads (AGENTS.md 'Params the framework itself reads') plus a
    count of the tenant-private rest — honest, never a guessed translation."""
    p = dict(params or {})
    segs: List[str] = []
    if p.get("qty") is not None:
        segs.append(f"{float(p['qty']):g} sh/clip")
    if p.get("budget") is not None:
        segs.append(f"${float(p['budget']):g} budget")
    if p.get("min_notional") is not None:
        segs.append(f"${float(p['min_notional']):g} min/order")
    tp, sl = p.get("exit_tp"), p.get("exit_sl")
    if tp is not None or sl is not None:
        parts = ([f"+{round(float(tp) * 100):g}%"] if tp is not None else []) + \
                ([f"−{round(float(sl) * 100):g}%"] if sl is not None else [])
        segs.append("exits " + " / ".join(parts) + " at fire time")
    else:
        segs.append("holds to settlement")
    if p.get("gate") == "llm":
        segs.append("LLM gate")
    if p.get("mode") == "maker":
        segs.append(f"maker quotes ({_fmt_span(float(p.get('maker_validity_s', 3600)))} validity)")
    shown = {"qty", "budget", "min_notional", "exit_tp", "exit_sl", "gate", "mode",
             "maker_validity_s", "live", "strategy_id", "_config_hash"}
    rest = [k for k in p if k not in shown]
    if rest:
        segs.append(f"+{len(rest)} strategy params")
    return " · ".join(segs) if segs else "no params"


def rule_line(name: str, strategy: str, params: dict, schedule: Optional[dict]) -> str:
    """The card's plain-English rule: schedule_text + the tenant's describe(params) (manifest
    hook `describe`; builtins may define describe() beside their entrypoint), falling back to
    describe_fallback. No tenant strings live here — the hook owns the translation."""
    fn = None
    try:
        fn = R.hook_for(name, "describe") or R.hook_for(strategy, "describe")
        if fn is None:
            base = R.base_specs().get(strategy)
            go = getattr(base, "generate_orders", None)
            if go is not None:
                import sys as _sys
                fn = getattr(_sys.modules.get(go.__module__), "describe", None)
    except Exception:
        fn = None
    txt = None
    if fn is not None:
        try:
            txt = str(fn(dict(params or {}))) or None
        except Exception as e:
            print(f"[control] describe hook for '{name}' failed ({e!r}) — labeled fallback",
                  flush=True)
    if not txt:
        txt = describe_fallback(params)
    st = schedule_text(schedule)
    return f"{st} · {txt}" if st else txt


# ── the enabled flip (the API's one write path) ────────────────────────────────
def _changed_paths(a, b, path: str = "") -> List[str]:
    """JSON structural diff → dotted paths that differ. The structural guard's eyes."""
    if type(a) is not type(b):
        return [path or "<root>"]
    if isinstance(a, dict):
        out: List[str] = []
        for k in sorted(set(a) | set(b)):
            kp = f"{path}.{k}" if path else str(k)
            if k not in a or k not in b:
                out.append(kp)
            else:
                out += _changed_paths(a[k], b[k], kp)
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}.<len>"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += _changed_paths(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [path]


def _write_config_bytes(path: str, data: bytes) -> None:
    """Atomic byte-level write preserving the file mode — the rollback path."""
    tmp = path + ".control.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    except OSError:
        pass
    os.replace(tmp, path)


def flip_enabled(arms: List[str], enable: bool, action: str,
                 config_path: Optional[str] = None,
                 source_ip: Optional[str] = None, ua: Optional[str] = None) -> dict:
    """Flip `enabled` on the named config rows — THE control API's only mutation.

    flock(<config>.control.lock) → read → assign row["enabled"] (nothing else) → structural
    JSON-diff guard (exactly the targeted strategies[i].enabled paths may differ) → atomic
    replace → loader verify (every config row still accepted, else byte rollback + 500) →
    one audit line per applied flip (audit failure also rolls back). Rows already in the target
    state are reported `unchanged` with no write and no audit line. Raises ControlError."""
    path = config_path or R.config_path()
    if not os.path.exists(path):
        raise ControlError(500, {"error": f"strategy config not found: {path}"})
    with open(path + ".control.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            with open(path, "rb") as f:
                orig_bytes = f.read()
            try:
                cfg = json.loads(orig_bytes.decode())
            except Exception as e:
                raise ControlError(500, {"error": f"config unreadable: {e!r}"})
            rows = cfg.get("strategies", [])
            by_name = {r.get("name"): r for r in rows}
            missing = sorted(set(arms) - set(by_name))
            if missing:
                raise ControlError(404, {"error": f"unknown arm(s): {missing}",
                                         "known": sorted(n for n in by_name if n)})
            new_cfg = json.loads(orig_bytes.decode())      # independent working copy
            now = time.time()
            results: List[dict] = []
            flipped: List[str] = []
            targets = set(arms)
            for i, row in enumerate(new_cfg.get("strategies", [])):
                name = row.get("name")
                if name not in targets:
                    continue
                prior = bool(row.get("enabled"))
                if prior == bool(enable):
                    results.append({"arm": name, "prior": prior, "new": prior,
                                    "unchanged": True, "effective_at": None,
                                    "effective_at_utc": None})
                    continue
                row["enabled"] = bool(enable)              # ← the ONLY field this API ever writes
                eff = next_fire_ts(effective_schedule(by_name[name]), now)
                flipped.append(f"strategies[{i}].enabled")
                results.append({"arm": name, "prior": prior, "new": bool(enable),
                                "unchanged": False, "effective_at": eff,
                                "effective_at_utc": _utc(eff)})
            busy = fire_in_progress()
            out = {"ok": True, "action": action, "results": results, "config": path,
                   "fire_in_progress": busy,
                   "note": ("fire in progress — flips land after it completes, "
                            "effective at the stated window") if busy else
                           "effective at the next fire window (driver reloads config every loop)"}
            if not flipped:                                # all no-ops: nothing to write or audit
                out["noop"] = True
                return out
            diff = _changed_paths(cfg, new_cfg)
            if sorted(diff) != sorted(flipped):            # structural guard: enabled-only edits
                raise ControlError(500, {"error": "refusing write: edit would touch more than "
                                                  "`enabled`", "changed_paths": diff})
            payload = (json.dumps(new_cfg, indent=1) + "\n").encode()   # registry reseal's format
            _write_config_bytes(path, payload)
            try:                                           # fail-closed loader verify
                loaded = {s.name for s in R.load_registry(path, include_disabled=True)}
                refused = [r.get("name") for r in new_cfg.get("strategies", [])
                           if r.get("name") not in loaded]
            except Exception as e:
                refused = [f"loader raised: {e!r}"]
            if refused:
                _write_config_bytes(path, orig_bytes)
                raise ControlError(500, {"error": "loader verify failed — config rolled back",
                                         "refused": refused})
            try:                                           # no unaudited mutation survives
                for res in results:
                    if res["unchanged"]:
                        continue
                    append_audit({"ts": int(now), "action": action, "arm": res["arm"],
                                  "field": "enabled", "prior": res["prior"], "new": res["new"],
                                  "source_ip": source_ip, "ua": ua,
                                  "effective_at": res["effective_at"],
                                  "row_before": by_name[res["arm"]],
                                  "row_after": next(r for r in new_cfg["strategies"]
                                                    if r.get("name") == res["arm"])})
            except Exception as e:
                _write_config_bytes(path, orig_bytes)
                try:
                    append_audit({"ts": int(time.time()), "action": "rollback", "arm": None,
                                  "field": "enabled", "prior": None, "new": None,
                                  "source_ip": source_ip, "ua": ua, "effective_at": None,
                                  "row_before": None, "row_after": None,
                                  "error": f"audit append failed mid-{action}: {e!r}"})
                except Exception:
                    pass
                raise ControlError(500, {"error": f"audit append failed — config rolled back "
                                                  f"({e!r})"})
            return out
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def pause_all(config_path: Optional[str] = None,
              source_ip: Optional[str] = None, ua: Optional[str] = None) -> dict:
    """Disable every currently-enabled row (kill-switch taxonomy: 'pause' = no new entries;
    settlement + the exit monitor keep managing open positions, disabled rows included)."""
    path = config_path or R.config_path()
    try:
        with open(path) as f:
            rows = json.load(f).get("strategies", [])
    except Exception as e:
        raise ControlError(500, {"error": f"config unreadable: {e!r}"})
    arms = [r.get("name") for r in rows if r.get("enabled")]
    if not arms:
        return {"ok": True, "action": "pause-all", "results": [], "noop": True,
                "config": path, "fire_in_progress": fire_in_progress(),
                "note": "no enabled rows — nothing to pause"}
    return flip_enabled(arms, False, "pause-all", config_path=path,
                        source_ip=source_ip, ua=ua)


def _cli(argv: List[str]) -> int:
    """`python3 -m desk.control_plane <cmd>`: seed — seed the audit log (idempotent);
    since — print per-arm mode-since from the audit log."""
    cmd = argv[0] if argv else "seed"
    if cmd == "seed":
        n = seed_audit()
        print(f"[control] seeded {n} audit entr{'y' if n == 1 else 'ies'} -> {audit_path()}"
              if n else f"[control] audit already has history -> {audit_path()} (no-op)")
        return 0
    if cmd == "since":
        print(json.dumps({k: _utc(v) for k, v in sorted(mode_since().items())}, indent=1))
        return 0
    print(f"unknown command {cmd!r} — use: seed | since")
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
