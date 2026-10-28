# Independent fact-check gate — the critique agent

No idea reaches the dashboard until an **independent** agent verifies every factual claim in it. This
exists because confident-but-wrong ideas (stale prices, misread resolution rules, fabricated sources,
hand-wavy fair values) are the worst failure mode — they look like money and aren't. The gate is
**mandatory**: only ideas that PASS are shown.

## Stance: indifferent and adversarial
The critic has **no stake in approving**. It is given ONLY the idea's factual claims — not the main
agent's narrative or enthusiasm — and is told to **try to refute each one**. Default to **FAIL** on
anything it cannot independently confirm. A plausible-sounding claim with no verifiable source is a FAIL,
not a pass. It is fine — good, even — for the gate to reject most ideas; a short verified list beats a
long unverified one. The critic must not "fix" or re-argue the idea; it only verifies and votes.

## What it checks (per idea) — all must hold to PASS
1. **Live market price.** Re-pull the market's order book (`clob.polymarket.com/book?token_id=…`, via the
   conditionId → token) and confirm the price the idea quotes is actually live (within a small tolerance).
   Stale or wrong price → FAIL. Catches the #1 artifact.
2. **Resolution rule.** Re-fetch the market's resolution criteria (`gamma-api.polymarket.com/markets?
   condition_ids=…` → `description`) and confirm the idea's interpretation is *exactly* what the rule says
   — especially "decided" vs "formally resolved by the cutoff," any "Other" outcome, the exact threshold/
   window/source. Misread rule → FAIL. Catches the Starmer-type trap.
3. **Fair-value inputs + math.** Independently verify the numbers the methodology relies on (e.g. re-fetch
   the live spot price and a realized-vol estimate; look up the poll/base rate) and **recompute** the
   fair value. If the inputs are wrong or the math doesn't reproduce the claimed fair value (within a
   reasonable band), → FAIL. "Research says ~X%" with no reproducible derivation → FAIL.
4. **Sources.** Open each cited URL (web). Confirm it exists AND actually supports the specific claim.
   A hallucinated link, a dead link, or a real link that doesn't say what's claimed → FAIL.
5. **Key real-world facts.** Verify the load-bearing facts via web (e.g. "only a temporary ceasefire
   exists," "SOL ≈ $63," "the Fed held at the last meeting"). Any false/unverifiable load-bearing fact → FAIL.

## How it runs — a SEPARATE sub-agent, always
- The critique **must** be done by a **distinct sub-agent**, spawned via the Agent tool — never by the
  idea-generating agent reviewing its own work. One critic **per idea**, in parallel, each with **web
  search + the public Polymarket APIs**. Give it ONLY the idea's claims (price, rule interpretation,
  fair-value derivation + inputs, sources, facts) and this checklist. It does its OWN fetching — it must
  not trust the idea's stated numbers.
- **There is no self-critique fallback.** A self-review is not a fact-check and never earns the ✓. If the
  environment truly cannot spawn a separate sub-agent, the skill must instead mark every idea
  `UNVERIFIED`, drop the green stamp, banner the dashboard with "fact-check did not run independently —
  do not trust," and tell the user — not quietly self-review and stamp it verified.

## Output: `critique.json`
```json
[{ "id": "<idea id/title>", "status": "PASS|FAIL",
   "checks": [
     {"name":"live price",      "verdict":"OK|FAIL", "evidence":"NO ask 0.96 on CLOB at 02:00Z"},
     {"name":"resolution rule", "verdict":"OK|FAIL", "evidence":"rule requires a *permanent* agreement"},
     {"name":"fair-value math", "verdict":"OK|FAIL", "evidence":"recomputed lognormal P(<60)=7.9% ≈ claim"},
     {"name":"sources",         "verdict":"OK|FAIL", "evidence":"Reuters link live and supports ceasefire-only"},
     {"name":"facts",           "verdict":"OK|FAIL", "evidence":"only temp ceasefire confirmed"} ],
   "reason": "one line — for a FAIL, the single decisive problem" }]
```
The main agent then keeps only `PASS` ideas in `ideas.json` (copying each idea's `checks` into its
`verification` field for the "Fact-checked ✓" stamp), and lists every `FAIL` in `rejected[]` with its
`reason`. **Never** show a FAILed or unchecked idea as a recommendation.
