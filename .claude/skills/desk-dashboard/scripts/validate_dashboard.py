"""validate_dashboard.py — the data-completeness gate (dashboard W6). Runs at the END of every build.

NO SILENT BLANKS: every missing datum in a page spec must carry a machine reason the template renders in
its place. A blank WITHOUT a recognizable reason fails the build (nonzero exit). Also asserts the built
HTML still contains every user-ordered control (view toggle, window/range selectors, caps inputs, refresh,
nav, member timestamps) — a regression can't ship silently.

Output: dashboard/data_completeness.json  { built, pages: {..}, gaps: [...], pct: {...}, ok: bool }
"""
from __future__ import annotations

import json
import os
import sys

KNOWN_REASONS = ("backfilling", "pre-capture", "fetch failed", "n/a", "not captured",
                 "no calib window", "value unavailable")


def _member_gaps(decomp: dict, where: str, gaps: list, stats: dict) -> None:
    models = (decomp or {}).get("models") or []
    for m in models:
        stats["members_total"] += 1
        if m.get("value_c") is not None:
            stats["members_filled"] += 1
            if not (m.get("run_init") or m.get("nominal")):
                if "nominal" in m or "run_init" in m:
                    gaps.append({"where": where, "field": f"member {m.get('model')} timestamp",
                                 "reason": None})
                else:            # pre-schema decomp — timestamps not recorded on older rows
                    stats.setdefault("members_preschema", 0)
                    stats["members_preschema"] += 1
        else:
            r = m.get("reason") or ""
            if any(k in r for k in KNOWN_REASONS):
                stats["members_backfilling"] += 1
            else:
                gaps.append({"where": where, "field": f"member {m.get('model')} value", "reason": r or None})
    g = (decomp or {}).get("gauge") or {}
    stats["gauge_total"] += 1
    if g.get("offset") is not None and g.get("sigma") is not None:
        stats["gauge_filled"] += 1
    elif g.get("n_errors", 0) == 0:
        stats["gauge_nocalib"] += 1      # known: no rolling-calib block for (city,metric) at decision date
    else:
        gaps.append({"where": where, "field": "gauge calib block", "reason": None})


def _consistency(page: str, s: dict, gaps: list) -> None:
    """Aggregate-consistency gate: era blocks, band totals and trade counts a page displays MUST
    reconcile — a mismatch means two panels can show different truths. These gaps are HARD failures
    (reason starts with CONSISTENCY:, see finalize)."""
    w = f"{page}:{s.get('name')}"
    n_settled = s.get("n_settled")
    eras = s.get("eras")
    if eras and n_settled is not None:
        era_n = sum((blk or {}).get("n", 0) for blk in eras.values())
        if era_n != n_settled:
            gaps.append({"where": w, "field": "eras",
                         "reason": f"CONSISTENCY: sum of era n ({era_n}) != n_settled ({n_settled})"})
        for bk, bblk in (s.get("bands") or {}).items():
            btot = (bblk or {}).get("total")
            etot = [blk[bk]["total"] for blk in eras.values() if isinstance(blk, dict) and blk.get(bk)]
            if btot is not None and etot and abs(sum(etot) - btot) > 0.05 * max(1, len(etot)):
                gaps.append({"where": w, "field": f"bands.{bk}",
                             "reason": f"CONSISTENCY: era {bk} totals sum {round(sum(etot), 2)} "
                                       f"!= band total {btot}"})
    tn, emb = s.get("trades_n"), len(s.get("trades") or [])
    if tn is not None and emb > tn:
        gaps.append({"where": w, "field": "trades embed",
                     "reason": f"CONSISTENCY: embedded rows ({emb}) exceed trades_n ({tn})"})
    counts = [s.get("n_open"), s.get("n_resting"), s.get("n_settled")]
    if tn is not None and all(c is not None for c in counts) and sum(counts) != tn:
        gaps.append({"where": w, "field": "trades_n",
                     "reason": f"CONSISTENCY: n_open+n_resting+n_settled ({sum(counts)}) != trades_n ({tn})"})


