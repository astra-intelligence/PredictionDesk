"""desk/glossary.py — the plain-language glossary (mission-control-v1 stage C, ui-redesign v2).

One entry per build_ops.THRESHOLDS key, rendered FROM the dict the caller passes in (the same dict
the chips judge from), so the glossary can never contradict a chip. The v2 critic found the old
hand-written capture note claiming "stale >5min = red" while the chip actually reds at 30 min; the
fix is structural: NO threshold number lives in this file. Each entry carries the plain-language
name, what it means, the threshold line (rendered from the dict), the action to take, and the
internal/aka name for grep-ability. TERMS holds the glossary entries that are concepts rather than
thresholds — including the two corrected v2 translations (capture health, live unlock).

Consumers: dashboard_server GET /glossary (page render) and desk/notifier.py (action lines on
pushes — the push says the same thing the glossary page says).
"""
from __future__ import annotations

from typing import Dict, List, Optional

# ── per-THRESHOLDS-key entries (label · meaning · action · aka). Numbers NEVER live here. ─────────
GLOSSARY: Dict[str, Dict[str, str]] = {
    "capture_hb": {
        "label": "Capture health — heartbeat",
        "meaning": "Are we recording the market? The capture process stamps a heartbeat about "
                   "every 30 s while it polls order books; a stale stamp means the recorder "
                   "itself stopped.",
        "action": "check the capture log and the network path; restart capture via supervisorctl "
                  "if it is dead",
        "aka": "capture heartbeat (capture dir _heartbeat)",
    },
    "parquet": {
        "label": "Capture health — newest book file",
        "meaning": "Age of the newest order-book file actually written to disk (today + yesterday "
                   "partitions). The heartbeat can be fresh while nothing lands on disk — this "
                   "row catches that.",
        "action": "check the capture log and disk; a fresh heartbeat with stale files means data "
                  "is not flowing (proxy/network)",
        "aka": "newest parquet",
    },
    "universe": {
        "label": "Market universe freshness",
        "meaning": "How old the tradable-market list is (it refreshes about every 30 min). A "
                   "stale universe means capture may be polling yesterday's markets.",
        "action": "check the capture log for universe-refresh errors",
        "aka": "_universe.json age",
    },
    "driver_hb": {
        "label": "Driver health — heartbeat",
        "meaning": "The strategy driver (the process that fires arms at their windows) writes a "
                   "heartbeat between loop iterations; during a fire it legally goes quiet for "
                   "up to ~20 min.",
        "action": "check the driver log; restart via make ops-* if it is dead",
        "aka": "driver heartbeat",
    },
    "exitmon_pass": {
        "label": "Exit monitor cadence",
        "meaning": "When the exit monitor last swept every positions file (paused arms included). "
                   "Paper TP/SL exits run at fire time; this sweep is the live-exit path and the "
                   "risk backstop, so its cadence is judged even while everything routes paper.",
        "action": "check the exitmonitor log",
        "aka": "exitmonitor lastpass",
    },
    "market_meta": {
        "label": "Market metadata build",
        "meaning": "Age of the tenant's market-metadata build (outcome labels and thresholds per "
                   "market). Stale or empty metadata silently zeroes every fire.",
        "action": "the tenantdaily loop rebuilds it — check its log if this stays stale",
        "aka": "market_meta.json",
    },
    "substrate_series": {
        "label": "Forecast error memory (30 d)",
        "meaning": "How wrong each forecast engine has been vs the official station lately — the "
                   "strategy prices with this rolling memory. Past the red age the arms refuse "
                   "to trade (fail closed) rather than price on a stale memory.",
        "action": "run the tenant daily build / wait for API quota; arms self-restore once the "
                  "memory is fresh again",
        "aka": "calibration substrate · error_series_*",
    },
    "quota_pct": {
        "label": "Forecast API quota",
        "meaning": "Share of today's weighted Open-Meteo call budget already used (UTC day).",
        "action": "nothing urgent — at the cap, cached pricing continues; heavy backfills wait "
                  "for tomorrow",
        "aka": "OM quota meter (__meter__)",
    },
}

