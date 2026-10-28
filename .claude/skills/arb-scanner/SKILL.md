---
name: arb-scanner
description: >-
  Scan Polymarket prediction markets end-to-end for TRADE OPPORTUNITIES of every kind — risk-free
  arbitrage, relative value (RV), AND directional edges (a researched view that the market price is
  wrong). Pulls every open market via the public Gamma API; screens mutually-exclusive (negRisk)
  baskets and cross-event logical links (complements / implications / duplicates / subset-sums) for
  arb/RV; screens for directional setups (favorite-longshot bias, small inefficient markets,
  research-grounded fair-value gaps, near-resolution flow); walks the real order-book depth to size
  each trade NET of taker fees; reads the markets' resolution rules; finds near-certain "bond" trades
  (buy $1 for ~96¢, resolves in days); and proposes repeatable backtestable signal ideas. Every idea is
  then verified by an INDEPENDENT fact-check gate (adversarial critic with web + live Polymarket data —
  only ideas that pass are shown). The deliverable is a single self-contained interactive HTML dashboard:
  ideas ranked by conviction, each expanding into a deep-dive with the fair-value methodology explained
  as a worked example, charts with plain explainers, and a fact-checked stamp. Use this WHENEVER the user
  wants to find trade ideas, arbitrage, mispricings, RV, directional bets, near-certain "bonds", signal
  ideas to backtest, "what should I trade", "where's the edge", "free money", risk-free locks, or
  tradable opportunities on Polymarket / prediction markets; run a daily or repeated market scan; or
  check whether a specific market or pair is worth trading — even if they never say "arbitrage".
  Free public APIs only.
---

# Polymarket trade-opportunity scanner (arbitrage · relative value · directional)

This skill runs a repeatable pipeline that finds tradable opportunities on Polymarket, sizes them
against real depth **net of taker fees**, and tells the user honestly what each one is: a risk-free
lock, relative value with basis risk, or a directional bet with a thesis (and how confident).

**Verify the rules before you trust them.** Polymarket's fees, negRisk/convert mechanics, and API
have changed more than once. At the start of a run, quickly re-confirm the current **fee schedule**
and any resolution mechanic you rely on via the official docs (`docs.polymarket.com`) / web search —
do not assume the numbers in this file are still current. (As of last check: taker fee =
`shares × rate × p × (1-p)`, rate by category — crypto 0.07, sports 0.03, finance/politics/tech 0.04,
econ/culture/weather/other 0.05, **geopolitics 0**; **makers pay 0**. Net every edge against this.)

## The ways to make money here (hunt ALL of them every run)

1. **Mispricing vs logic (arb / RV)** — a set of mutually exclusive, exhaustive outcomes must have
   YES prices summing to **$1**; when a *tradable* sum (bid/ask, not mid) deviates, there's an edge.
   Within one event (negRisk basket) it's bot-arbed thin; **across events** that price the same
   underlying it's far less arbed — that's where the locks live. Don't declare arb "dead" because the
   thin baskets net negative after taker fees — **look harder**: bigger cross-event gaps, and capture
   thin ones as a **maker** (limit orders, 0 fee) instead of a taker.
2. **Near-certain "bond" trades (the highest-value realistic edge)** — a market whose outcome is
   *already essentially decided* by its resolution rule + current facts, that **resolves very soon**,
   but trades a few cents below certainty because holders **sell early to free up capital**. Buy $1
   for ~96¢, collect $1 in days → high annualized, low (not zero) risk. The edge is real ONLY if you
   read the resolution rule + recent news and conclude reality is *materially more certain than the
   price* (a 0.97 that is truly 97% is NOT an edge). This is the closest thing to free money that
   actually exists post-fees. See Step 8.
