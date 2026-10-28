"""desk/mcp_server.py — the desk's MCP server: Claude (or any MCP client) sits at the desk.

Stdio MCP server (official `mcp` SDK, MCPServer/FastMCP style) exposing EXACTLY nine tools:

    desk_health      GET /api/health passthrough — the whole operational state as one JSON
    desk_events      GET /api/events — recent activity, newest-first (limit ≤ 60)
    desk_week        GET /api/week — last 7 UTC days of realized P&L + per-arm attribution
    desk_strategies  the per-arm rows from /api/health (mode, since, rule, today, positions)
    desk_explain     plain-language glossary lookup (threshold chips + concepts), fuzzy on
                     name/aka; threshold numbers render from the LIVE health payload
    desk_alerts      the notifier's active conditions + their last transitions (state file)
    desk_pause       POST /api/strategy/<name>/pause   — audited control path
    desk_resume      POST /api/strategy/<name>/resume  — audited; 409 typed-confirm surfaced
    desk_pause_all   POST /api/stack/pause-all         — audited kill switch (pause semantics)

WHAT THIS SERVER DELIBERATELY EXCLUDES (by design, not omission): no live-arming (neither
LIVE_TRADING_ENABLED nor any PASS is readable-writable here beyond what /api/health reports),
no order placement, no config or seal mutation, no reseal. Control flips go EXCLUSIVELY through
the dashboard's HTTP control API with the X-Desk-Control header, so every flip lands in the
append-only audit JSONL exactly like a UI action — this module never imports or calls the
config-writing internals (desk.control_plane / desk.signals), and the eval gate's `mcp_server`
section statically enforces that. Your assistant can watch the desk and stop it; it cannot bet
your money.

Refused control actions are surfaced VERBATIM — 409 (typed-confirm for a live-routed resume),
429 (debounce), 403 (CSRF floor), 404 (unknown arm), 500 — as {"status", "body"} so the client
can read the server's own wording (e.g. the 409 body names the exact confirm_name expected).

Run:   python3 -m desk.mcp_server         # stdio — from the repo root (the venv's python)
Wire:  claude mcp add predictiondesk -- python3 -m desk.mcp_server      (see docs/mcp.md)
Env:   DESK_DASH_URL (default http://127.0.0.1:$DASH_PORT, DASH_PORT default 8788),
       DESK_DATA_DIR (notifier state file home, default ~/polyedge-data).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

from mcp.server.mcpserver import MCPServer

from desk import glossary as G   # read-only vocabulary (labels/meanings/actions/aka + renderers)

server = MCPServer(
    "predictiondesk",
    instructions=(
        "PredictionDesk — read the desk's health, activity, weekly P&L and glossary; "
        "pause/resume strategy arms through the audited control API. No live-arming, no order "
        "placement, no config editing: this surface can watch the desk and stop it, nothing more."
    ),
)


# ── plumbing ───────────────────────────────────────────────────────────────────
def _base() -> str:
    """Dashboard base URL, resolved per call (tests re-point it via env)."""
    url = os.environ.get("DESK_DASH_URL")
    if url:
        return url.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('DASH_PORT', '8788')}"


def _data_root() -> str:
    """$DESK_DATA_DIR else ~/polyedge-data — the SAME default as control_plane.control_data_root,
    restated here because this module is forbidden (eval-gated) from importing the config-writing
    side of the desk; the notifier state file is the only thing read from it."""
    return os.environ.get("DESK_DATA_DIR") or os.path.join(os.path.expanduser("~"),
                                                           "polyedge-data")


def _get(path: str, timeout: float = 45.0) -> dict:
    """GET → parsed JSON passthrough. 200 ⇒ the body verbatim; non-200 ⇒ {status, body};
    unreachable ⇒ {error, url} (never raises into the MCP transport)."""
    url = _base() + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw[:2000]}
        return {"status": e.code, "body": body}
    except Exception as e:
        return {"error": f"dashboard unreachable: {e!r}", "url": url,
                "hint": "is the dashboard running? make ops-start, or "
                        "python3 -m desk.dashboard_server"}


def _control_post(path: str, body: Optional[dict] = None) -> dict:
    """POST through the dashboard's AUDITED control API — the X-Desk-Control header makes the
    flip land in the control audit JSONL exactly like a UI action. Always {status, body}; the
    body is the server's own JSON VERBATIM (409 typed-confirm, 429 debounce, 403, 404, 500)."""
    url = _base() + path
    req = urllib.request.Request(
        url, data=json.dumps(body or {}).encode(), method="POST",
        headers={"X-Desk-Control": "1", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"status": r.status, "body": json.loads(r.read().decode() or "{}")}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw[:2000]}
        return {"status": e.code, "body": parsed}
    except Exception as e:
        return {"error": f"dashboard unreachable: {e!r}", "url": url,
                "hint": "is the dashboard running? make ops-start, or "
                        "python3 -m desk.dashboard_server"}


