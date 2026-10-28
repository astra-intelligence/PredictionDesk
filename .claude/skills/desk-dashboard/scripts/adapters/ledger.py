"""adapters/ledger.py — THE one dashboard adapter.

Reads the ONE trade-event log and emits the SPEC the shared render.py/template.html already
consume. A single flag picks the lane; the schema is identical across all three.

    --source paper   → desk/tracker/trades.jsonl rows where source=paper, grouped by strategy
    --source live    → desk/tracker/trades.jsonl rows where source=live  (+ sim_vs_live deviations,
                       heartbeat/staleness banner, gate PASS stamp)
    --source backtest→ desk/eval/runs/<run_id>/ run dir(s): one config = one deep-dive; --grid = all
                       run dirs (the ablation), --run RUN_ID = one, --strategy NAME = filter by strategy

The renderer NEVER touches the network: slug/decomp/fair/edge were logged AT TRADE TIME into the
TradeRow, so even a closed (resolved, Gamma-dropped) market renders its full pricing breakdown.

Footer/banner data (surfaced in meta so the template shows it without a code change):
  footnote  ← RunMeta code_ref · config_hash · env snapshot · settle-source counts (gamma vs clob)
  banner    ← (live only) heartbeat/staleness · gate PASS scope · mean sim-vs-live deviation

Usage:
    python3 .claude/skills/desk-dashboard/scripts/adapters/ledger.py \
        --source backtest --grid --out backtest.html
"""
from __future__ import annotations
import argparse
import sys as _sys
_REPO_ROOT = __import__("os").path.abspath(__import__("os").path.join(
    __import__("os").path.dirname(__file__), "..", "..", "..", ".."))
if _REPO_ROOT not in _sys.path:                 # fresh-clone: repo root isn't on sys.path in a bare
    _sys.path.insert(0, _REPO_ROOT)             # subprocess (the eval gate's run_dir smoke) — bootstrap it
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from render import render  # noqa: E402

from adapters import desk_tz, desk_tz_name, tz_label  # noqa: E402  (display tz: env DESK_TZ, default UTC)

TZ = desk_tz()
REPO = os.getcwd()
TRADES = os.path.join("desk", "tracker", "trades.jsonl")
RUNS_DIR = os.path.join("desk", "eval", "runs")
SIM_VS_LIVE = os.path.join("desk", "eval", "recon", "findings", "sim_vs_live_report.json")
HEARTBEAT = os.path.join("desk", "driver", "_heartbeat")
GATE_PASS = os.path.join("desk", "live", "..", "eval", "recon", "findings", "PASS")
STRATEGY_CONFIG = os.path.join("desk", "signals", "strategy_config.json")


def _fmt_ts(ts):
    """Rendered timestamp in the display tz (env DESK_TZ, default UTC), labeled."""
    if not ts:
        return None
    dt = datetime.fromtimestamp(ts, TZ)
    return dt.strftime("%Y-%m-%d %H:%M ") + tz_label(dt)


def _built() -> str:
    dt = datetime.now(tz=TZ)
    return dt.strftime("%Y-%m-%d %H:%M ") + tz_label(dt)


# ── config → HUMAN-LEGIBLE levers (the "what knobs am I turning" glossary) ─────
def _engine_models(engine: str):
    """(models list, central statistic, error distribution) for a named ensemble engine, if the strategy
    exposes an ENGINES registry — so the levers panel can name the ACTUAL component models + how they are
    combined, not just the engine label. Returns (None,None,None) when the strategy defines no such registry
    (the shipped sample does not). Looked up via an optional `desk.signals.<strategy>.pricing.ENGINES`."""
    try:
        sys.path.insert(0, REPO)
        import importlib
        pricing = importlib.import_module("desk.signals.pricing")   # optional; absent for simple strategies
        e = pricing.ENGINES.get(engine)
        if e:
            return e["models"], e.get("central"), e.get("dist")
    except Exception:
        pass
    return None, None, None


def _config_levers(cfg: dict, extra=None) -> list:
    """Build the labeled [key, value, description] levers glossary from a strategy's config dict. Surfaces the
    FULL set of knobs the user asked for: ensemble models + central statistic (expanded from the engine),
    entry/edge threshold, lead window, regime, edge mode/cap, metric, exec mode, calibration frame, qty/budget.
    Empty/absent knobs are skipped so the panel shows only what's actually set."""
    cfg = cfg or {}
    engine = cfg.get("engine")
    lev = []
    if engine:
        models, central, dist = _engine_models(engine)
        if models:
            mlist = models if isinstance(models, str) else ",".join(models)
            n = len(mlist.split(","))
            lev.append(["Ensemble", f"{engine} · {n} model(s)",
                        f"Component models blended: {mlist}."])
            if central:
                lev.append(["Central statistic", central,
                            f"How the {n} component model outputs are combined into one estimate ({central})"
                            + (f"; error dist = {dist}." if dist else ".")])
        else:
            lev.append(["Engine", engine, "The pricing engine (ensemble) this strategy prices fair on."])
    if cfg.get("universe"):
        us = cfg["universe"]
        lev.append([f"Universe ({len(us)})", ", ".join(us[:6]) + ("…" if len(us) > 6 else ""),
                    "The allowlist of markets/entities this strategy trades. Full list: "
                    + ", ".join(us) + "."])
    elif cfg.get("bucket_type") or cfg.get("engine"):
        lev.append(["Universe", "all", "No filter — every captured market in scope trades."])
    thr = cfg.get("entry_threshold", cfg.get("threshold", cfg.get("edge_threshold")))
    if thr is not None:
        lev.append(["Entry threshold", f"{thr}", "Minimum |fair − market mid| (in prob) to open a trade."])
    if cfg.get("lead") is not None:
        lev.append(["Lead window", str(cfg.get("lead")), "How far before resolution the entry priced on."])
    if cfg.get("regime"):
        lev.append(["Regime", str(cfg.get("regime")), "Volatility-regime filter the entry must fall in."])
    if cfg.get("edge_mode") or cfg.get("edge_cap") is not None:
        lev.append(["Edge mode / cap", f"{cfg.get('edge_mode','—')} · cap {cfg.get('edge_cap','—')}",
                    "How raw edge is scaled, and any per-trade edge cap."])
    if cfg.get("metric"):
        lev.append(["Metric", str(cfg.get("metric")), "The market variable this strategy prices."])
    mode = cfg.get("mode")
    if mode:
        lev.append(["Exec mode", mode, "taker crosses the book (pays spread); maker rests a quote (earns it, may not fill)."])
    calib = cfg.get("calibration_truth") or cfg.get("calibration") or cfg.get("calib")
    if calib:
        lev.append(["Calibration frame", str(calib),
                    "Truth source + window used to de-bias the model into a fair probability."])
    if cfg.get("qty") is not None:
        lev.append(["Qty / trade", str(cfg.get("qty")), "Contracts per fill."])
    if cfg.get("budget") is not None or cfg.get("bankroll") is not None:
        lev.append(["Budget", f"${cfg.get('budget', cfg.get('bankroll'))}", "Capital allotted to this strategy."])
    if extra:
        lev.extend(extra)
    return lev


