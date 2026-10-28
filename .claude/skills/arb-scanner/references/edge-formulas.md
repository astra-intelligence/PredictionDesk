# Edge formulas & depth-walk trade specs

All prices are YES prices. NO = 1 − YES. Buy = pay the **ask**; sell = earn the **bid**. Never use mids.

## Relationship types and their executable edge

For two markets A, B (or a subset {A_i} and a single B):

| Type | Logical identity | Executable edge | The trade when edge > 0 |
|---|---|---|---|
| **DUPLICATE** | P(A) = P(B) | max( bid_B − ask_A , bid_A − ask_B ) | buy the cheap one, sell the rich one |
| **COMPLEMENT** | P(A) + P(B) = 1 | max( 1 − (ask_A+ask_B) , (bid_A+bid_B) − 1 ) | buy both (<1) or sell both (>1) |
| **IMPLICATION** | A ⟹ B, so P(A) ≤ P(B) | bid_A − ask_B | sell the rich A, buy the cheap B (B≥A at settle protects you) |
| **SUBSET_SUM** | Σ P(A_i) = P(B) | max( bid_B − Σ ask_A_i , Σ bid_A_i − ask_B ) | buy basket / sell B, or sell basket / buy B |
| **BASKET (intra-event)** | Σ P(outcome_i) = 1 | buy: 1 − Σ ask_i ; sell: Σ bid_i − 1 | buy all (<$1) or sell all (>$1) |

**IMPLICATION sign intuition:** if A implies B, then at settlement B pays whenever A does (and
sometimes more), so a long-B / short-A position can never lose at resolution. You profit if you can
put it on for a credit, i.e. sell A above where you buy B: `bid_A − ask_B > 0`.

**Selling a basket / shorting YES (the negRisk convert path):** selling all YES of a negRisk event is
done by **buying all NO** and calling `convertPositions`, which burns the complete NO set for (N−1)
USDC instantly. Realizable edge = Σ(YES bid) − 1. It is *instant* (not held to resolution), but
capacity is capped by the thinnest expensive leg and bots keep it near zero.

## Building a depth_walk trade spec

`depth_walk.py` reads a JSON spec and walks the full ladders. The only subtle field is
`settle_per_unit`: **the guaranteed dollars one fully-assembled unit pays at resolution.** For a
correct hedge this is the *same number in every outcome* — that constancy is what makes it a lock.
Compute it by writing out what the bundle pays in each possible world; if it isn't constant, the
trade isn't hedged and you've mis-specified it.

```json
{
  "name": "Senate: buy binary-Dem, sell seat-count R<=49 basket",
  "settle_per_unit": 3,
  "legs": [
    {"cond": "0x...", "side": "buy",  "label": "binary: Democrats control Senate"},
    {"cond": "0x...", "side": "sell", "label": "seat-count: R <= 47"},
    {"cond": "0x...", "side": "sell", "label": "seat-count: R = 48"},
    {"cond": "0x...", "side": "sell", "label": "seat-count: R = 49"}
  ]
}
```

`settle_per_unit` worked examples:
- **BASKET buy** (buy all N outcomes of one event): exactly one pays $1 → `settle_per_unit = 1`.
- **DUPLICATE** buy-A / sell-B: A and B are the same event. If true: A pays 1, B's NO pays 0 → 1. If
  false: A 0, B-NO 1 → 1. `settle_per_unit = 1`.
- **COMPLEMENT** buy-both: exactly one of A,B is true → `settle_per_unit = 1`.
- **IMPLICATION** sell-A / buy-B: outcomes — (A&B): A-NO 0, B 1 →1; (¬A&B): A-NO 1, B 1 →2; (¬A&¬B):
  A-NO 1, B 0 →1. **Not constant (1 or 2)** → not a pure cash lock; the position is still safe (never
  <0 beyond the credit) but model it as a credit trade: set `settle_per_unit` to the *minimum* (1) and
  read the curve as a floor, or just evaluate it as "credit kept = bid_A − ask_B" at small size.
- **SUBSET_SUM** sell-basket / buy-B (e.g. Senate): the k subset legs (shorted via NO) plus long B.
  Exactly one subset leg is true iff B is true, so the NOs pay (k−1 or k) and B pays (1 or 0), summing
  to a constant `k + ... ` — write it out; for the Senate 3-bucket case it's **3**.

When `settle_per_unit` is genuinely constant, `profit(q) = settle·q − total_cost(q)` is your locked
profit and `capital = total_cost(q)`. When it isn't (pure IMPLICATION), report the small-size credit
and note it's a one-sided safe bet, not a fixed payout.
