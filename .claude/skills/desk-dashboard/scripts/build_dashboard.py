"""build_dashboard.py — the ONE dashboard site.

Orchestrates the three pages through the single `ledger` adapter — identical information
hierarchy on each (overview scorecard → per-config/run → trades → per-trade decomp):

    backtest.html  ← --source backtest --grid   (the ablation: one row per run_id)
    forward.html   ← --source paper             (the paper test on the converged driver)
    live.html      ← --source live              (real-money orders + sim-vs-live + gate/heartbeat)
    index.html     ← links the three with headline numbers

All self-contained (inline JSON, no network, no build step). Re-run to refresh.

    cd /path/to/worktree
    python3 .claude/skills/desk-dashboard/scripts/build_dashboard.py --out-dir dashboard/
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
from render import render                       # noqa: E402
from adapters import ledger as L                # noqa: E402


def _opp_adapter():
    """The installed OPPORTUNITIES adapter (contract: adapters/CONTRACT.md): env DESK_OPP_ADAPTER
    names it, else the first module in adapters/ exposing PAGE + scan. None = none installed."""
    import importlib
    adir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters")
    env = os.environ.get("DESK_OPP_ADAPTER")
    try:
        names = [env] if env else sorted(
            f[:-3] for f in os.listdir(adir)
            if f.endswith(".py") and not f.startswith("_") and f != "ledger.py")
    except Exception:
        names = []
    for name in names:
        try:
            mod = importlib.import_module(f"adapters.{name}")
        except Exception:
            continue
        if hasattr(mod, "PAGE") and hasattr(mod, "scan"):
            return mod
    return None

from adapters import desk_tz, tz_label           # noqa: E402  (display tz: env DESK_TZ, default UTC)

TZ = desk_tz()


def _headline(spec: dict) -> dict:
    strategies = spec.get("strategies", [])
    overall = round(sum((s.get("overall") or 0) for s in strategies), 2)
    # trades_n = the true per-strategy count; len(trades) is the CAPPED embed (≤400/strategy)
    trades = sum((s.get("trades_n") if s.get("trades_n") is not None else len(s.get("trades", [])))
                 for s in strategies)
    settled = sum((s.get("n_settled") or 0) for s in strategies)
    return {"strategies": len(strategies), "overall": overall, "trades": trades, "settled": settled}


INDEX_TMPL = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PredictionDesk — dashboard</title>
<style>
  :root{{--bg:#0c0e12;--bg2:#11141a;--line:#262d39;--ink:#e9e4d6;--ink-dim:#9aa1ad;--warm:#e0a44a;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}}
  body{{background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Helvetica,sans-serif;max-width:820px;margin:0 auto;padding:48px 24px}}
  h1{{font-weight:700;letter-spacing:-.5px}}h1 .em{{color:var(--warm)}}
  .sub{{color:var(--ink-dim);margin:-6px 0 30px}}
  a.card{{display:block;text-decoration:none;color:inherit;border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin:14px 0;background:var(--bg2);transition:border-color .15s}}
  a.card:hover{{border-color:var(--warm)}}
  .card h2{{margin:0 0 4px;font-size:19px}}.card .d{{color:var(--ink-dim);font-size:13px;margin-bottom:12px}}
  .nums{{display:flex;gap:26px;font-family:var(--mono);font-size:13px}}
  .nums b{{display:block;font-size:19px;color:var(--ink)}}.nums span{{color:var(--ink-dim)}}
  .pos{{color:#2b6}}.neg{{color:#c44}}
  footer{{color:var(--ink-dim);font-size:12px;margin-top:40px;font-family:var(--mono)}}
</style></head><body>
<div style="margin-bottom:6px;font-family:var(--mono);font-size:12px"><a href="/" style="color:var(--warm);text-decoration:none">← overview (mission control)</a></div>
<h1>The <span class="em">Desk</span> — dashboard <span style="font-size:13px;color:var(--ink-dim);font-weight:400">· classic</span></h1>
<div class="sub">One site, three lanes, one information hierarchy. Backtest ablation → forward test → live.</div>
{cards}
<footer>built {built} · self-contained · re-run build_dashboard.py to refresh</footer>
</body></html>"""