def _strategy_config_row(name: str) -> dict:
    """The strategy_config.json params for a paper/live strategy, matched by name (exact, then prefix). The
    paper/live lane has no run.json — its knobs live in the registry overlay the driver reads."""
    try:
        rows = (json.load(open(STRATEGY_CONFIG)) or {}).get("strategies", [])
    except Exception:
        return {}
    for r in rows:
        if r.get("name") == name:
            return dict(r.get("params") or {})
    base = name.split("·")[0].split("@")[0].strip()
    for r in rows:
        if r.get("name") == base:
            return dict(r.get("params") or {})
    return {}


def _read_jsonl(path):
    rows = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


# ── TradeRow → SPEC trade ─────────────────────────────────────────────────────
def _trade_from_row(r: dict, sidecar: dict | None = None) -> dict:
    ex = r.get("extras") or {}
    lead = ex.get("lead")
    # subject/date/slug the strategy captured at entry. pnl_ledger forwards lead/engine/gap into extras but
    # not subject/date, so a run may write a run-dir signals.json sidecar the adapter merges here (by
    # order_id) — the sanity-check link (real slug + subject + target date) works even for closed markets.
    sc = (sidecar or {}).get(r.get("order_id")) or {}
    # optional per-trade context a strategy may log (a subject label + a target date). Absent for the
    # shipped sample; the template renders "—" when missing.
    city = ex.get("city") or sc.get("city") or ""
    target_date = ex.get("target_date") or sc.get("target_date") or ""
    if not isinstance(lead, int):
        lead = sc.get("lead") if isinstance(sc.get("lead"), int) else None
    url = r.get("url") or (f"https://polymarket.com/market/{sc.get('slug')}" if sc.get("slug") else "")
    if re.search(r"/market/0x[0-9a-fA-F]{40,}$", url or ""):
        url = ""                                    # conditionId-as-slug (pre-backfill row) → 404s; show '—' not a dead ↗
    fee = r.get("fees")
    ex2 = r.get("extras") or {}
    gross = ex2.get("gross")
    fee = r.get("fees") or 0.0
    pnl_b = r.get("pnl_realized")
    pnl_a = ex2.get("pnl_adverse")
    # assumed exec cost per band (¢/share): pnl_band = gross − fee − cost_band  ⇒  cost = gross − fee − pnl
    qd = max(1e-9, abs(r.get("qty") or 1.0))
    cost_b = round((gross - fee - pnl_b) / qd * 100, 2) if (gross is not None and pnl_b is not None) else None
    cost_a = round((gross - fee - pnl_a) / qd * 100, 2) if (gross is not None and pnl_a is not None) else None
    return {
        "status": r.get("status", "open"),
        "side": r.get("side"), "qty": r.get("qty"),
        "pnl_bands": ({"opt": (round(gross - fee, 4) if gross is not None else None),
                       "base": pnl_b, "adv": pnl_a} if gross is not None else None),
        "cost_c": ({"base": cost_b, "adv": cost_a} if cost_b is not None else None),
        "entry_px": r.get("price"),
        "fair": r.get("fair"), "mid_entry": r.get("mid_entry"), "live_mid": r.get("live_mid"),
        "unrealized": r.get("pnl_mtm"), "realized": r.get("pnl_realized"),
        "queue_ahead": r.get("queue_ahead"),
        "lead": lead,
        "city": city,
        "target_date": target_date,
        "entry_ts": r.get("ts"), "url": url,
        "fair_model": ex.get("engine") or r.get("strategy") or "",
        "decomp": r.get("decomp"),
        "note": ex.get("note"),
        # ── execution ASSUMPTIONS (what data entered the trade + what cost/slippage was assumed) ──
        # fidelity_tier: T5 = filled by walking the REAL captured order book (exact); T1 = mid + MODELED
        # spread/slippage (cost model). The template turns this into a plain-language sentence.
        "fidelity_tier": r.get("fidelity_tier"),
        "degraded": bool(r.get("degraded")),
        "cost_est_c": ex.get("cost_est_c"),
        "slippage_bps_vs_mid": ex.get("slippage_bps_vs_mid"),
        "est_confidence": ex.get("est_confidence"),
        "fees": round(fee, 4) if isinstance(fee, (int, float)) else None,
    }