3. **Mispricing vs reality (directional)** — the market's probability is simply wrong vs a
   well-reasoned estimate: **favorite-longshot bias**, **sub-$100k inefficient markets**, and
   **research-grounded fair value**. Directional = RISK, not a lock; size it as a bet, ground every
   number in a source, and **show your pricing methodology**. See `references/directional-plays.md`.
   The one ROBUST documented bias (research-notes.md §3): **long-horizon POLITICAL favorites are
   underconfident** (Polymarket calibration slope ~1.31; slopes up to 1.83 at ≥1wk) — buying 60–90¢
   political favorites is the evidence-backed directional angle; liquid short-horizon sports are
   well-calibrated (fading them is dead). Net any bias against carry + fee + spread.
4. **Maker income — the liquidity-rewards program (daily realized PnL).** Polymarket pays makers
   daily (midnight UTC) from per-market reward pools with machine-readable economics (quadratic
   tightness scoring, ~3× for two-sided quotes, ZERO for one-sided when mid is outside [0.10,0.90]).
   Run `mm_rewards_screen.py` (Step 2b) to rank markets by reward-vs-competition; surface the best as
   **[MAKER-YIELD]** ideas (income from quoting, not a view). The cost is adverse selection — prefer
   slow, low-vol markets; params are live API data, never hardcoded. See research-notes.md §2.
5. **Instant-realization structures.** Two documented paths realize PnL immediately instead of at
   resolution: (a) **negRisk convert** — if `sum(NO asks) < m−1` in one basket, buying the NOs and
   calling `convertPositions` releases the collateral difference NOW (+ free YES tails; check
   `feeBips` + guaranteed-winner; research-notes.md §1 — this rebalancing family made ~$29M of the
   documented ~$40M/yr); (b) **post-outcome capture** — the outcome is already KNOWN (game ended,
   number printed) but the market trades <$1 until the UMA proposal/2h-dispute window completes; buying
   near-$1 right after the fact is a bond with hours-not-days duration (verify the rule/source; this
   angle is plausible-but-unverified, treat skeptically).

**Quantity matters:** surface *many* money-making ideas (aim for 8–15 across categories), ranked.
Only show ideas with a **real mispricing** (fair value ≠ price) in the main list — if your fair value
equals the market, that's not an idea, it's a one-line "checked, efficient" note.

## Non-negotiable principles (why naive scans fail)

1. **Use executable prices, never mids.** A basket summing to 1.04 on *mid* is usually just the
   spread — at the *bid* it sums to ~0.99 and there's nothing to sell. Buying costs the **ask**,
   selling earns the **bid**. Every edge in this skill is computed from bestBid/bestAsk or the full
   ladder, never the midpoint.
2. **Big edge + tiny liquidity = artifact.** A 90% "edge" on a $15 book is a stale or crossed quote,
   not money. Real finds are *modest* edges with *real* capacity.
3. **The headline edge is per $1 of payout; the return is per $ of capital.** A multi-leg trade ties
   up roughly $(number of legs) of capital to earn the edge, so divide. An "8%" 9-leg basket can be
   <1% on capital. Always report **return-on-capital**, not just the edge. See `references/traps.md`.
4. **Profit is realized at resolution, not instantly** (except the negRisk convert path). Annualize
   honestly over the time to resolution; many "arbs" annualize below T-bills.
5. **An edge is only risk-free if both legs resolve identically.** Cross-event links almost always
   carry some definitional/timing basis risk — that's what separates arb from RV. Always read the
   rules (Step 6).
6. **A buy-all-basket is only an arb if the basket is EXHAUSTIVE.** "All legs have a live quote"
   (complete) is NOT the same as "the listed outcomes cover every way the event can resolve"
   (exhaustive). If there's an unpriced winner — an "Other"/field outcome (Nobel laureate, election
   with a write-in), **or a viable independent/third party in a "Dem vs Rep" race** — then buying all
   listed YES can pay $0, and a sum < $1 is *correct pricing, not free money*. The bigger the buy-side
   edge, the more likely the basket is non-exhaustive. Numeric/range partitions (temperature buckets,
   "N cuts", price buckets) are inherently exhaustive; **named fields (people, parties, teams) are
   guilty until the resolution rule proves otherwise.** This is the gate that kills most fake intra
   edges — apply it before calling anything a lock. See `references/traps.md`.

## Pipeline

