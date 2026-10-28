# Classifying risk: risk-free arb vs relative value

An edge is only **risk-free** if the legs are guaranteed to resolve consistently with the logical
identity you traded on. After `resolution.py` dumps the rules, read them side by side and walk this
checklist. The goal is to name the *exact* scenario where the two legs diverge, then judge its
probability. If you can't find one, it's arb; if you can, it's RV (still tradable, but price the risk).

## Checklist

0. **Enumerate EVERY world — especially the "both legs lose" one.** Before trusting a lock, write the
   payoff of the assembled position in every combination of leg outcomes. The failure mode is assuming
   two legs are perfect complements when a gap between their windows/definitions creates a state where
   **both pay $0** (or both pay $1 against you). Worked example: "cut **by the Dec FOMC meeting**" + "no
   cuts in **calendar 2026**" look like complements, but an (emergency) cut in the Dec 10–31 gap —
   after the meeting, still in 2026 — makes the first NO *and* the second NO → buy-both pays $0. Low
   probability, but it means RV, not risk-free. If you can't rule out a both-lose world, it is not arb.

1. **Same definition?** Do both legs key off the *same measurable fact*, worded the same way?
   Watch for: "control / caucus" vs "party seats by ballot affiliation"; "close price" vs "intraday
   high" vs "anytime touch"; "by December meeting" vs "in 2026"; headline vs core; one index vs another.
   A different definition is the #1 source of basis risk.

2. **Same date / timing?** Compare `endDate`s and the resolution windows. Two legs that resolve at
   different times leave you partially unhedged for a stretch (not a loss, but capital/timing risk).
   For elections/sports, **runoffs and ambiguous calls** can push resolution weeks past the nominal
   date — and the two markets may handle that differently.

3. **Same resolution source?** Different sources (different data providers, "consensus of credible
   reporting" vs a specific URL) can disagree at the margin.

4. **Exhaustiveness / "Other" outcomes.** Does either leg have a catch-all ("Other", "No IPO",
   "decides differently") that can make a complement/duplicate fail? E.g. a control market that
   resolves "Other" while the seat-count market still pays.

5. **Edge cases in the mapping.** For SUBSET_SUM/COMPLEMENT, is the partition *exactly* right?
   - Tiebreaks (50-50 Senate → VP's party).
   - Independents / third parties counted differently by the two events.
   - Vacancies, party switches, mid-period changes.
   - Rounding/bucket boundaries ("≥3.3%" vs "3.3%").

6. **Settlement mechanics.** Can you actually realize it? Held-to-resolution (capital locked) vs
   instant convert (negRisk only, same event). Cross-event trades are always held to resolution.

## How to label the opportunity

- **RISK-FREE ARB** — you found no scenario where the legs diverge. Strongest case: YES+NO of the
  *same* market; or a basket that is the exact, same-source, same-date complement of a single market.
  Even here, residual UMA/oracle dispute risk exists (small, ever-present).

- **RELATIVE VALUE (RV)** — the relationship is tight but a concrete divergence scenario exists.
  State it explicitly and estimate its probability, e.g.: *"Lock holds unless an independent senator
  caucuses Republican while R-party seats ≤ 49 — historically independents caucus D, so low
  probability, but non-zero."* Part of the observed gap may be fair compensation for exactly this.

- **ARTIFACT** — the edge dies at real depth (Step 5), or the legs aren't actually the same thing
  (re-read the questions). Discard, and note why so a future scan doesn't re-flag it.

## Reporting the risk

Give the user the specific break scenario, not a vague "some risk." They can only decide to deploy if
they know *what* would make them lose and *how likely* it is. When in doubt, label it RV, not arb —
overstating safety is the worst failure mode here.
