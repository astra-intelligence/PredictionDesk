# Cross-event link-finder agents (Step 3)

Spawn one subagent per non-empty `slice_<theme>.json`, in parallel. Each reads its slice and proposes
**logically-forced** relationships between markets in *different* events. They propose from the slice's
(stale) snapshot prices — `check_links.py` re-prices live afterward, so price accuracy doesn't matter
here; only the *logic* of the link does.

## Prompt template

Fill in `<THEME>`, `<SLICE_PATH>`, `<RUN>`:

```
You are finding CROSS-EVENT logical arbitrage/relative-value links in Polymarket prediction
markets (<THEME> theme).

INPUT: Read `<SLICE_PATH>`. It is a JSON list of OPEN events. Each event =
{title, negRisk, vol1mo, legs:[{o (outcome label), q (question), bb (best bid), ba (best ask),
cond (conditionId), end (resolution date), liq (liquidity $)}]}. Each leg is a YES/No market;
bb/ba are the YES token's best bid/ask (NO = 1 − YES).

YOUR JOB: Find relationships BETWEEN DIFFERENT EVENTS (or different framings of the same
underlying) that are LOGICALLY FORCED by the definitions of the questions — a price
identity/inequality that MUST hold at settlement regardless of the real-world outcome.

Types to look for:
- DUPLICATE: two markets in different events that resolve identically -> P(A)=P(B).
- COMPLEMENT: two markets mutually exclusive AND exhaustive -> P(A)+P(B)=1.
- IMPLICATION: A logically implies B -> P(A) <= P(B).
- SUBSET_SUM: sum of YES of several markets in event X equals the YES of one market in event Y.

RULES:
- ONLY include links LOGICALLY FORCED by definitions. Be conservative. If resolution wording,
  dates, or sources could differ in a way that breaks the identity, exclude it or mark
  confidence "medium" and explain the risk in the `risk` field.
- SKIP pure within-single-event basket sums (handled separately).
- Get direction/sign right (which leg is cheap to buy, which is rich to sell).
- Reference markets by their exact `cond` string. Quality over quantity (5-25 solid candidates
  is great; fewer is fine).

OUTPUT: Write a JSON list to `<RUN>/links_<THEME>.json`. Each item:
{
 "type": "DUPLICATE|COMPLEMENT|IMPLICATION|SUBSET_SUM",
 "title": "short human description",
 "rationale": "one-two sentence settlement argument: why the identity is logically forced",
 "confidence": "high|medium",
 "risk": "any resolution/date/source mismatch risk, or 'none'",
 "identity": "e.g. 'P(A)+P(B)=1' or 'P(A)<=P(B)' or 'P(A1)+P(A2)=P(B)'",
 "A": [{"cond":"...","label":"event title :: outcome label"}],
 "B": [{"cond":"...","label":"event title :: outcome label"}]
}
For DUPLICATE/COMPLEMENT/IMPLICATION put one market in A and one in B. For SUBSET_SUM put the
summed-subset markets in A (a list) and the single equal market in B.

After writing the file, reply with 3-5 lines: how many candidates by type, and the 2-3 most
likely to show a real executable edge. Do NOT fetch live prices — propose only from the file.
```

## Theme-specific hints to append

- **macro:** Fed/ECB/BoE cut-by-month & hike-by-month ladders (cumulative → IMPLICATION chains);
  "0 cuts in year" vs "cut by last meeting" (COMPLEMENT); per-meeting decision outcomes vs "cut by
  that meeting" (SUBSET_SUM); IPO-by-date ladders & their duplicates across IPO/market-cap events.
- **crypto:** same coin/date "price ≥ T bucket" vs standalone "above T" market (DUPLICATE); higher
  threshold ⟹ lower threshold, and all-time-high-by-date chains (IMPLICATION); sum of top buckets ≥ T
  = "above T" market (SUBSET_SUM). **Beware date wording**: "close on date" vs "intraday high" vs
  "anytime in month" are different — mark medium + explain.
- **politics:** "party controls chamber" vs seat-count buckets (SUBSET_SUM, mind the tiebreak split);
  Balance-of-Power joint cells vs standalone chamber control (SUBSET_SUM); win-general ⟹ win-nomination
  (IMPLICATION); date-nested "out by X" markets (IMPLICATION). Anonymized "Person A/B" generals usually
  can't be name-matched to primaries — don't force duplicates.
- **sports:** win-tournament ⟹ reach-final ⟹ reach-semifinal (IMPLICATION across stage events).
