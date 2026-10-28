"""reports/run_report.py — shared per-run report harness.

Mechanical + DRY: loads a strategy's run record (ledger line, open positions, fresh metrics),
runs deterministic ANOMALY detection, and emits a structured report (dict + Markdown + json).
The strategy-reporter SKILL adds the qualitative narrative (reading the strategy's playbook)
and converts the report to PDF via the `pdf` skill. One harness serves every strategy.
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, Optional

from desk.data.store import Store
from desk.runtime.paths import data_path
from desk.tracker.pnl_ledger import PnLLedger

OUT_DIR = data_path("reports", "out")   # env-resolved (DESK_DATA_DIR) / __file__-anchored, never cwd


def _positions(strategy: str) -> Dict[str, dict]:
    path = data_path("driver", f"positions_{strategy}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def detect_anomalies(positions: Dict[str, dict]) -> list:
    """Deterministic setup-anomaly checks (the report's red flags)."""
    out = []
    for key, p in positions.items():
        e = p.get("entry_edge", 0.0)
        if abs(e) > 0.5:
            out.append(f"extreme entry_edge {round(e,3)} on {key[:24]} — likely an outcome/mapping "
                       f"error or YES/NO confusion; CONFIRM the resolution rule before sizing")
        if p.get("entry_price", 0) >= 0.97 and (p.get("qty", 0) < 0):
            out.append(f"selling a near-certain outcome (px {p['entry_price']:.3f}) on {key[:24]} — "
                       f"verify the outcome bounds/rounding are right")
    return out


def build_record(strategy: str = "calendar") -> dict:
    ledger = PnLLedger().snapshot()
    line = ledger.by_strategy.get(strategy, {}) if hasattr(ledger, "by_strategy") else {}
    positions = _positions(strategy)
    return {
        "strategy": strategy,
        "generated_ts": int(time.time()),
        "ledger_line": line,
        "runs_logged": getattr(ledger, "runs_logged", 0),
        "open_positions": positions,
        "n_open": len(positions),
        "anomalies": detect_anomalies(positions),
    }


def render_markdown(record: dict, playbook: str = "") -> str:
    L = record.get("ledger_line", {})
    lines = [f"# {record['strategy']} — run report",
             f"_generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(record['generated_ts']))}_", "",
             "## 1. Executive summary",
             f"- realized PnL: **{L.get('realized', 0)}**  unrealized: {L.get('unrealized', 0)}  fees: {L.get('fees', 0)}",
             f"- open positions: **{record['n_open']}**   runs logged: {record['runs_logged']}", "",
             "## 2. How / why it works",
             "_(LLM narrative inserted by strategy-reporter from playbook + this run)_", "",
             "## 3. Open positions"]
    if record["open_positions"]:
        lines += ["| market\\|token | qty | entry_px | entry_edge |", "|---|---|---|---|"]
        for k, p in record["open_positions"].items():
            lines.append(f"| {k[:30]} | {round(p.get('qty',0),2)} | {round(p.get('entry_price',0),4)} | {round(p.get('entry_edge',0),3)} |")
    else:
        lines.append("_none_")
    lines += ["", "## 4. Setup anomalies / red flags"]
    lines += [f"- ⚠️ {a}" for a in record["anomalies"]] or ["- none detected"]
    lines += ["", "## 5. Improvement suggestions",
              "_(LLM, from anomalies + metrics)_", "",
              "## Appendix — strategy playbook", playbook or "_(playbook not loaded)_"]
    return "\n".join(lines)


def generate(strategy: str = "calendar", out_dir: str = OUT_DIR) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    record = build_record(strategy)
    # optional playbook shipped inside the strategy's TENANT package (manifest "playbook" path,
    # resolved through the registry — variants resolve to their base; builtins/absent -> none).
    playbook = ""
    from desk.signals.registry import playbook_for
    pb_path = playbook_for(strategy)
    if pb_path and os.path.exists(pb_path):
        with open(pb_path) as f:
            playbook = f.read()
    md = render_markdown(record, playbook)
    stamp = time.strftime("%Y-%m-%d", time.gmtime(record["generated_ts"]))
    base = os.path.join(out_dir, f"{strategy}_run_{stamp}")
    # md + json only. The dashboard (desk-dashboard skill, `ledger` adapter) is the ONE HTML surface;
    # the strategy-reporter skill converts this md to PDF.
    with open(base + ".md", "w") as f:
        f.write(md)
    with open(base + ".json", "w") as f:
        json.dump(record, f, indent=2)
    return {"record": record, "md": base + ".md", "json": base + ".json"}


if __name__ == "__main__":
    r = generate()
    print(json.dumps({"md": r["md"], "json": r["json"], "anomalies": r["record"]["anomalies"]}, indent=2))