def _merge_pairs(rows: list) -> list:
    """Collapse each trade's OPEN entry event + CLOSED settle event (same order_id) into ONE record:
    entry context (price/fair/edge/decomp/ts/lead) from the open row, settlement (pnl_realized, payout,
    settle ts) from the closed row, status=closed. Without this every settled backtest trade rendered
    TWICE — once as a phantom 'open (unsettled)' row with no PnL (user bug report). Rows without a
    counterpart pass through unchanged (genuinely-open positions stay open; orphan settles stay closed)."""
    opens = {r.get("order_id"): r for r in rows if r.get("status") in ("open", "resting") and r.get("order_id")}
    open_no_oid = [r for r in rows if r.get("status") in ("open", "resting") and not r.get("order_id")]
    # driver settles carry order_id="" — fall back to FIFO pairing on (market_id, token_id) so a
    # settled trade never renders as phantom-open + orphan-settle (double count)
    by_mkt: dict = {}
    for r in sorted(list(opens.values()) + open_no_oid, key=lambda x: x.get("ts") or 0):
        by_mkt.setdefault((r.get("market_id"), r.get("token_id")), []).append(r)
    out = []
    seen_closed = set()
    for r in rows:
        if r.get("status") != "closed":
            continue
        oid = r.get("order_id")
        dk = (oid or f"{r.get('market_id')}|{r.get('token_id')}", r.get("ts"), r.get("pnl_realized"))
        if dk in seen_closed:                 # duplicate settle event (crash/retry replay) — count once
            continue
        seen_closed.add(dk)
        o = opens.pop(oid, None) if oid else None
        if o is None:
            lst = by_mkt.get((r.get("market_id"), r.get("token_id"))) or []
            if lst:
                o = lst.pop(0)
                if o.get("order_id"):
                    opens.pop(o["order_id"], None)
                else:
                    open_no_oid.remove(o)
        else:
            lst = by_mkt.get((o.get("market_id"), o.get("token_id"))) or []
            if o in lst:
                lst.remove(o)
        if o is not None:
            m = dict(o)
            m["status"] = "closed"
            m["pnl_realized"] = r.get("pnl_realized")
            ex = dict(m.get("extras") or {})
            ex.update({k: v for k, v in (r.get("extras") or {}).items() if v is not None})
            ex["settle_ts"] = r.get("ts")
            ex["payout"] = (r.get("extras") or {}).get("payout", r.get("price"))
            m["extras"] = ex
            out.append(m)
        else:
            out.append(r)                     # orphan settle (no logged entry event) — keep as-is
    out.extend(opens.values())                # genuinely open/resting (no settle yet)
    out.extend(open_no_oid)
    return out


MARKS_PATH = os.path.join("desk", "tracker", "marks.json")
_MARKS: dict | None = None


def _live_unrealized(rows: list) -> None:
    """Mark truly-OPEN rows from the DRIVER-persisted marks file (render stays PURE — no network at
    build time; the driver fetches exit-side quotes on its own live rails and writes marks.json each
    settle pass). Missing mark → mtm stays None with the reason + mark age shown."""
    global _MARKS
    if _MARKS is None:
        try:
            _MARKS = json.load(open(MARKS_PATH))
        except Exception:
            _MARKS = {}
    for r in rows:
        if r.get("status") != "open" or r.get("pnl_mtm") is not None:
            continue
        mk = _MARKS.get(f"{r.get('market_id')}|{r.get('token_id')}")
        ex = r.setdefault("extras", {}) if isinstance(r.get("extras"), dict) else r.setdefault("extras", {})
        if not mk:
            ex["mtm_note"] = "no driver mark yet (next settle pass writes marks.json) — MTM pending"
            continue
        # per-ROW unrealized from the mark's QUOTES (marks are per-market; two strategies holding
        # different entries on the same market must not share one pnl number)
        side, qty, entry = r.get("side") or "BUY", abs(r.get("qty") or 0.0), r.get("price") or 0.0
        if mk.get("bid") is not None and mk.get("ask") is not None and qty and entry:
            exit_px = mk["bid"] if side == "BUY" else mk["ask"]
            r["pnl_mtm"] = round(qty * ((exit_px - entry) if side == "BUY" else (entry - exit_px)), 4)
        else:
            r["pnl_mtm"] = mk.get("pnl_mtm")            # legacy mark (no quotes stored)
        r["live_mid"] = mk.get("live_mid", r.get("live_mid"))
        age_m = int((time.time() - mk.get("ts", 0)) / 60)
        ex["mtm_basis"] = f"{mk.get('basis')} · marked {age_m}m ago by the driver"