Make a fresh dated run directory so repeated daily scans don't clobber each other. This feature is
SEPARATE from the trading desk — never write runs under `desk/`; use the top-level gitignored dir:
```
RUN=arb-scanner-runs/$(date +%Y-%m-%d_%H%M); mkdir -p "$RUN"
```
All scripts live in this skill's `scripts/` and take `--out "$RUN"`. They depend only on
`requests` (public Gamma + CLOB APIs; no keys).

### Step 1 — Scan all markets + screen intra-event baskets
```
python <skill>/scripts/scan_markets.py --out "$RUN"
```
Writes `catalog.json` (every open event + markets, for Step 2) and `intra_candidates.json` (ranked
negRisk baskets whose executable touch already shows an edge). Skim the printed table; note that the
biggest edges with tiny `minLiq` / wide `maxSpread` are probably artifacts to confirm later.

### Step 2 — Partition the catalog for the cross-event search
```
python <skill>/scripts/partition_catalog.py --out "$RUN"
```
Writes `slice_macro.json`, `slice_crypto.json`, `slice_politics.json`, `slice_sports.json`.

### Step 2b — Maker-yield screen (liquidity rewards)
```
python <skill>/scripts/mm_rewards_screen.py --out "$RUN"
```
Ranks reward-carrying markets by daily-reward-per-$100-of-resting-liquidity (crowding proxy), filtered
to mid ∈ [0.10,0.90] (two-sided scoring safe) and DTE ≥2d (pick-off risk). Feed the top rows to a
MAKER-YIELD lane: it must judge **adverse selection** per market (slow/low-vol good; news-driven bad)
and state est. $/day honestly as pro-rata-vs-competitors, not guaranteed. See research-notes.md §2.

### Step 3 — Fan out link-finder agents (the high-value step)
Spawn parallel subagents — one per non-empty slice — to propose cross-event logical links. Use the
prompt template in `references/agent-prompts.md` (it specifies the exact output schema: each link is
a typed relationship referencing markets by `cond`, written to `<RUN>/links_<theme>.json`). Tell each
agent its slice path and theme. Agents propose from the (stale) snapshot prices — that's fine, Step 4
re-prices live. Emphasize **logically-forced** links only and conservative confidence.

If subagents aren't available, do the link-finding inline: read each slice and apply the same
logic from `references/agent-prompts.md`, writing the same `links_<theme>.json` files.

### Step 4 — Verify links against live books
```
python <skill>/scripts/check_links.py --out "$RUN"
```
Re-fetches live order books for every leg, computes the executable edge per relationship type, and
ranks into `link_candidates.json`. The edge formulas are in `references/edge-formulas.md`. Again:
huge edge + tiny `cap$` = artifact or an agent mislabel (the two "identical" legs aren't actually
identical) — flag for scrutiny, don't celebrate.