def _utc(ts) -> Optional[str]:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(ts)))
    except Exception:
        return None


# ── read tools ─────────────────────────────────────────────────────────────────
@server.tool()
def desk_health() -> dict:
    """The desk's whole operational state as one JSON (GET /api/health passthrough, nothing
    trimmed): process table, capture/driver/exit-monitor freshness, forecast-memory (substrate)
    ages, API quota, today's paper P&L, the two live gates (env flag + scoped PASS), per-arm rows
    (4-state mode OFF/PAPER/LIVE/LIVE-LOCKED, mode-since, open positions, last fire, today
    counts, plain-English rule line) and the THRESHOLDS dict the chips judge by. 30s server-side
    cache; control flips invalidate it."""
    return _get("/api/health")


@server.tool()
def desk_events(limit: int = 50) -> dict:
    """Recent desk activity, newest-first (GET /api/events): strategy fires (with markets
    evaluated / orders / shares), control-API pause/resume flips from the audit log, and
    supervisord process starts/exits. `limit` caps the returned events (1..60 — the API keeps
    the newest 60)."""
    out = _get("/api/events")
    try:
        n = max(1, min(int(limit), 60))
    except Exception:
        n = 50
    if isinstance(out.get("events"), list):
        out["events"] = out["events"][:n]
    return out


@server.tool()
def desk_week() -> dict:
    """Weekly P&L (GET /api/week): the last 7 UTC days from the trades ledger — per-day realized
    aggregate, closed-trade count, and per-arm attribution."""
    return _get("/api/week")


@server.tool()
def desk_strategies() -> dict:
    """The per-arm strategy rows (the arms[] of /api/health): name, 4-state mode
    (OFF/PAPER/LIVE/LIVE-LOCKED), enabled/live routing, mode-since (audit clock), open
    positions, last fire/error, today's counts (fires/evaluated/orders/shares/realized),
    schedule, effective params and the plain-English rule line. Rows = config rows ∪ on-disk
    positions files (a retired arm's residue stays visible until it resolves)."""
    h = _get("/api/health")
    if "arms" not in h:
        return h                                   # error/status passthrough
    return {"ts": h.get("ts"), "arms": h["arms"]}


@server.tool()
def desk_explain(term: str) -> dict:
    """Explain a desk term in plain language — the same vocabulary as the /glossary page.
    Covers every health-chip threshold (capture heartbeat, driver heartbeat, exit-monitor
    cadence, forecast memory, quota, …) and the concept terms (live unlock, mode chips, fire
    window, pause/halt/flatten, mode-since, actions history, …). Fuzzy on name, label and aka
    internal names, so 'quota', 'driver heartbeat' or 'n_views' all resolve. Threshold numbers
    come from the LIVE health payload (the dict the chips judge by), never from a hardcoded
    copy."""
    q = (term or "").strip().lower()
    if not q:
        return {"found": False, "error": "empty term"}

    def _score(hays: List[str]) -> float:
        best = 0.0
        for h in hays:
            hl = str(h).lower()
            if q == hl:
                return 100.0
            toks = [t for t in re.split(r"[^a-z0-9]+", hl) if t]
            if q in toks:
                best = max(best, 92.0)
            elif q in hl:
                best = max(best, 82.0)
            else:
                r = difflib.SequenceMatcher(None, q, hl).ratio()
                tr = max((difflib.SequenceMatcher(None, q, t).ratio() for t in toks),
                         default=0.0)
                best = max(best, 70.0 * max(r, tr))
        return best

    cands: List[tuple] = []
    for key, meta in G.GLOSSARY.items():
        cands.append((_score([key, meta["label"], meta["aka"]]), "threshold", key, meta))
    for t in G.TERMS:
        cands.append((_score([t["label"], t["aka"]]), "concept", None, t))
    cands.sort(key=lambda c: -c[0])
    best = cands[0]
    if best[0] < 56.0:
        return {"found": False, "term": term,
                "available": [c[3]["label"] for c in cands]}
    score, kind, key, meta = best
    out = {"found": True, "term": term, "kind": kind, "label": meta["label"],
           "meaning": meta["meaning"], "aka": meta["aka"]}
    if kind == "threshold":
        out["key"] = key
        out["action"] = meta["action"]
        h = _get("/api/health", timeout=30.0)
        pair = (h.get("thresholds") or {}).get(key) if isinstance(h, dict) else None
        if pair:
            out["threshold"] = G.threshold_text(key, pair)
            out["threshold_values"] = list(pair)
        else:
            out["threshold"] = None
            out["note"] = "live thresholds unavailable (dashboard unreachable?)"
    others = [c[3]["label"] for c in cands[1:5] if c[0] >= 56.0]
    if others:
        out["other_matches"] = others
    return out