def _agg_strategy(name: str, rows: list, edge_cents: int, sidecar: dict | None = None,
                  cfg_levers: list | None = None) -> dict:
    """Collapse one strategy's TradeRows into a SPEC strategy. closed rows carry realized PnL;
    open rows carry MTM. A resting row = a maker quote placed but unfilled."""
    rows = _merge_pairs(rows)                 # ONE record per trade (entry+settle joined by order_id)
    _live_unrealized(rows)                    # truly-open rows marked to the live exit side (bid/ask − fee)
    # Embed a BOUNDED sample per strategy: aggregates above use ALL rows, but inlining every TradeRow
    # (24k+ events x decomp JSON) produced a 435MB backtest.html. Open/resting rows always embed (live
    # state must be inspectable); closed rows embed newest-first up to the cap. The desc labels the cut —
    # no silent truncation; full data stays in the run dir jsonl.
    _cap = int(os.environ.get("DASH_MAX_TRADES_PER_STRAT", "400"))
    _closed_oids = {r.get("order_id") for r in rows if r.get("status") == "closed"}
    # truly-live rows (no closed counterpart) always embed; a settled trade's open row is just its
    # entry EVENT — it competes for the cap alongside the closed rows, newest first.
    _live_rows = [r for r in rows if r.get("status") in ("open", "resting")
                  and r.get("order_id") not in _closed_oids]
    _rest = sorted((r for r in rows if r not in _live_rows),
                   key=lambda r: r.get("ts") or 0, reverse=True)
    _embed = _live_rows + _rest[:max(0, _cap - len(_live_rows))]
    trades = [_trade_from_row(r, sidecar) for r in _embed]
    n_settled = sum(1 for r in rows if r.get("status") == "closed")
    n_open = sum(1 for r in rows if r.get("status") == "open")
    n_resting = sum(1 for r in rows if r.get("status") == "resting")
    realized = round(sum((r.get("pnl_realized") or 0.0) for r in rows), 4)
    mtm = round(sum((r.get("pnl_mtm") or 0.0) for r in rows if r.get("status") == "open"), 4)
    fees = round(sum((r.get("fees") or 0.0) for r in rows), 4)
    is_maker = any((r.get("mode") == "maker") for r in rows)
    exec_lbl = "maker" if is_maker else "taker"
    settled_rows = [r for r in rows if r.get("status") == "closed"]
    nwin = sum(1 for r in settled_rows if (r.get("pnl_realized") or 0) > 0)
    win_pct = round(100 * nwin / len(settled_rows), 1) if settled_rows else None
    # per-cost-band aggregates (opt = gross−fee, base = engine fill, adv = p90 stress) for the overview
    # cost-band selector: totals + win% re-score client-side when the band changes.
    bands = {}
    for bk in ("opt", "base", "adv"):
        vals = []
        for r in settled_rows:
            ex_ = r.get("extras") or {}
            g_ = ex_.get("gross")
            if bk == "base":
                v = r.get("pnl_realized")
            elif bk == "adv":
                v = ex_.get("pnl_adverse")
            else:
                v = (g_ - (r.get("fees") or 0.0)) if g_ is not None else None
            if v is not None:
                vals.append(v)
        if vals:
            bands[bk] = {"total": round(sum(vals), 2),
                         "win": round(100 * sum(1 for v in vals if v > 0) / len(vals), 1), "n": len(vals)}
    leads = [ld for r in rows for ld in [(r.get("extras") or {}).get("lead")] if isinstance(ld, int)]
    lead_band = [min(leads), max(leads)] if leads else None
    cfg_hash = next((r.get("config_hash") for r in rows if r.get("config_hash")), "")
    return {
        "name": name, "exec": exec_lbl, "fair": (rows[0].get("strategy") if rows else name),
        "lead": lead_band, "edge": edge_cents / 100.0, "cities": "all", "is_maker": is_maker,
        "desc": f"{name}: {len(rows)} trade events "
                f"({n_open} open · {n_resting} resting · {n_settled} settled). "
                + (f"Showing newest {len(trades)} of {len(rows)} (full: run-dir trades.jsonl). "
                   if len(trades) < len(rows) else "")
                + f"config {cfg_hash[:12] + '…' if cfg_hash else 'n/a'}.",
        "realized": realized, "mtm": mtm, "overall": round(realized + mtm, 4),
        "bands": bands or None,
        "eras": None,
        "return_pct": None, "trades_n": len(rows), "win_pct": win_pct,
        "avg_cap": None, "peak_cap": None, "budget": None,
        "n_settled": n_settled, "n_open": n_open, "n_resting": n_resting,
        "maker_placed": (n_resting + n_open) if is_maker else 0,
        "maker_filled": n_open if is_maker else 0,
        "maker_fill_pct": (round(100 * n_open / (n_open + n_resting), 1)
                           if is_maker and (n_open + n_resting) else None),
        "levers": (cfg_levers or []) + [
            ["Execution", exec_lbl, "How orders hit the book (taker crosses; maker rests)."],
            ["Edge", f"{edge_cents}¢", "|fair − mid| entry threshold."],
            ["Config hash", (cfg_hash[:12] + "…") if cfg_hash else "—",
             "The frozen config-of-record this row's trades were priced under."],
            ["Fees", f"${fees}", "Exact per-fill fees summed over the events."],
        ],
        "trades": trades,
    }