# ── concept entries (no numbers anywhere — nothing here can drift from a chip) ───────────────────
TERMS: List[Dict[str, str]] = [
    {
        "label": "Live unlock (the two gates)",
        "meaning": "Real-money trading sits behind two independent gates: the environment flag "
                   "(LIVE_TRADING_ENABLED) and a scoped PASS earned by proving the simulator "
                   "against real fills. The PASS is not a blanket unlock — it names an execution "
                   "capability (taker/maker), the exact strategies, and one market class, and it "
                   "expires; the chip shows scope + days left (e.g. “PASS: taker · myarm · "
                   "weather · 12 d left”), never a bare countdown. Either gate missing ⇒ "
                   "every order routes paper. Live-arming is deliberately absent from this UI and "
                   "its API — the escalation ritual stays manual, out of band.",
        "aka": "recon PASS · PASS.json + LIVE_TRADING_ENABLED",
    },
    {
        "label": "Mode chips — OFF / PAPER / LIVE / LIVE-LOCKED",
        "meaning": "OFF = enabled:false or no config row — never evaluates. PAPER = evaluates and "
                   "trades simulated fills; no wallet is touched. LIVE = the row asks for live "
                   "AND both desk gates are open — real orders. LIVE-LOCKED = the row asks for "
                   "live but a gate is missing — it evaluates, it cannot send.",
        "aka": "mode_of() · config enabled × params.live × desk gates",
    },
    {
        "label": "Views — markets evaluated",
        "meaning": "Markets the strategy actually priced in one fire. “Evaluated 0 markets” "
                   "at a window means the substrate gate failed closed, the universe window was "
                   "empty, or capture has a gap.",
        "aka": "n_views",
    },
    {
        "label": "Guard veto",
        "meaning": "A named safety rule rejected a would-be trade (jump veto, exposure cap, …). "
                   "Not yet surfaced in the activity feed — veto events join later via the "
                   "__skips__ convention.",
        "aka": "guard veto · out_signals __skips__",
    },
    {
        "label": "Fire / fire window",
        "meaning": "One scheduled evaluation pass of an arm (release times or an interval). A "
                   "fire may legally run up to the driver's deadline, during which the driver "
                   "heartbeat pauses — a quiet heartbeat mid-window is not a dead driver.",
        "aka": "release_times / interval schedule · FIRE_DEADLINE_S",
    },
    {
        "label": "Pause · halt · flatten (kill-switch taxonomy)",
        "meaning": "Pause = no new entries; settlement and the exit monitor keep managing open "
                   "positions, disabled rows included. Halt = pause-all + cancel resting live "
                   "quotes. Flatten = exit everything at touch via the exit-monitor path. "
                   "Emergency = halt + flatten: stop AND get me out. Halt and flatten act on "
                   "live arms — the real build.",
        "aka": "kill-switch taxonomy (ui-redesign v2)",
    },
    {
        "label": "Mode-since",
        "meaning": "How long an arm has been in its current mode. The clock resets on a "
                   "pause/resume flip and reads from the control audit log (seeded once at "
                   "bootstrap with the best-known mode start).",
        "aka": "control audit effective_at",
    },
    {
        "label": "Actions history",
        "meaning": "The append-only control audit log: every pause/resume flip (when, prior→new, "
                   "when it takes effect, from where) plus the bootstrap seed rows, newest first "
                   "on the System page. The control API cannot write anything else.",
        "aka": "control_audit.jsonl",
    },
]


# ── rendering ─────────────────────────────────────────────────────────────────────────────────────
def fmt_span(s: Optional[float]) -> str:
    """Same wording build_ops._fmt_age uses on the chips, so page and glossary read identically."""
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


def threshold_text(key: str, pair) -> str:
    """The threshold sentence for one THRESHOLDS entry — rendered from the dict, never typed."""
    g, r = float(pair[0]), float(pair[1])
    if key == "quota_pct":
        return f"amber ≥ {g:g}% · no red — at the cap, cached pricing continues"
    return f"amber > {fmt_span(g)} · red > {fmt_span(r)}"


def action_for(key: str, fallback: str = "check the System page") -> str:
    return GLOSSARY.get(key, {}).get("action", fallback)


