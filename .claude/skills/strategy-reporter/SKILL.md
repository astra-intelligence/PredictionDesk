---
name: strategy-reporter
description: Produce a per-strategy run report — an executive summary, how/why it works, open positions, setup anomalies, and improvement suggestions — as Markdown and PDF. Use when asked to report on a strategy's run, explain what a strategy is doing and where it might be wrong, or generate a shareable run write-up. Wraps desk/reports/run_report.py (the deterministic scaffold + anomaly flags) with the qualitative narrative.
---

# Strategy reporter

Turns one strategy's run record into a readable report. The deterministic scaffold comes from
`desk/reports/run_report.py`; you add the qualitative narrative and convert to PDF.

## Procedure
1. Build the scaffold:
   ```
   python3 -c "from desk.reports.run_report import generate; print(generate(strategy='calendar')['md'])"
   ```
   `generate(strategy=NAME)` writes `desk/reports/out/<NAME>_run_<date>.{md,json}` — the executive
   summary (realized/unrealized/fees, open positions, runs logged), the open-positions table, and the
   deterministic **setup anomalies** (extreme entry edge, selling a near-certain outcome, …).
2. Read the strategy's playbook if it ships one (`desk/signals/<strategy>/playbook.md`) plus the run's
   `.json`, and fill the narrative sections the scaffold leaves as placeholders:
   - **How / why it works** — the edge in plain language, grounded in the playbook + this run's trades.
   - **Improvement suggestions** — from the anomalies + metrics; concrete, not generic.
3. Convert the finished Markdown to PDF via the `pdf` skill.

## Rules
- The scaffold's numbers are authoritative — never restate a different figure in the narrative.
- Every anomaly the scaffold flags must be addressed (explained or actioned), not dropped.
- Default strategy is `calendar` (the shipped sample); pass the real strategy name when reporting one.
- No network. The report reads the persisted ledger + run json only.