### Step 4b — Structural arb screen (buy-all · ranked-pair · convert-pair)
```
python <skill>/scripts/structural_arb_screen.py --out "$RUN"            # snapshot
python <skill>/scripts/structural_arb_screen.py --out "$RUN" --watch 30 # monitor mode
```
Screens three verified structural families over ≤7d events: (1) **BUY-ALL** negRisk baskets summing
<$1 (esp. determined-but-unpublished: weekly charts/rankings whose window already ended — placeholder
legs are dead by construction); (2) **RANKED-PAIR complements** (#1 + #2 events over the same ranking:
any near-certain-top-2 candidate's YES(#1)+YES(#2) < $1 pays $1 either way); (3) **CONVERT pairs**
(two dominant legs' YES bids sum >$1 → buy both NOs, convertPositions pays $1 instantly). These edges
are TRANSIENT — fattest during one-leg panics when the logically-linked twin lags (measured: 2.5¢→0 in
~6 min) — so run --watch during volatile sessions. Depth-walk + fee-check every hit; traps: zombie
baskets with a closed winner leg (filtered), placeholder legs, non-exhaustive sets.
For each genuinely promising candidate (from `intra_candidates.json` or `link_candidates.json`),
build a trade spec and walk the full ladder:
```
python <skill>/scripts/depth_walk.py --trade <RUN>/trade_<name>.json
```
The spec format and how to set `settle_per_unit` for each structure are in
`references/edge-formulas.md`. This produces the capacity curve: profit, capital, and
return-on-capital at increasing sizes, and the size where the edge closes. This is the honest answer
to "how much can I actually make."

Triage discipline: do **not** depth-walk only the top few by headline edge — the biggest headline
edges are usually artifacts/non-exhaustive, and the real money often sits in a *cluster of modest
edges* (e.g. dozens of House-race baskets). Depth-walk every candidate that survives the
exhaustiveness gate, and **group near-identical small locks** (same structure, same resolution date)
into one line with an aggregate capacity and profit.

### Step 5b — Make charts (visual, and self-explanatory)
Markdown tables don't render for the user, but **a naked chart is useless too** — every chart must
carry a plain-English **takeaway** and label the decision-relevant numbers directly. Two chart types:

For **arb/RV** — the capacity curve (profit & ROC vs capital, peak marked):
```
python <skill>/scripts/depth_walk.py --trade <RUN>/trade_<name>.json --json <RUN>/depthwalk_<name>.json
python <skill>/scripts/make_charts.py --depthwalk <RUN>/depthwalk_<name>.json --out <RUN>/chart_<name>.svg \
  --takeaway "Best size ~$4k locks $34 net; edge gone past $8k."
```

For **directional** — a capacity curve is the WRONG visual (it's a bet, not a sized lock). Use the
**edge/payoff chart**: write an edge spec and render market-price-vs-fair-value + win/lose dollars:
```
# edge_<name>.json: {"name","market_pct","fair_low","fair_high","win_usd","lose_usd","takeaway"}
#   For a FADE (buy NO), set "win_label":"if NO (win)" and "lose_label":"if YES (lose)" so the payoff
#   bars aren't mislabeled (market_pct is still the YES price; fair is your YES fair value).
python <skill>/scripts/make_charts.py --edge <RUN>/edge_<name>.json --out <RUN>/chart_<name>.svg
```
Embed with `![](chart_<name>.svg)` AND, right under it, a one-line **"What this shows: …"** in words.
The chart supports the prose; it never carries the point alone.

### Step 6 — Read the resolution rules → classify risk
```
python <skill>/scripts/resolution.py --conds 0xLEG1,0xLEG2,...
```
Read the full rules of every leg side by side and apply `references/resolution-risk.md` to classify:
- **RISK-FREE ARB** — legs provably resolve identically (e.g. YES+NO of the same market; a basket that
  is the exact complement of a single market with matching wording/source/date).
- **RELATIVE VALUE (RV)** — the relationship is economically tight but the legs *can* diverge
  (different resolution definitions, dates/runoffs, independents/caucus, an "Other" outcome). Name the
  exact break scenario and judge its probability.
- **ARTIFACT** — the edge vanishes at real depth or the legs aren't actually the same thing.

### Step 7 — Directional opportunities (price vs reality)
Arb/RV (Steps 1–6) is usually thin and bot-arbed. The other half of the job is **directional**: markets
whose probability is simply *wrong*. Surface candidates, then research them:
```
python <skill>/scripts/directional_screen.py --out "$RUN" --max-days 45 --min-liq 5000
```
This lists liquid, soon-resolving, not-yet-decided markets, **diversified across themes**
(politics · econ · crypto · geopolitics · finance · sports · culture) and capped to one leg per event,
each tagged with an angle (favorite-longshot extreme · small inefficient market · near-term catalyst).

**Go broad.** Research a *diverse slate* — aim for the single best idea from **several different
themes** (target ~6–10 researched candidates), NOT three deadlines of the same event. A report with
one politics, one crypto, one geopolitics, one econ, one sports idea is far more useful than five
variations of one. **Fan out research sub-agents** (one per candidate) when available; otherwise
research inline. For each, follow `references/directional-plays.md`: lead with qualitative reasoning,
ground every probability in a **cited source** (poll, base rate, forecast, official data, news), and
only call it a trade when |fair value − price| clearly beats **spread + taker fee + your own
uncertainty**. Label these **[DIRECTIONAL]** — bets with a thesis, never locks. Show your *best* ideas
even if the absolute edge is modest, but if the sources are weak or the edge is inside costs, say "no
real edge" for that one rather than manufacture conviction.

### Step 8 — Near-certain "bond" trades (do this every run — it's the best realistic edge)
```
python <skill>/scripts/bond_screen.py --out "$RUN" --max-days 14 --min-liq 3000
```
This surfaces markets resolving **within days** that trade at a **discount to certainty** (a YES at
0.90–0.985, or the NO side equivalently) — candidates for "buy $1 for ~96¢, collect $1 in N days." It
now **excludes pure-chance markets** (sports exact-scores, over/unders, player props, esports) whose
high price is just their true probability, not an exit discount — but plenty of price-shaped noise
remains. **Dig for SEVERAL genuinely-near-certain bonds (aim 3–6), don't stop at one.** Prioritise
event / geopolitics / macro / crypto-threshold markets where the real-world outcome is essentially
*known*. The screen only finds the *shape*; the edge is real ONLY when you do the work:
1. **Read the resolution rule** (`resolution.py`) — what *exactly* must happen, by when, per which source.
2. **Check recent news** — is the deciding event already locked in (a game already 5-0 at halftime, a
   bill already signed, a number already published, a candidate already conceded)?
3. **Decide certainty**: is reality *materially more certain than the price*? A 0.97 that is genuinely
   ~99.7% (held down by exit sellers) is a trade; a 0.97 that is truly 97% is not.
Report each confirmed one as **[NEAR-CERTAIN]** with: buy price, the rule + the news that makes it
near-decided, **annualized return** (small absolute %, huge annualized because it's days), capacity,
and the **residual risk** (resolution dispute / late surprise / you misread the rule). These are
*near*-free money, not risk-free — say so. Net of taker fee (small at p≈0.97); rest as maker for 0.

### Step 9 — Signal ideas (for the backtester, not just one-off trades)
Beyond one-off trades, propose repeatable **signals** — rules with a persistent edge to backtest before
any money. For each, emit a structured `backtest_spec` (no code) per `references/signals.md`, whose fields
map onto this project's `desk/signals/<name>/` contract (`generate_orders(store, as_of_ts, params,
positions) → Order[]`; ref impl `strategies/sample_weather/`). Good signals: favorite-longshot fade of cheap
longshots resolving soon, near-resolution "bond" exit-pressure capture, model-vs-market on a forecastable
quantity. State the hypothesis, market-selection filter, entry/sizing rule, features, mark_mode, why it
persists, and failure modes. These flow into the user's backtesting framework.

### Step 10 — Independent fact-check gate (MANDATORY — a SEPARATE sub-agent, gates the report)
Before building anything, every draft idea must pass an **independent** fact-check performed by a
**separate sub-agent — NOT the agent that generated the ideas, and NOT a self-review.** This is a hard
requirement: the agent running this skill **must spawn a distinct critique sub-agent per idea** (in
parallel via the Agent tool), each given **only that idea's factual claims** + `references/critique.md`,
with **web search + the public Polymarket APIs**. The critic is indifferent and adversarial — it
re-fetches the live price, re-reads the resolution rule, recomputes the fair value from independently
verified inputs, opens every cited source, and verifies the load-bearing facts; it **defaults to FAIL**
on anything it can't confirm. Collect verdicts into `<RUN>/critique.json`. **Keep only PASS ideas;** list
every FAIL (with reason) under "Rejected by fact-check."

**No self-critique escape hatch.** If — and only if — the environment genuinely cannot spawn a separate
sub-agent, you must **NOT** present the dashboard as fact-checked: do not show the green "Fact-checked ✓"
stamp on any idea, set every `verification.status` to `UNVERIFIED`, put a prominent banner in the
summary ("⚠ fact-check gate could not run independently — do NOT trust these numbers"), and tell the
user the gate didn't run. A self-review never counts as the fact-check and never earns the ✓.

### Step 11 — Forecasting + debate panel (the deliberation layer — runs ONLY on fact-check survivors)
The fact-check proves an idea is *true*; this proves the market is *mispricing the odds* and by how much.
For each PASS idea, spawn a **SEPARATE sub-agent** (again NOT the author) that simulates a quantitative
panel per `references/deliberation.md` + `references/panel-prompts.md`:
**EVIDENCE** (news/retrieval + verification loop) → **FORECAST** (3–5 *diverse* lenses, each emits an
independent fair probability `p_fair_i`) → **DEBATE** (Bull "price too low" vs Bear "price too high";
forecasters update, iterate to stability ≤3 rounds; resolution-risk + execution specialists feed in) →
**AGGREGATE** (`p_fair_agg` = median, `dispersion` = half-band) → **JUDGE** (mispricing = `p_fair_agg −
p_market` net of fee + spread + dispersion haircut → `TRADE|WATCH|REJECT` + **conviction 0–100** +
rationale). The **Judge owns conviction** (it replaces any generator number); it rises with bigger net
edge AND tighter forecaster agreement AND better evidence AND real capacity AND low resolution risk.
Each agent writes the augmented idea (original fields + `verification` + `deliberation`) to
`<RUN>/del_<id>.json`; only `TRADE`/`WATCH` reach the dashboard, `REJECT` → "Rejected" with the reason.
This is the multi-agent negotiation the dashboard renders (mispricing gauge + agent×layer×verdict ledger).

## Output: the interactive dashboard
The deliverable is ONE self-contained `dashboard.html` the user double-clicks: best ideas on top (ranked
by a 0–100 conviction you assign), grouped into sections, each expanding into a deep-dive. Assemble the
PASS-only ideas into `<RUN>/ideas.json`, render charts (Step 5b) into the same dir, then:
```
python <skill>/scripts/build_dashboard.py --ideas <RUN>/ideas.json --out <RUN>/dashboard.html
```
Then print a SHORT markdown summary to chat (headline + the top 2–3 ideas, one line each + "open
dashboard.html for the full deep-dives"). **Always actually point the user to the dashboard file.**
(Sibling surface: the desk dashboard's live **opportunities** page — `desk/dashboard_server.py` `/api/opportunities` — is the always-on rescan feed; this skill's `dashboard.html` is the deeper, fact-checked per-run write-up.)

**`ideas.json` per-idea fields** (the dashboard renders these; exact schema in build_dashboard.py docstring):
- `section` (`BOND|ARBITRAGE|DIRECTIONAL|SIGNAL`), `title`, `one_liner`, **`conviction` (0–100)**,
  `badge`, `headline_metric` + `headline_metric_label`.
- `thesis` (plain English) ; **`fair_price_explainer`** — THE headline block: how you got the fair price,
  explained simply, **with the actual numbers as a worked example** a layperson can follow (e.g. the
  "SOL is $63 … ~8% it closes <$60 … so worth ~92¢ vs 88¢ → 4¢ edge" form). If a normal person can't
  follow it, the idea fails the bar.
- `mispricing` (why it's mispriced / cheap), `market_price`, `fair_value`, `edge_net`, `execution`,
  `risk`, `sources[]`.
- **`markets` (MANDATORY)** — `[{label, url, note}]`, one per market/leg involved: the VERIFIED Polymarket
  link (`https://polymarket.com/event/<eventSlug>/<marketSlug>` via Gamma slug lookup — never fabricate;
  confirm the returned question matches) + `note` = the action at that leg ("buy NO @0.96"). The user
  must be able to click straight to every market they'd trade.
- **`how_it_works` (MANDATORY)** — `{context, steps[]}` in PLAIN language for a reader with ZERO context
  on the market: `context` = 1-2 sentences on what the market is about and why there's money here (define
  jargon inline); `steps` = 3-7 short imperatives with concrete numbers ("buy NO at $0.96 → pays $1.00 if
  X, = +4¢/share"). **SELF-EXPLANATORY BAR: the reader must be able to execute without asking a single
  follow-up question.** The steps MUST answer, in order encountered:
  ① **Order type per leg** — market or LIMIT; if limit/maker-only, say WHY (fee kills crossing / price only
    exists as a resting bid) and give the exact resting price. Never assume the reader knows bid vs ask —
    when relevant, say "you BUY at the higher (ask) price; the lower number is what sellers receive".
  ② **Minimum ticket + max size** — Polymarket enforces ~$1 min notional per order (+ share minimums);
    state the min ticket per leg AND the honest max (depth), e.g. "min $1 ≈ 3 shares; book holds ~$230".
  ③ **Multi-leg = NOT atomic** — legs fill independently; state what to do if only one side fills (the
    bail-out rule + its cost, e.g. "cancel the other order, sell back near touch, ~1¢ loss").
  ④ **Settlement** — the exact event/time that decides it (e.g. "Netflix prints Tue 3PM ET"), when cash is
    redeemable (resolution usually within hours via UMA; note any lockup/slip precedent), and any
    non-UI mechanics (convertPositions is a CONTRACT CALL, not a button — give the no-contract fallback).
  ⑤ **Loss scenario** — what exactly makes it pay $0, with its rough probability; plus the exit/hedge rule
    (incl. "hedging locks value but does not recover sunk cost" when applicable).
  This is the FIRST block the dashboard shows — if a no-context reader can't execute from it alone, the
  idea fails the bar.
- **MANDATORY return & timing on every actionable idea (arb/bond/directional).** Keep the OVERVIEW
  clean — only short values go on the row; the derivation goes in the click-to-expand details:
  - `annualized` — **SHORT** compact value only, e.g. `"≈1.5%/yr"`, `"≈+65%/yr"`, `"~+86% (5d one-shot)"`.
    This is what shows on the overview row. Do NOT put the explanation here.
  - `resolution_date` (e.g. `"2026-12-31"`, or `"rolling"` for signals) + `dte_days` (days from today).
  - `est_profit` — the estimated profit, $ and/or %, e.g. `"+25¢/share = +33% on a 75¢ NO"`.
  - `return_calc` — the **actual derivation**, shown only in the details: e.g. `"Buy NO @0.75 → pays
    $1.00 if not law = +0.25/share = +33% over ~188 days → ×365/188 ≈ +65%/yr"`. This is where the
    "return on the resolution date and how it's computed" lives.
  Annualize as `return_if_correct × 365/dte_days`; for a short binary, mark it one-shot in `annualized`
  and `return_calc`. SIGNAL ideas: `resolution_date:"rolling"`, `annualized:"n/a"`, `return_calc` = "measure via backtest".
- **`entry_level`** — the concrete **maker** plan: the limit price to *rest* at to enter (0 fee), e.g.
  "rest a maker BUY-NO bid @ 0.58 (0 fee)". MUST respect exchange minimums: ~$1 minimum notional per
  order + per-market min share counts — state the minimum ticket per leg, and for multi-leg structures
  confirm BOTH legs' minimums are satisfiable at available depth (else label "min-order infeasible"). **`exit_level`** — a rough **exit / take-profit** target,
  e.g. "take profit ~0.40 as it converges to fair; re-eval if it trades back above 0.62". Every
  *actionable* idea (arb, bond, directional) gets both — a trade is enter-as-maker-at-X, exit-at-Y, not
  just "buy at the ask."
- `chart` `{file, type: capacity|edge}` (rendered in Step 5b; the dashboard embeds it + a built-in
  "how to read this / what ROC means / how to act" explainer, so charts are never naked).
- `verification` (copied from the PASS critique — drives the "Fact-checked ✓" stamp).
- **`deliberation`** (from Step 11 — drives the mispricing gauge + agent×layer×verdict ledger):
  `{verifier, evidence, forecasts:[{lens,p_fair,reasoning}], panel:[{agent,layer,verdict,note,flag}],
  rounds, p_market, p_fair_agg, dispersion, contention, judge:{decision,conviction,rationale}}`. Idea
  `conviction` = `judge.conviction`. SIGNAL ideas omit `p_market`/`p_fair_agg`/`dispersion` (no gauge).
- SIGNAL only: `backtest_spec` (Step 9).

**Requirements on the ideas.json content:**
- **The ARBITRAGE section is never empty — there is always *some* edge, look harder.** Don't conclude
  "arb is dead." After the taker fee, most thin taker-arb nets negative — but the same edges are
  **capturable as a MAKER (0 fee)** by resting limit orders, and small/transient mispricings always
  exist somewhere. So: always surface the **best 3–5 available** arb/RV mispricings, ranked, even if
  small or maker-only, each honestly labeled (`maker-only (0 fee)` / `tiny` / `negRisk-convert` /
  `cross-event`). Evaluate every basket/link **both as taker (net fee) and as maker (fee=0,
  `depth_walk.py --fee-rate 0`)** and show the maker-capturable ones. Look across the *whole* board, not
  just the top liquid baskets — lower the liquidity floor (`scan_markets.py`), and actually run the
  cross-event link finder (Steps 2–4); the smallest cross-event locks are where retail never looks.
  If after all that the best arb is genuinely sub-0.5% even as a maker, say so — but still show it.
- **Conviction 0–100, ranked.** Assign each idea a conviction from your qualitative reasoning (not a
  formula); the dashboard sorts by it so the best surface on top.
- **Fair price explained simply, with a worked numeric example** — the single most important field. Show
  the inputs and the arithmetic in plain words.
- **Only real mispricings** become ideas (fair value ≠ price). Efficient ones go to `rejected` with the
  reason "fair value ≈ price (efficient)", or are omitted.
- **Quantity: aim for 8–15** PASS ideas across the four sections, ranked.
- **Default to a MAKER plan with entry + exit.** Every actionable idea (arb/bond/directional) states a
  concrete **maker entry level** (the limit price to rest at — 0 fee) and a **rough exit/take-profit
  level** (often your fair value, or $1 for a hold-to-resolution lock). A trade is "rest at X → exit at
  Y," not "buy at the ask." Use taker (cross the spread) only when filling fast matters, and then show
  the fee. This applies to fair-value/directional trades too, not just arbitrage.
- **Everything net of taker fee**; show the maker version (0 fee) wherever resting a limit captures it.
- **Scrutinize counterintuitive theses HARD** (the critic enforces this too).
- **Never oversell.** If nothing clears costs, the headline says so plainly.

## Repeating throughout the day

Each run is a fresh `$RUN` dir. Edges are **transient** (a 3% gap one day can be gone the next), so
re-running is the point. For a fast re-check of known candidates, skip Steps 2–4 and just re-run
`depth_walk.py` / `resolution.py` on saved trade specs. To study how often a gap appears, the user
can later pull historical depth from a paid source — but live snapshots need only the free `/book`.

## References
- `references/edge-formulas.md` — edge math per relationship type + how to build a depth_walk trade spec (read in Steps 4–5).
- `references/traps.md` — the mistakes that turn a "find" into nothing (read whenever an edge looks too good).
- `references/resolution-risk.md` — checklist to classify risk-free vs RV (read in Step 6).
- `references/agent-prompts.md` — the cross-event link-finder agent prompt + output schema (read in Step 3).
- `references/directional-plays.md` — how to find & evaluate directional edges, grounded in cited research (read in Step 7).
- `references/signals.md` — backtest_spec schema for repeatable signals, mapped to `desk/signals/` (read in Step 9).
- `references/critique.md` — the independent fact-check gate: critic checklist, indifferent stance, PASS/FAIL schema (read in Step 10).
- `references/deliberation.md` — the forecasting + debate panel: layers, conviction rubric, `deliberation` schema (read in Step 11).
- `references/panel-prompts.md` — per-role prompt templates (news/evidence, forecaster lenses, bull, bear, resolution, execution, judge) (read in Step 11).
- `references/research-notes.md` — the VERIFIED evidence base (what's documented to pay, what's refuted, empirical kill-lessons). Read at the START of every run; every lane + every verifier applies its kill-lessons checklist.