_CSS = """
:root{--bg:#0b0f14;--panel:#141a22;--line:#232c38;--ink:#e8edf4;--mut:#9aa7b5;--faint:#5e6b7a;
--gold:#F5C518;--serif:Georgia,'Times New Roman',serif;
--mono:ui-monospace,Menlo,Monaco,'SF Mono',monospace;--sans:system-ui,-apple-system,'Segoe UI',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:dark}
body{background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:14px;line-height:1.55;
max-width:880px;margin:0 auto;padding:28px 20px 72px;-webkit-font-smoothing:antialiased}
.top{font-family:var(--mono);font-size:12px;margin-bottom:14px}
a{color:var(--mut)} a:hover{color:var(--ink)}
h1{font-family:var(--serif);font-weight:400;font-size:32px;letter-spacing:.4px}
h1 .em{font-style:italic;color:var(--gold)}
.sub{font-family:var(--mono);font-size:12px;color:var(--mut);margin:6px 0 10px}
.seclabel{font-family:var(--mono);font-size:10.5px;letter-spacing:.2em;text-transform:uppercase;
color:var(--faint);margin:30px 0 6px;display:flex;align-items:center;gap:12px}
.seclabel::after{content:"";flex:1;height:1px;background:var(--line)}
.entry{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 16px;margin:10px 0}
.entry h3{font-size:14.5px;font-weight:600;margin-bottom:4px}
.entry p{color:var(--mut);font-size:13px}
.kv{display:flex;gap:10px;margin-top:7px;border-top:1px solid var(--line);padding-top:6px}
.kv .k{flex:0 0 84px;font-family:var(--mono);font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;
color:var(--faint);padding-top:2px}
.kv .v{flex:1;font-family:var(--mono);font-size:11.5px;color:var(--ink)}
.kv .v.dim{color:var(--mut)}
code{font-family:var(--mono);font-size:11px;color:var(--gold)}
footer{margin-top:34px;font-family:var(--mono);font-size:11px;color:var(--faint)}
"""


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _entry(label: str, meaning: str, rows: List) -> str:
    kv = "".join(f'<div class="kv"><span class="k">{_esc(k)}</span>'
                 f'<span class="v{" dim" if k == "Action" else ""}">{v}</span></div>'
                 for k, v in rows)
    return f'<div class="entry"><h3>{_esc(label)}</h3><p>{_esc(meaning)}</p>{kv}</div>'


def render_glossary(thresholds: dict) -> str:
    """The /glossary page. `thresholds` IS build_ops.THRESHOLDS (or its /api/health passthrough) —
    every key renders, known or not, so a new chip can never be missing from the help page."""
    H = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,"><title>PredictionDesk · Glossary</title><style>{_CSS}</style></head><body>
<div class="top"><a href="/">← overview</a> · <a href="/ops">system</a> · <a href="/index.html">classic</a></div>
<h1>Prediction<span class="em">Desk</span> — glossary</h1>
<div class="sub">plain language for every chip and term · the threshold numbers below render from the
 live THRESHOLDS dict — the same values the chips judge by, so this page can never contradict a chip</div>
<div class="seclabel">Health chips — one entry per threshold</div>"""]
    for key in thresholds:
        meta = GLOSSARY.get(key) or {
            "label": key.replace("_", " "),
            "meaning": "(new threshold — no plain-language entry written yet; the chip judges "
                       "from the values below)",
            "action": "check the System page",
            "aka": key,
        }
        H.append(_entry(meta["label"], meta["meaning"], [
            ("Threshold", _esc(threshold_text(key, thresholds[key]))),
            ("Action", _esc(meta["action"])),
            ("aka", f'{_esc(meta["aka"])} · <code>{_esc(key)}</code>'),
        ]))
    H.append('<div class="seclabel">Concepts</div>')
    for t in TERMS:
        H.append(_entry(t["label"], t["meaning"], [("aka", f'<code>{_esc(t["aka"])}</code>')]))
    H.append('<footer><a href="/">overview</a> · <a href="/ops">system</a> · '
             'thresholds live in build_ops.THRESHOLDS — edit there, both chip and glossary follow'
             '</footer></body></html>')
    return "\n".join(H)