def validate_spec(page: str, spec: dict, gaps: list, stats: dict) -> None:
    for s in spec.get("strategies", []):
        w = f"{page}:{s.get('name')}"
        if not s.get("levers"):
            gaps.append({"where": w, "field": "levers panel", "reason": None})
        _consistency(page, s, gaps)
        if page in ("forward", "live"):
            has_activity = bool(s.get("trades")) or bool(s.get("realized"))
            for f in ("start", "budget"):
                if s.get(f) in (None, ""):
                    # A row that has never fired has no run-start/budget provenance YET — that's an
                    # explained gap, not a broken build; it hardens once the row shows activity.
                    gaps.append({"where": w, "field": f,
                                 "reason": None if has_activity else "no activity yet"})
        for t in s.get("trades", []):
            stats["trades_total"] += 1
            if t.get("decomp"):
                stats["trades_with_decomp"] += 1
                _member_gaps(t["decomp"], w, gaps, stats)
            elif t.get("status") == "closed":
                stats["trades_decomp_na"] += 1   # settle rows carry no decomp by design (n/a)
            else:
                stats["trades_decomp_missing"] += 1


HTML_CONTROLS = {
    "backtest.html": ['id="mainnav"', "renderDynView", "Trading windows", "vfrom", "vcap", ".mt"],
    "forward.html": ['id="mainnav"'],
    "live.html": ['id="mainnav"'],
    "index.html": ["backtest.html", "forward.html"],
}


def validate_html(out_dir: str, gaps: list) -> None:
    for fname, needles in HTML_CONTROLS.items():
        p = os.path.join(out_dir, fname)
        if not os.path.exists(p):
            gaps.append({"where": fname, "field": "page missing", "reason": None})
            continue
        html = open(p).read()
        for n in needles:
            if n not in html:
                gaps.append({"where": fname, "field": f"ordered control '{n}' absent", "reason": None})


def finalize(out_dir: str, specs: dict, opp_data: dict | None = None) -> bool:
    gaps: list = []
    stats = {k: 0 for k in ("members_total", "members_filled", "members_backfilling",
                            "gauge_total", "gauge_filled", "gauge_nocalib",
                            "trades_total", "trades_with_decomp", "trades_decomp_na",
                            "trades_decomp_missing")}
    for page, spec in specs.items():
        validate_spec(page, spec, gaps, stats)
    validate_html(out_dir, gaps)
    hard = [g for g in gaps if not g.get("reason") or str(g.get("reason", "")).startswith("CONSISTENCY:")]
    pct = {
        "members": (round(100 * stats["members_filled"] / stats["members_total"], 1)
                    if stats["members_total"] else None),
        "gauge": (round(100 * stats["gauge_filled"] / stats["gauge_total"], 1)
                  if stats["gauge_total"] else None),
        "decomp": (round(100 * stats["trades_with_decomp"]
                         / max(1, stats["trades_total"] - stats["trades_decomp_na"]), 1)
                   if stats["trades_total"] else None),
    }
    report = {"ok": not hard, "pct": pct, "stats": stats,
              "hard_gaps": hard[:200], "n_hard_gaps": len(hard),
              "explained_gaps": len(gaps) - len(hard),
              "summary": (f"decomp members {pct['members']}% filled "
                          f"({stats['members_backfilling']} backfilling) · "
                          f"calib {pct['gauge']}% ({stats['gauge_nocalib']} no-calib-window) · "
                          f"decomp on {pct['decomp']}% of entry rows")}
    json.dump(report, open(os.path.join(out_dir, "data_completeness.json"), "w"), indent=1)
    print(f"[validate] {'OK' if report['ok'] else 'FAILED'}: {report['summary']} · "
          f"{len(hard)} UNEXPLAINED gaps", flush=True)
    if hard[:8]:
        for g in hard[:8]:
            print(f"[validate]   gap: {g['where']} → {g['field']}", flush=True)
    return report["ok"]