@server.tool()
def desk_alerts() -> dict:
    """The red-alert notifier's view: conditions currently ACTIVE (each with its message and the
    moment it transitioned active — the last transitions) plus the blind-spot counters, read from
    the notifier's state file. Empty active list + a recent check time = the desk is healthy.
    The notifier pushes one alert per condition transition and a recovery line when it clears;
    this tool shows what is alerted RIGHT NOW."""
    p = os.path.join(_data_root(), "notifier_state.json")
    try:
        with open(p) as f:
            st = json.load(f)
    except OSError:
        return {"state_file": p, "active": [],
                "note": "no notifier state file — notifier not running yet (or a fresh data "
                        "dir); supervisord program 'notifier' owns it"}
    except Exception as e:
        return {"state_file": p, "active": [], "error": f"state file unreadable: {e!r}"}
    act = st.get("active") or {}
    rows = [{"condition": cid,
             "since": (v or {}).get("since"),
             "since_utc": _utc((v or {}).get("since")),
             "message": (v or {}).get("msg")}
            for cid, v in sorted(act.items())]
    try:
        checked = int(os.path.getmtime(p))
    except OSError:
        checked = None
    return {"state_file": p, "last_check_ts": checked, "last_check_utc": _utc(checked),
            "active": rows, "health_fails_in_a_row": int(st.get("health_fails") or 0),
            "book_flow_stalled_checks": int(st.get("book_stall") or 0)}


# ── control tools (the audited HTTP path — never a direct config write) ────────
@server.tool()
def desk_pause(name: str) -> dict:
    """Pause one strategy arm — POST /api/strategy/<name>/pause through the audited control API
    (the flip lands in the control audit JSONL exactly like a UI click). Pause = no NEW entries;
    settlement and the exit monitor keep managing any open positions. Takes effect at the arm's
    next fire window (the response says when). Refusals come back verbatim as {status, body}:
    429 = debounce (retry after retry_after_s), 404 = unknown arm, 403 = control gate."""
    return _control_post(f"/api/strategy/{urllib.parse.quote(str(name), safe='')}/pause")


@server.tool()
def desk_resume(name: str, confirm_name: Optional[str] = None) -> dict:
    """Resume (re-enable) one strategy arm — POST /api/strategy/<name>/resume through the
    audited control API. Resuming a LIVE-ROUTED arm restores configured real-money exposure, so
    the API demands a typed confirmation: the first call returns status 409 with
    body.expect.confirm_name — ask the human, then retry with confirm_name set to exactly that
    value. Paper arms resume without confirmation. Refusals surface verbatim as {status, body}."""
    body = {"confirm_name": confirm_name} if confirm_name is not None else None
    return _control_post(
        f"/api/strategy/{urllib.parse.quote(str(name), safe='')}/resume", body)


@server.tool()
def desk_pause_all() -> dict:
    """Pause EVERY enabled arm (the kill switch's pause tier) — POST /api/stack/pause-all
    through the audited control API, one audit line per flipped arm. Pause semantics: no new
    entries anywhere; settlement + the exit monitor keep managing open positions. Does NOT
    cancel resting live quotes or flatten positions (halt/flatten stay on the real desk's
    manual path, out of MCP by design)."""
    return _control_post("/api/stack/pause-all")


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