# ── backtest run dir → SPEC run (per-config drill) ────────────────────────────
def _strategy_from_run(run_dir: str, edge_cents: int) -> dict:
    meta = {}
    mp = os.path.join(run_dir, "run.json")
    if os.path.exists(mp):
        try:
            meta = json.load(open(mp))
        except Exception:
            meta = {}
    rows = _read_jsonl(os.path.join(run_dir, "trades.jsonl"))
    # order_id → {slug, city, target_date, lead} sidecar (run-written; pnl_ledger can't forward subject/date).
    sidecar = {}
    sp = os.path.join(run_dir, "signals.json")
    if os.path.exists(sp):
        try:
            sidecar = json.load(open(sp))
        except Exception:
            sidecar = {}
    run_id = meta.get("run_id") or os.path.basename(run_dir)
    cfg = meta.get("config") or {}
    cb = meta.get("cost_band") or {}
    # Run-of-record provenance folded in AFTER the strategy's actual knobs (models/central/threshold/mode/…).
    provenance = [
        ["Config hash", (meta.get("config_hash", "") or "")[:12] + "…",
         "sha256 of the config-of-record — the pre-registration anchor."],
        ["Code ref", (meta.get("code_ref", "unknown") or "unknown")[:10],
         "git HEAD the backtest ran on (silent code drift becomes impossible to miss)."],
        ["Env", ", ".join(f"{k}={v}" for k, v in (meta.get("env") or {}).items()) or "—",
         "Environment snapshot — the behavior-flag contamination guardrail."],
        ["Cost band", f"opt {cb.get('opt','—')} · base {cb.get('base','—')} · adv {cb.get('adv','—')}",
         "Whole-run P&L ($) under optimistic (fill-at-mid) / base (median half-spread) / "
         "adverse (p90 half-spread) fill-cost assumptions."],
        ["Queue model", ("maker: fill only when flow crosses the resting quote"
                         if cfg.get("mode") == "maker" else "taker: cross the book at submit"),
         "How fills are modeled for this exec mode."],
        ["Settles", ", ".join(f"{k}:{v}" for k, v in (meta.get("settle_source_counts") or {}).items()) or "—",
         "Resolution provenance (GAMMA vs CLOB) — proxy settles have no path."],
    ]
    cfg_levers = _config_levers(cfg, extra=provenance)
    strat = _agg_strategy(run_id, rows, edge_cents, sidecar=sidecar, cfg_levers=cfg_levers)
    strat["name"] = run_id
    # facets for the overview LEVER FILTER bar (filter strategies by lever values)
    _bt = cfg.get("bucket_type") or "all"
    _universe = "all markets"
    if cfg.get("universe"):
        _universe = f"allowlist ({len(cfg['universe'])})"
    strat["facets"] = {
        "engine": cfg.get("engine") or "—",
        "threshold": (f"{int(round((cfg.get('threshold') or 0) * 100))}¢" if cfg.get("threshold") else "—"),
        "bucket": {"all": "all buckets", "open": "open-ended (≥/≤)", "interior": "interior bands"}.get(_bt, _bt),
        "cities": _universe,
        "exec": cfg.get("mode") or "taker",
    }
    win = meta.get("window") or {}
    strat["start"] = _fmt_ts(win.get("min_ts"))
    if win.get("min_ts") and win.get("max_ts"):          # stated backtest period
        # window = first→last order SUBMIT ts, i.e. ENTRIES — not the scan window (a lane whose last
        # signal fired well before the run's scan end shows the entry date, not the scan end); label both
        _scan = cfg.get("window") or (meta.get("config") or {}).get("window") if isinstance(meta.get("config"), dict) else cfg.get("window")
        strat["period"] = (time.strftime("%Y-%m-%d", time.gmtime(win["min_ts"])) + " → "
                           + time.strftime("%Y-%m-%d", time.gmtime(win["max_ts"]))
                           + (f" (entries · scan {_scan})" if _scan else " (entries)"))
    if cfg.get("threshold"):                             # Edge col = the run's ACTUAL threshold, not EDGE_DEFAULT
        strat["edge"] = cfg["threshold"]
    strat["budget"] = meta.get("budget") or meta.get("starting_cash")
    strat["levers"] = cfg_levers   # replace the generic agg levers with the full config-expanded glossary
    strat["_meta"] = meta
    return strat


def _footer_html(metas: list, source: str) -> str:
    """Footer line from RunMeta(s): code_ref · config_hash · env · settle-source counts."""
    if not metas:
        return ""
    codes = sorted({(m.get("code_ref") or "unknown")[:10] for m in metas})
    hashes = sorted({(m.get("config_hash") or "")[:12] for m in metas if m.get("config_hash")})
    settle = {}
    env = {}
    for m in metas:
        for k, v in (m.get("settle_source_counts") or {}).items():
            settle[k] = settle.get(k, 0) + v
        ev = m.get("env")
        if isinstance(ev, dict):                 # probe rows carry a free-text env note instead of a dict
            for k, v in ev.items():
                env.setdefault(k, v)
        elif isinstance(ev, str) and ev:
            env.setdefault("note", ev)
    parts = [
        f"<b>code</b> {', '.join(codes)}",
        f"<b>config</b> {', '.join(hashes) if hashes else '—'}",
        f"<b>env</b> {', '.join(f'{k}={v}' for k, v in env.items()) or '—'}",
        f"<b>settles</b> {', '.join(f'{k}:{v}' for k, v in settle.items()) if settle else 'none'}",
    ]
    return " · ".join(parts)


# ── live extras: heartbeat/staleness banner · gate stamp · sim-vs-live deviation ──
def _pass_stamp() -> str:
    """The current gate scope, for the banner. ONLY the structured scoped PASS counts
    (executor.pass_scope — {scope, market_class, strategies, granted, expiry}, fail-closed on
    stale/undated). Returns '' when there is no valid PASS.

    A green pill REQUIRES a live-unlocking PASS.json — the same source the executor gates on — so a
    stale bare marker can never paint a false green 'gate PASS' while live is actually blocked."""
    try:
        sys.path.insert(0, REPO)
        from desk.live import executor  # noqa: E402
        p = executor.pass_scope()
        if p:
            return (f"{p.get('scope', '?')}/{p.get('market_class', '?')} "
                    f"· {len(p.get('strategies', []))} strat · exp {p.get('expiry', '?')}")[:70]
    except Exception:
        pass
    return ""