# plain entry cards — NO headline totals (user: mixed winner/loser cohorts made "overall $" misleading)
CARD = """<a class="card" href="{href}">
  <h2>{title}</h2><div class="d">{desc}</div></a>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="dashboard")
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)

    # Refresh the PASSIVE sim-vs-live reconciliation (replay each real live fill through the sim engine at its
    # as-of ts) so the live-tab 'sim↔live mean |dev| bps' badge is current. Fail-soft — a recon error (or no
    # captured books) must never break the dashboard build.
    try:
        from desk.eval.recon.reconcile_live_fills import reconcile as _recon_live
        _r = _recon_live()
        print(f"[build] sim-vs-live recon: {_r.get('n_measured')}/{_r.get('n_fills')} fills · mean|dev| {_r.get('mean_abs_dev_bps')}bps")
    except Exception as _e:
        print(f"[build] sim-vs-live recon skipped: {repr(_e)[:110]}")

    pages = [
        ("backtest.html", "Backtest ablation", "Every run_id in desk/eval/runs/ — one row per config.",
         lambda: L.build_backtest(grid=True)),
        ("forward.html", "Forward test", "The paper test on the converged driver (settled-only realized + MTM).",
         L.build_paper),
        ("live.html", "Live trade", "Real-money orders + sim-vs-live deviation + gate/heartbeat.",
         L.build_live),
    ]
    cards = []
    specs = {}
    for fname, title, desc, builder in pages:
        spec = builder()
        specs[fname.split(".")[0]] = spec
        render(spec, os.path.join(a.out_dir, fname))
        h = _headline(spec)
        cards.append(CARD.format(href=fname, title=title, desc=desc))
        print(f"[build] {fname}: {h}")

    # the CURRENT opportunities screen (own renderer — a live pricing pass, not a strategies spec).
    # Builds NEVER auto-rescan (the Refresh button is the sole live-scan trigger, to save OM quota): the
    # build only REUSES an existing opportunities cache and otherwise renders an EMPTY page. So a clean
    # tree (no cache) produces a valid, empty opportunities page with zero network calls, never a crash.
    import json as _json
    D = _opp_adapter()
    opp_data = {"rows": [], "n_in_window": 0, "n_actionable": 0, "gauges": {},
                "as_of": 0, "decision_date": "", "lanes": {}}
    if D is not None:
        if os.path.exists(D.OPP_CACHE):
            try:
                opp_data = D.scan_cached(max_age_s=7 * 86400)
            except Exception as e:
                print(f"[build] opportunities.html: cache unusable ({e}) — rendering empty page")
        with open(os.path.join(a.out_dir, "opportunities.html"), "w") as f:
            f.write(D.PAGE.replace("/*__OPP__*/null", _json.dumps(opp_data)))
        cards.append(CARD.format(
            href="opportunities.html", title="Current opportunities",
            desc="What the desk sees RIGHT NOW: every in-window market priced vs the live book, with "
                 "the full idea-generation breakdown."))
        print(f"[build] opportunities.html: {len(opp_data.get('rows', []))} rows, "
              f"{opp_data.get('n_actionable')} actionable")
    else:
        print("[build] no opportunities adapter installed (adapters/CONTRACT.md) — page skipped")

    cards.append(CARD.format(
        href="/ops", title="System",
        desc="Is everything running and healthy? Processes, capture freshness, driver heartbeat, "
             "forecast-error memory age, quota, paper P&L — with thresholds explained."))

    # write index first (so the completeness gate can see every built page, index included), then run
    # the gate, then re-write index with the completeness badge folded in.
    built = (lambda dt: dt.strftime("%Y-%m-%d %H:%M ") + tz_label(dt))(datetime.now(tz=TZ))
    idx_path = os.path.join(a.out_dir, "index.html")
    with open(idx_path, "w") as f:
        f.write(INDEX_TMPL.format(cards="\n".join(cards), built=built))

    # completeness gate — validates specs + built pages, writes data_completeness.json,
    # and injects the summary panel into the index so missing data is EXPLAINED, never silent.
    from validate_dashboard import finalize
    ok = finalize(a.out_dir, specs, opp_data)
    comp = {}
    try:
        comp = __import__("json").load(open(os.path.join(a.out_dir, "data_completeness.json")))
    except Exception:
        pass
    badge = ("<div class='card' style='border-color:%s'><h2 style='font-size:15px'>Data completeness — %s</h2>"
             "<div class='d'>%s</div></div>") % (
        ("#2b6" if comp.get("ok") else "#c44"), ("OK" if comp.get("ok") else "GAPS UNEXPLAINED"),
        comp.get("summary", "not computed"))
    with open(idx_path, "w") as f:
        f.write(INDEX_TMPL.format(cards="\n".join(cards) + badge, built=built))
    print(f"[build] index.html + 3 pages → {a.out_dir}/")
    if not ok:
        sys.exit(3)                        # fail-loud: unexplained blanks block a green build


if __name__ == "__main__":
    main()
