# Directional opportunities — trading a view, not a lock

Arb/RV exploits price-vs-logic. Directional exploits **price-vs-reality**: the market's implied
probability is wrong versus a well-reasoned estimate. This is a **bet, not a lock** — you can lose.
Treat it as such: ground every probability in a source, state your confidence, and size small.

This is LLM-judgment work, not a formula. Lead with qualitative reasoning grounded in **web research**;
a quantitative estimate can support it, but the reasoning drives. Never fabricate a number — every
fair-value estimate must trace to a cited source (a poll, a base rate, a forecast, a news item, a
data release). If you can't ground it, don't claim an edge.

## Where directional edges actually are on Polymarket (documented)

1. **Favorite-longshot bias.** Retail systematically **overpays for extreme longshots** (low-priced
   YES) and **overpays for near-certainty** too, leaving **mid-range probabilities underpriced**.
   - Trade: *fade* a longshot you believe is even more unlikely than its price (buy its NO / sell YES);
     *buy* an underpriced favorite/mid you believe is more likely than its price.
   - It's a tendency, not a guarantee — you still need a reason THIS contract is mispriced.
2. **Small / inefficient markets.** Markets under ~$100k liquidity are materially less accurate (~61%).
   A confident, well-researched view is more likely to beat the crowd here than in a $10M market.
   But depth is thin — size to the book and expect to move the price.
3. **Research-grounded fair value.** Pick a concrete market, research the real-world question (base
   rates, polls, forecasts, official data, recent news), form a probability with a confidence band,
   and trade only if your estimate differs from the executable price by **more than the spread + taker
   fee + a margin for your own uncertainty**.
4. **Event / data-release drift.** Right after a CPI/jobs/FOMC/election-night print, prices gap and can
   over- or under-react for minutes. An edge only if you are faster or better-calibrated than the
   crowd — otherwise you're the liquidity. Be honest about whether you actually have that edge.
5. **Stale new markets.** Just-listed markets can sit at round/placeholder prices before the crowd
   arrives — sometimes a free-ish view if you have a grounded estimate.

## How to evaluate a directional candidate (the bar)

For each candidate market, produce:
- **Market price** (executable: the ask if you'd buy YES, the bid if you'd sell) and the implied prob.
- **Your fair value**: a probability with a 1-line reasoning chain and **cited source(s)**. Use a
  research sub-agent for anything non-obvious; do not guess.
- **Edge** = |fair value − implied price|, then **net of spread + taker fee**. If that's not clearly
  positive with margin, it's not a trade.
- **Confidence** (high/med/low) — qualitative, based on how solid the sources are and how wide your
  estimate band is. Low confidence ⇒ either pass or size tiny.
- **Side, size, horizon, and the catalyst** (what makes the price converge to your view, and when).
- **A maker entry + exit plan.** Don't just "buy at the ask." Give a concrete **maker entry level** —
  the limit price to *rest* at (0 fee), typically a bit better than the current touch — and a rough
  **exit / take-profit level** (often your fair value, or a partial-take + re-eval band). A fair-value
  trade is: rest a maker order at entry X, exit as it converges toward Y. Note when crossing as a taker
  (and paying the fee) is worth it (speed/thin book) vs resting as a maker.
- **What would make you wrong** (the bear/bull case you're betting against).

## Show your pricing methodology (required)
The user needs to see *how* you got the fair value, not just the number. For every directional/
near-certain idea, state the derivation explicitly and in plain words — pick whatever fits:
- **Base rate:** "incumbents win this office ~80% of the time; adjust down for X → ~70%."
- **Poll/forecast average:** "three recent polls: 52/49/55 → ~52%, minus partisan-internal haircut → ~48%."
- **Resolution-rule logic:** "rule needs a *formal* handover by Jul 17; the contest result isn't due
  till September, so P(resolved YES by then) ≈ the chance of an uncontested same-week handover ≈ 30%."
- **Comparable odds:** "sportsbook has France +350 ≈ 22% raw, ~19% no-vig → market's 19¢ is fair."
- **Simple model:** "needs the 7-day MA to climb from 23 to 60 in 35 days; at the current recovery
  slope that's ~loosely even → ~50%."
Put this both in the idea bullets ("How I priced it") AND in the text under the chart. If you can't
articulate a methodology, you don't have a fair value — pass.

## Verify counterintuitive theses HARD
The dangerous trades are the ones that *sound* clever against a salient fact. If you're about to
recommend the opposite of what a headline implies (e.g. "buy NO on 'X out by date' right after X
announced resignation"), STOP and re-read the resolution rule word-for-word and confirm the real
timeline. Distinguish **"announced/decided"** from **"formally resolved by the cutoff"** — they're
often weeks apart, and that gap is either your edge or your blow-up. Get the mechanics exactly right
before you commit; a confident wrong call is worse than a pass.

## Honesty rules
- Distinguish a *view* from a *lock* explicitly. Directional = **[DIRECTIONAL]**; near-certain "bond"
  trades = **[NEAR-CERTAIN]** (near-free, not risk-free); only logic-locks are risk-free.
- Show **many** real ideas (8–15 across categories) — but every one needs a genuine mispricing
  (fair ≠ price). If fair value ≈ price, it's "checked, efficient," not an idea. Don't manufacture
  conviction to pad the list, and don't drop a real edge just because it's small.