def _live_banner(dev_rows: list) -> str:
    pills = []
    # heartbeat / staleness
    hb = {}
    if os.path.exists(HEARTBEAT):
        try:
            hb = json.load(open(HEARTBEAT))
        except Exception:
            hb = {}
    now = int(time.time())
    hb_ts = hb.get("ts")
    if hb_ts:
        age = now - int(hb_ts)
        cls = "ok" if age < 3600 else ("warn" if age < 6 * 3600 else "bad")
        pills.append(f'<span class="bpill {cls}"><b>heartbeat</b> {_fmt_ts(hb_ts)} '
                     f'({age // 60}m ago)</span>')
    else:
        pills.append('<span class="bpill bad"><b>heartbeat</b> none — driver not seen</span>')
    # gate PASS stamp — the structured scoped PASS (executor.pass_scope). Fail-closed → "no PASS".
    scope = _pass_stamp()
    if scope:
        pills.append(f'<span class="bpill ok"><b>gate</b> PASS {scope}</span>')
    else:
        pills.append('<span class="bpill warn"><b>gate</b> no PASS — live blocked</span>')
    # mean sim-vs-live deviation
    devs = [abs(d) for d in dev_rows if d is not None]
    if devs:
        md = round(sum(devs) / len(devs), 1)
        cls = "ok" if md <= 15 else ("warn" if md <= 40 else "bad")
        pills.append(f'<span class="bpill {cls}"><b>sim↔live</b> mean |dev| {md} bps '
                     f'(n={len(devs)})</span>')
    else:
        pills.append('<span class="bpill warn"><b>sim↔live</b> no measured fills yet</span>')
    return "".join(pills)


def _apply_sim_vs_live(rows_by_strategy: dict) -> list:
    """Join per-order sim-vs-live deviations onto matching live TradeRows (by order_id) and return
    the deviation bps list for the banner. Deviation shows in each trade's note."""
    dev_bps = []
    runs = []
    if os.path.exists(SIM_VS_LIVE):
        try:
            runs = json.load(open(SIM_VS_LIVE)).get("runs", [])
        except Exception:
            runs = []
    by_oid = {}
    for r in runs:
        dev = r.get("deviation") or {}
        oid = r.get("order_id") or r.get("ts")
        by_oid[oid] = r
        if dev.get("price_bps") is not None:
            dev_bps.append(dev["price_bps"])
    # annotate matching trades
    for rows in rows_by_strategy.values():
        for tr in rows:
            match = by_oid.get(tr.get("order_id"))
            if match:
                dev = match.get("deviation") or {}
                sim, live = match.get("sim", {}), match.get("live", {})
                ex = tr.setdefault("extras", {})
                ex["note"] = (f"SIM fill {sim.get('avg_fill_price')} vs LIVE {live.get('avg_fill_price')} "
                              f"· deviation {dev.get('price_bps', '—')}bps")
    return dev_bps


# ── build the three lanes ─────────────────────────────────────────────────────
EDGE_DEFAULT = 10

ABLATION_DESIGN = """<details style='margin:10px 0;border:1px solid #262d39;border-radius:8px;padding:10px 14px'>
<summary style='cursor:pointer;color:#e0a44a;font-weight:600'>Ablation design — how the grid is read</summary>
<div style='font-size:13px;line-height:1.55;color:#c9c4b6;margin-top:8px'>
Each row is one <b>run_id</b> under <code>desk/eval/runs/</code> — one config of the strategy over the capture window. Ideally the grid runs ONE detection pass per (engine, threshold); every other lever is a post-detection <i>slice</i> of the same trades, so cells are comparable rather than re-optimized.<br><br>
<b>Costs</b>: the execution cost model is never zero — each cell is scored at <b>base</b> (median half-spread + fee) AND <b>adverse</b> (p90 half-spread). A candidate should clear the promotion gate at BOTH bands (net&gt;0 base AND adverse), with a clustered-by-date t-stat and enough distinct settle dates, before it earns paper — then a small live lane measures whether the real execution cost matches the model.<br>
Expand a row for its trades, then a trade for its full pricing decomposition.
</div></details>"""



def build_backtest(run=None, strategy=None, grid=False) -> dict:
    dirs = []
    if run:
        dirs = [os.path.join(RUNS_DIR, run)]
    elif os.path.isdir(RUNS_DIR):
        dirs = sorted(os.path.join(RUNS_DIR, d) for d in os.listdir(RUNS_DIR)
                      if os.path.isdir(os.path.join(RUNS_DIR, d)))
    strategies, metas = [], []
    for d in dirs:
        if not os.path.exists(os.path.join(d, "run.json")) and not os.path.exists(os.path.join(d, "trades.jsonl")):
            continue
        s = _strategy_from_run(d, EDGE_DEFAULT)
        if strategy and (s.get("_meta") or {}).get("strategy") != strategy:
            continue
        metas.append(s.pop("_meta", {}))
        strategies.append(s)
    tot = round(sum((s.get("overall") or 0) for s in strategies), 2)
    return {
        "meta": {
            "title": "Backtest <span class='em'>ablation</span>",
            "subtitle": f"{len(strategies)} run(s) from desk/eval/runs/ · one row per config/run — "
                        "expand for its trades, then a trade for its full pricing decomp.",
            "source_kind": "backtest", "n_cycles": len(strategies),
            "built": _built(), "tz": desk_tz_name(),
            "footnote": (ABLATION_DESIGN + f"{len(strategies)} runs · total overall ${tot}. " + _footer_html(metas, "backtest")),
        },
        "_edge_cents": {s["name"]: int(round((s.get("edge") or EDGE_DEFAULT / 100.0) * 100))
                        for s in strategies},
        "strategies": strategies,
    }


