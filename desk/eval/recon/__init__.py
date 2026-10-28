"""desk/eval/recon — the calibration loop (pin the engine to reality).

`golden_replay` replays the engine over the recorded book and diffs each fill against REAL
captured fills → `deviation_report.json`. The `recon-evaluator` skill diagnoses each deviation
(BUG vs PHYSICAL residual) from independent evidence, never by tuning to match; fees are never
tuned. A scoped PASS verdict (median ≤15 bps · p90 ≤40 bps · fees exact · no open BUG) is the ONLY
thing that may unlock real money. Process: skills.md.
"""
