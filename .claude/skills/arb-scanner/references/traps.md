# Traps — the mistakes that turn a "find" into nothing

Read this whenever an edge looks too good. Each of these has personally produced a fake winner.

## 1. Mid-price overstates edge by ~half the spread
A basket summing to 1.04 on **mid** looks like a 4% sell opportunity. But the mids sit halfway across
wide spreads; at the **bid** the same basket sums to ~0.99 — nothing to sell. Always recompute on
bestBid (to sell) / bestAsk (to buy). The "overround" in thin daily markets (weather, crypto-on-date)
is almost entirely spread, not edge. (It *can* be a market-making opportunity — earn the spread by
quoting — but that's a different strategy, not taker arb.)

## 2. Wide spread / tiny liquidity = stale or crossed quote
The flashiest edges (50–90%) are almost always one illiquid leg with a stale quote (e.g. bid 0.012 /
ask 0.22), or a book with $15 on it. The edge evaporates the instant you try to size. Gate on
liquidity and spread; treat anything with `cap$` in the tens of dollars as noise until depth proves
otherwise.

## 3. The headline edge is per $1 of payout — divide by the number of legs for return-on-capital
A 9-outcome basket "summing to 0.986" looks like 1.4%. But to capture it you buy ~8 NO shares (~$8 of
capital) to make ~1.4¢ → **~0.18% on capital**, not 1.4%. The more legs, the more capital, the more
the headline edge dilutes. **Always report return-on-capital and annualize over time-to-resolution.**
Many "arbs" land below the T-bill rate once you do this.

## 4. Profit is realized at resolution, not now
Buying a complete YES basket for <$1 only pays out when the event resolves (weeks to months). The
only *instant* realization is the negRisk **convert** (sell-all-YES / buy-all-NO → burn → USDC), and
that edge is bot-compressed to near zero with tiny capacity. So "8% edge" on something resolving in 5
months is ~7% annualized at best — fine, but not "free money today."

## 5. Incomplete baskets fake a buy edge
A basket only sums to 1 if **every** outcome has a live book. If an illiquid tail outcome is dropped,
the remaining legs sum to <1 and look like a buy arb — but the missing outcome carries the gap.
Rule: **sell-side (sum>1) is robust to missing legs; buy-side (sum<1) is suspect unless the basket is
verified complete** (all Gamma outcomes present and quoted).

## 5b. NON-EXHAUSTIVE fields fake the *biggest* buy edges (the most important trap)
"Complete" (every listed leg is quoted) is NOT "exhaustive" (the listed outcomes cover every way the
event can resolve). The deadliest fake: a named field with an **unpriced winner**.
- **Nobel/elections/awards:** 20 listed names sum to $0.78 not because of free money but because ~22%
  is "someone not listed" / an explicit **"Other"** resolution. Buy all 20 → $0 if an unlisted laureate
  wins. *The bigger the buy-side edge on a named field, the more certain it's non-exhaustive.*
- **"Dem vs Rep" races (the subtle one):** a 2-leg party basket looks binary, but a **viable
  independent/third-party** candidate (or a write-in) breaks exhaustiveness — buy-both can pay $0, or
  the rules may resolve "Other". Don't assume Dem+Rep = 1; **read the rule** (some force any winner into
  D/R, which *does* make it exhaustive — but verify, don't assume).
Rule: numeric/range partitions (temperature buckets, "N cuts", price buckets) are exhaustive by
construction; **named fields are non-exhaustive until the resolution rule proves otherwise.** A real
buy-all lock needs an exhaustive basket. `scan_markets.py` emits an `exhaustiveness_hint` to triage,
but confirm by reading the rule (Step 6) before calling it a lock.

## 6. Edges are transient — a real find today can be gone tomorrow
A 3% gap can close within a day (bots, or the underlying repricing). Don't assume a past edge is still
there; that's why the scan is meant to be re-run. Conversely, a *persistent* gap (present across many
re-runs) is more likely real structural inefficiency than a momentary fluke.

## 7. "Identical" legs that aren't
When an agent proposes a DUPLICATE, check the two markets really resolve on the same thing. Classic
failure: matching a price bucket ">$1.60" to a threshold market ">$1.00" — different numbers, not a
duplicate. A huge edge on a "duplicate" is more often a mislabel than a mispricing.

## 8. Multi-leg execution risk
A cross-event trade needs all legs filled near the quoted prices. If you get one leg and the others
move, you're no longer hedged. Use marketable limit orders / accept partial fills, and size to the
**thinnest** leg's depth (the binding constraint), not the deepest.
