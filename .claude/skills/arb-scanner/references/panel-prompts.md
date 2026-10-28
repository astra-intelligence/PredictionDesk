# Panel role prompts (Step 11) — copy/adapt per idea

One INDEPENDENT sub-agent per idea (never the author) runs these in sequence. It first runs the Step-10
fact-check gate (`critique.md`); on PASS it simulates the panel below and emits the `deliberation` object
(`deliberation.md` schema). On FAIL it stops (no panel) and the idea is rejected.

Give every role: the idea's claims, the verified live price (p_market), and web + Gamma/CLOB access.
Probabilities are always for the SAME outcome whose market price = p_market.

## News / Evidence (retrieval + verification loop)
"Gather the most recent, decision-relevant facts for this claim from primary/high-quality sources.
For EACH fact: cite it and confirm it independently; DROP anything you cannot verify. Loop until the set
is clean. Output a 1–2 sentence evidence summary + the load-bearing verified facts."

## Forecasters (run 3–5, diverse — each emits ONE number)
Each: "As the **<lens>** forecaster, give your INDEPENDENT fair probability p_fair (0–1) for the outcome,
+ one line of reasoning. Do NOT anchor on the market price."
- **base-rate (outside view)** — reference class / historical frequency.
- **news (inside view)** — the specific current situation and latest developments.
- **efficient-market skeptic** — assume the crowd is mostly right; justify any deviation.
- **quant model** — for price/level markets: live spot + realized vol → lognormal P(barrier); state σ, days.
- **contrarian** — where is the consensus systematically biased (favorite-longshot, recency)?

## Debate (iterate to stability, ≤3 rounds)
- **Bull**: "Argue the market price is too LOW (true p higher). Strongest evidence + scenario."
- **Bear**: "Argue the market price is too HIGH (true p lower). Strongest objection — flag it." (flag=true)
- After each round, forecasters UPDATE their p_fair seeing the debate + each other's numbers. Stop when
  the spread stops moving or at round 3. Record `rounds`.
- **Resolution-risk specialist** (layer MONEY/GATE): can the rule resolve against you on a technicality,
  timing, source, or dispute? flag if material.
- **Execution/liquidity specialist** (layer MONEY): is there real depth at the maker entry? capacity?

## Aggregate
median of the UPDATED forecaster p_fair = `p_fair_agg`; half the inter-quartile spread = `dispersion`.

## Judge
"Mispricing = p_fair_agg − p_market, NET of fee + spread + a dispersion haircut. Apply resolution-risk +
capacity gates. Decide TRADE / WATCH / REJECT and set conviction 0–100 per the rubric (edge × agreement ×
evidence × capacity × low-res-risk). Give a one-line rationale and name the single sharpest contention."