def _group_trades(rows: list) -> dict:
    by = {}
    for r in rows:
        by.setdefault(r.get("strategy") or "unknown", []).append(r)
    return by


def _lane_meta(strategies: list) -> None:
    """Join registry facts every lane card shows: start date (registered_at), capital cap
    (params.budget), trades/day so far, and the live⇄paper twin linkage (a registry row may name its
    twin lane via params.twin)."""
    try:
        reg = {r.get("name"): r for r in (json.load(open(STRATEGY_CONFIG)) or {}).get("strategies", [])}
    except Exception:
        reg = {}
    today = datetime.now(tz=TZ).date()
    for s in strategies:
        row = reg.get(s.get("name")) or {}
        p = row.get("params") or {}
        if s.get("budget") is None:                  # _agg emits budget:None — setdefault can't fix that
            s["budget"] = p.get("budget")
        if row.get("registered_at"):
            s["start"] = row["registered_at"]
            try:
                d0 = datetime.strptime(row["registered_at"], "%Y-%m-%d").date()
                days = max(1, (today - d0).days + 1)
                s["trades_per_day"] = round((s.get("trades_n") or 0) / days, 2)
            except Exception:
                pass
        twin = p.get("twin")                         # registry row may name its live/paper twin lane
        if twin and twin in reg:
            s["twin"] = twin


def build_paper() -> dict:
    rows = [r for r in _read_jsonl(TRADES) if r.get("source") == "paper"]
    by = _group_trades(rows)
    strategies = [_agg_strategy(name, rs, EDGE_DEFAULT,
                                cfg_levers=_config_levers(_strategy_config_row(name)))
                  for name, rs in sorted(by.items())]
    for s in strategies:                     # Edge/Fair cols = the lane's REGISTERED config, not defaults
        p = _strategy_config_row(s["name"]) or {}    # returns the PARAMS dict directly
        if p.get("entry_threshold"):
            s["edge"] = p["entry_threshold"]
        if p.get("engine"):
            s["fair"] = p["engine"]
        s.pop("eras", None)                  # era split is a backtest-only concept —
                                             # every forward trade is a real-time driver fill
    # REGISTERED lanes with no fills yet still render — an empty forward page would hide that sealed
    # specs are armed and firing on every scan window. A live lane's paper twin shows here too (same
    # cell): the forward lane starts the moment the live lane does.
    try:
        reg = (json.load(open(STRATEGY_CONFIG)) or {}).get("strategies", [])
    except Exception:
        reg = []
    for row in reg:
        name = row.get("name", "")
        if not row.get("enabled") or name in by or not name:
            continue
        p = row.get("params") or {}
        is_live = bool(p.get("live"))
        strategies.append({
            "name": name, "exec": p.get("mode", "taker"),
            "fair": p.get("engine", ""), "lead": [p.get("lead_min"), p.get("lead_max")],
            "edge": p.get("entry_threshold"), "cities": "all", "is_maker": p.get("mode") == "maker",
            "desc": (("Live lane — paper twin fires in the SAME driver pass. " if is_live else "")
                     + "Registered & armed; no signal has cleared the entry gauntlet yet "
                     + f"(threshold {p.get('entry_threshold')}, bucket {p.get('bucket_type', 'all')}). "
                     + (row.get("_note") or "")[:180]),
            "realized": 0.0, "mtm": 0.0, "overall": 0.0, "return_pct": None,
            "trades_n": 0, "win_pct": None, "avg_cap": None, "peak_cap": None,
            "budget": p.get("budget"), "n_settled": 0, "n_open": 0, "n_resting": 0,
            "maker_placed": 0, "maker_filled": 0, "maker_fill_pct": None,
            "levers": _config_levers({**(row.get("params") or {}), "budget": p.get("budget")}), "trades": [],
        })
    metas = [{"code_ref": rs[0].get("extras", {}).get("code_ref"),
              "config_hash": rs[0].get("config_hash"),
              "env": rs[0].get("extras", {}).get("env") or {},
              "settle_source_counts": _settle_from_rows(rs)} for name, rs in sorted(by.items())]
    _lane_meta(strategies)
    tot = round(sum((s.get("overall") or 0) for s in strategies), 2)
    return {
        "meta": {
            "title": "Forward <span class='em'>test</span>",
            "subtitle": f"{len(strategies)} paper strategy(ies) on the converged driver · settled-only "
                        f"realized + open MTM. {len(rows)} trade events.",
            "source_kind": "forward", "n_cycles": len(rows),
            "cadence": _cadence_note("paper"),
            "built": _built(), "tz": desk_tz_name(),
            "footnote": (f"{len(strategies)} strategies · total overall ${tot}. " + _footer_html(metas, "paper")),
        },
        "_edge_cents": {s["name"]: int(round((s.get("edge") or EDGE_DEFAULT / 100.0) * 100))
                        for s in strategies},
        "strategies": strategies,
    }


