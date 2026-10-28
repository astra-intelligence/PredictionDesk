# Deliberation panel — quantitative forecasting + debate, layered ON TOP of the fact-check gate

The fact-check (Step 10, `critique.md`) answers **"is this factually true?"** — necessary but not the
real question for a prediction market. The deliberation panel answers **"is the market mispricing the
odds, and by how much?"** It runs **only on fact-check survivors**, as a SEPARATE sub-agent per idea
(never the idea's author). It produces a quantitative **fair probability** and a Judge decision, not a
yes/no vote.

Architecture (grounded in forecasting research):
- **Halawi 2024** (arXiv 2402.18563): retrieve (news) → reason → aggregate (trimmed-mean). = the
  "news/evidence agent → verification loop" front end.
- **Schoenegger 2024** (arXiv 2402.19379): a *diverse* LLM forecaster ensemble, median-aggregated;
  estimates improve when agents see each other's → supports debate-and-update.
- **Du 2023 / LLM-judge debate**: diverse agents propose/critique/revise, iterate to stability, a Judge
  decides. Each agent emits a **probability**, not a stance; the Judge computes the mispricing.

## Layers (each a distinct voice; one sub-agent simulates the panel)
```
EVIDENCE   news/retrieval + VERIFICATION LOOP — gather recent source-grounded facts, verify each,
           drop the unverifiable, loop until clean.                          → evidence:{note}
FORECAST   3–5 DIVERSE lenses, each an INDEPENDENT fair probability p_fair_i + 1-line reasoning:
           base-rate(outside) · news(inside) · efficient-market skeptic · quant model
           (price markets: live spot + realized vol → lognormal tail) · contrarian.
                                                                              → forecasts:[{lens,p_fair,reasoning}]
DEBATE     Bull (p too LOW) vs Bear (p too HIGH); forecasters UPDATE; iterate until dispersion < thresh
           or round cap (≤3). Resolution-risk + Execution/liquidity specialists feed in.
                                                                              → panel:[{agent,layer,verdict,note,flag}]
AGGREGATE  p_fair_agg = median of UPDATED p_fair; dispersion = half the spread band (a probability).
                                                                              → p_fair_agg, dispersion, p_market, rounds
JUDGE      mispricing = p_fair_agg − p_market, NET of fee + spread + a dispersion haircut; apply
           resolution-risk + capacity gates → TRADE / WATCH / REJECT + conviction 0–100 + rationale.
                                                                              → judge:{decision,conviction,rationale}
```
Only **TRADE / WATCH** reach the dashboard; **REJECT** → "Rejected" with the decisive reason.

## Conviction rubric (the Judge owns it — replaces any generator number)
Conviction rises with: bigger |edge| net of cost · tighter forecaster **agreement** (low dispersion) ·
better **evidence** quality · real **capacity** (depth) · **low resolution risk**. It falls with wide
dispersion, thin depth, definitional/timing ambiguity, or an edge inside costs. Same edge + wide
dispersion → lower conviction than same edge + tight agreement (a hard requirement — test it).

## `deliberation` output schema (the dashboard renders this verbatim)
```
deliberation: {
  verifier:   {status:"PASS"|"FAIL", note},          # mirrors the Step-10 gate
  evidence:   {note},                                 # optional
  forecasts:  [{lens, p_fair (0–1), reasoning}],      # the ensemble dots on the gauge
  panel:      [{agent, layer:"DEBATE"|"MONEY", verdict, note, flag:bool}],  # bull/bear/res/exec
  rounds:     <int>,
  p_market:   <0–1>,                                  # verified live price of the SAME outcome
  p_fair_agg: <0–1>,                                  # median of updated forecasts
  dispersion: <0–1>,                                  # half-band (drives the green band + gauge)
  contention: "<sharpest bull-vs-bear point>",
  judge:      {decision:"TRADE"|"WATCH"|"REJECT", conviction:0–100, rationale}
}
```
Idea-level `conviction` = `judge.conviction`. `market_price`/`fair_value`/`edge_net` are refreshed to the
verified panel numbers. **SIGNAL ideas** (backtest hypotheses) OMIT `p_market`/`p_fair_agg`/`dispersion`
(so no mispricing gauge renders); keep a light `panel` + `judge` (TRADE = worth backtesting).

Per-role prompt templates: `references/panel-prompts.md`.