def _cadence_note(source: str = "") -> str:
    """Compact scan status: last scan · orders it placed · next scan. `source` scopes the order count
    to the page (the live page must not count paper orders, and vice-versa)."""
    try:
        reg = (json.load(open(STRATEGY_CONFIG)) or {}).get("strategies", [])
        times = sorted({t for r in reg if r.get("enabled")
                        for t in (r.get("schedule") or {}).get("times_utc", [])})
    except Exception:
        times = []
    if not times:
        return ""
    import calendar
    now = time.time()
    g = time.gmtime(now)
    stamps = []
    for d_off in (-1, 0, 1):
        base = calendar.timegm((g.tm_year, g.tm_mon, g.tm_mday, 0, 0, 0, 0, 0, 0)) + d_off * 86400
        for t in times:
            hh, mm = t.split(":")
            stamps.append(base + int(hh) * 3600 + int(mm) * 60)
    last = max((s for s in stamps if s <= now), default=None)
    nxt = min((s for s in stamps if s > now), default=None)
    if not last or not nxt:
        return ""
    n = sum(1 for r in _read_jsonl(TRADES)
            if r.get("status") in ("open", "resting") and (r.get("ts") or 0) >= last - 60
            and (not source or r.get("source") == source))
    f = lambda ts: (lambda dt: dt.strftime("%H:%M ") + tz_label(dt))(datetime.fromtimestamp(ts, tz=TZ))
    lbl = {"live": "live order(s)", "paper": "paper order(s)"}.get(source, "order(s)")
    return f"last scan {f(last)} · {n} {lbl} placed · next scan {f(nxt)}"


def _settle_from_rows(rows: list) -> dict:
    c = {}
    for r in rows:
        if r.get("status") == "closed":
            src = (r.get("extras") or {}).get("settle_source", "GAMMA")
            c[src] = c.get(src, 0) + 1
    return c


def build_live() -> dict:
    rows = [r for r in _read_jsonl(TRADES) if r.get("source") == "live"]
    by = _group_trades(rows)
    dev_bps = _apply_sim_vs_live(by)
    strategies = [_agg_strategy(name, rs, 20,
                                cfg_levers=_config_levers(_strategy_config_row(name)))
                  for name, rs in sorted(by.items())]
    for s in strategies:                     # Edge/Fair cols = the arm's REGISTERED config, not defaults
        p = _strategy_config_row(s["name"]) or {}    # returns the PARAMS dict directly
        if p.get("entry_threshold"):
            s["edge"] = p["entry_threshold"]
        if p.get("engine"):
            s["fair"] = p["engine"]
        s.pop("eras", None)                  # era split is a backtest-only concept (see build_paper)
    # if no live trades yet, keep the page present with a clear empty state
    metas = [{"code_ref": rs[0].get("extras", {}).get("code_ref"),
              "config_hash": rs[0].get("config_hash"),
              "env": rs[0].get("extras", {}).get("env") or {},
              "settle_source_counts": _settle_from_rows(rs)} for name, rs in sorted(by.items())]
    try:
        reg = (json.load(open(STRATEGY_CONFIG)) or {}).get("strategies", [])
    except Exception:
        reg = []
    for row in reg:                                     # ARMED live lanes render even before their first fill
        name = row.get("name", "")
        p = row.get("params") or {}
        if not row.get("enabled") or not p.get("live") or name in by or not name:
            continue
        strategies.append({
            "name": name, "exec": p.get("mode", "taker"), "fair": p.get("engine", ""),
            "lead": [p.get("lead_min"), p.get("lead_max")], "edge": p.get("entry_threshold"),
            "cities": "all", "is_maker": p.get("mode") == "maker",
            "desc": ("Live lane — armed & firing on scan windows; no real order has cleared the "
                     f"entry gauntlet yet (threshold {p.get('entry_threshold')}, bucket "
                     f"{p.get('bucket_type', 'all')}). " + (row.get("_note") or "")[:180]),
            "realized": 0.0, "mtm": 0.0, "overall": 0.0, "return_pct": None,
            "trades_n": 0, "win_pct": None, "avg_cap": None, "peak_cap": None,
            "budget": p.get("budget"), "n_settled": 0, "n_open": 0, "n_resting": 0,
            "maker_placed": 0, "maker_filled": 0, "maker_fill_pct": None,
            "levers": _config_levers({**(row.get("params") or {}), "budget": p.get("budget")}), "trades": [],
        })
    _lane_meta(strategies)
    sub = (f"{len(strategies)} live strategy(ies) · {len(rows)} real order event(s)."
           if rows else "No live trades yet — a live lane populates this page once it fires a real order.")
    return {
        "meta": {
            "title": "Live <span class='em'>trade</span>",
            "subtitle": sub, "source_kind": "live", "n_cycles": len(rows),
            "cadence": _cadence_note("live"),
            "built": _built(), "tz": desk_tz_name(),
            "banner": _live_banner(dev_bps),
            "footnote": ("Real-money orders through the SAME engine (executor/LiveFillSource). Each row's "
                         "note shows sim-predicted vs live-actual fill (the credibility line). "
                         + _footer_html(metas, "live")),
        },
        "_edge_cents": {s["name"]: int(round((s.get("edge") or 0.20) * 100)) for s in strategies},
        "strategies": strategies,
    }


def build_spec(source: str, run=None, strategy=None, grid=False) -> dict:
    if source == "backtest":
        return build_backtest(run=run, strategy=strategy, grid=grid)
    if source == "paper":
        return build_paper()
    if source == "live":
        return build_live()
    raise SystemExit(f"unknown --source {source}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=["backtest", "paper", "live"])
    ap.add_argument("--run", help="backtest: a single run_id under desk/eval/runs/")
    ap.add_argument("--strategy", help="backtest: filter run dirs by RunMeta.strategy")
    ap.add_argument("--grid", action="store_true", help="backtest: all run dirs (the ablation)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    spec = build_spec(a.source, run=a.run, strategy=a.strategy, grid=a.grid)
    out = render(spec, a.out)
    print(f"[ledger] --source {a.source} → {out} | {len(spec['strategies'])} strategy(ies), "
          f"{sum(len(s['trades']) for s in spec['strategies'])} trades")


if __name__ == "__main__":
    main()
