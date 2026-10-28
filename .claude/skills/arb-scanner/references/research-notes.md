# Verified research base (deep-research + multi-vote adversarial verification per claim)

What is DOCUMENTED to make money on Polymarket, what is refuted, and what each lane should look for.
Every claim below survived independent verification; refuted claims are listed so no lane rebuilds on them.

## 1. Structural arbitrage — the richest documented pool (peer-reviewed)
- **~$40M realized arb profit Apr 2024–Apr 2025** (AFT 2025, arXiv 2508.03474; on-chain Polygon fills).
  Split: **negRisk basket rebalancing ~$29M** >> single-market YES+NO complements ~$10.6M >> cross-market
  combinatorial ~$95K (negligible — don't over-invest in cross-event combinatorial hunting).
  Caveats: pre-taker-fee era, election-heavy, upper bound (ε=$1/trade, gas not netted), top-3 wallets took ~$4.2M.
- **The primitive**: `NegRiskAdapter.convertPositions(marketId, indexSet, amount)` converts m NO tokens →
  `amount×(m−1)` collateral **immediately + unconditionally** + one YES of each remaining outcome.
  Scanner formula: **sum(NO asks) < m−1 ⇒ locked profit at conversion**, YES tails are free upside.
  Two mandatory checks: (a) per-market `feeBips` deducted at conversion; (b) value-equivalence holds ONLY
  if exactly one outcome resolves true — **if ALL resolve false you lose $1/unit** (negRisk = mutually
  exclusive, NOT necessarily exhaustive — verify a guaranteed winner). (Sources: Polymarket
  neg-risk-ctf-adapter docs + Solidity.)
- Note for the competition/short-PnL use case: convert realizes collateral **instantly** — the only
  strategy family with same-day realized PnL besides MM rewards.

## 2. Market-making vs the liquidity-rewards program (machine-readable economics)
- Paid **daily at midnight UTC** to maker wallets ($1/day min, no rollover) — PnL realizes daily.
- Quadratic tightness scoring `S(v,s)=((v−s)/v)²·b`; two-sidedness rule with c=3.0 (two-sided ≈3× reward
  for equal size; **mid outside [0.10,0.90] ⇒ one-sided scores ZERO** — rewards there force pick-off risk).
- Per-market params are live API data — **never hardcode**: Gamma `rewardsMinSize`/`rewardsMaxSpread`,
  CLOB `/sampling-markets` → `rewards.rates[].rewards_daily_rate`, `min_size`, `max_spread`.
- Rewards are pro-rata vs competitors → realized yield unknowable statically; rank candidates by
  daily-rate-per-competition proxy (poly-maker's `gm_reward_per_100`), prefer LOW-vol markets (adverse
  selection is the cost). Case study: $10K → $200/day (peak $700–800) mid-2025, **decayed** to ~$30–80/day
  per $10K after reward cuts; poly-maker README: naive bot now loses money. Treat as yield-with-work.

## 3. One robust directional bias: political-favorite underconfidence
- Political markets are **persistently underconfident** — prices compressed toward 50%; calibration slope
  up to 1.83 at ≥1w horizons; **replicates on Polymarket: mean slope 1.31** (227.6M trades) (arXiv
  2602.19520; corroborated SSRN 5910522: favorites >55% systematically underpriced).
- Scanner action: **buy long-horizon POLITICAL favorites** (60–90¢) — the market under-prices how often
  they win. This IS the classic favorite-longshot bias (not "reversed"); 1–3h horizon shows mild
  overconfidence instead, so don't apply it intraday. Gross bias ≠ net tradability: net it against
  long-horizon carry (vs T-bills) + fee + spread before calling it a trade.
- Liquid short-horizon **sports are well-calibrated** (slopes 0.90–1.10 ≤48h; SSRN 5910522, 478M trades)
  — fading them is net-losing after costs (confirm with a backtest before trusting). Sports >1mo horizon
  drifts underconfident (slope ~1.74) — same buy-favorites logic, weaker evidence.
- LLM forecasting: best single LLM matched market accuracy but earned higher BACKTEST returns
  (Forecast@ICML26 Spotlight) — hypothesis only; live-execution evidence says accuracy edges can go
  negative net. Keep the panel as judge, don't assume LLM-vs-market alpha.

## REFUTED (0-3) — do NOT build lanes on these
- "41% of conditions had arb" / "only 1% of election arb was exploited" (capacity stats — killed).
- "No FLB on Polymarket; scan Yes-side overpricing instead" (killed; the underconfidence result stands).
- "Mispricing concentrates early-life + near-resolution" (killed as a general timing rule).
- "Weather overconfident ≤48h — fade extreme weather prices" (killed).
- LLM edge mechanism attribution + ensemble-agreement filter (killed).

## Unverified-but-plausible angles (zero surviving claims either way — treat as hypotheses, verify hard)
current per-category taker-fee table (verify live at docs.polymarket.com each run) · UMA 2h dispute
window / settlement-lag capture (buy near-$1 right after outcome known, before resolution) · cross-platform
Kalshi/sportsbook spreads · whale/copy-trading · in-play sports · news lead-lag. Polymarket added
**dynamic fees on short-term crypto markets to curb latency arb** (Finance Magnates) — assume the 15-min
up/down latency game is patched.

## Empirical kill-lessons — check these EVERY idea
1. **Book-side misread**: "deep book" is often the BID (sell side), not buyable ask depth. Always verify
   which side you can actually BUY and its size.
2. **Weather = station, not city**: markets resolve on the AIRPORT station (Barajas/Malpensa/EDDM…) via
   Wunderground — city forecasts are systematically different. Pull the station forecast.
3. **Stale externals**: sportsbook lines (opening vs live), polls (check the YEAR), forecast trackers —
   re-fetch everything live; it is easy to build a wrong idea on a stale (e.g. prior-year) poll.
4. **Vol-window robustness**: a crypto fair value that flips sign between 14d/30d/90d realized vol is NOT
   an edge. Require the sign to survive all reasonable windows.
5. **NO above fair ≠ bond**: near-certainty is only a trade if price < fair MATERIALLY (exit-discount),
   not "barrier is far away". Recompute fair; many 0.97s are worth 0.97.
6. **Minimum order sizes break micro-arbs**: Polymarket enforces ~$1 minimum notional per order (+
   per-market minimum share counts on limit orders). A leg priced <2¢ forces a 50-1,000+ share order —
   if the OTHER leg's depth can't match that share count, a paired/arb structure is INFEASIBLE at any
   size. Every proposed idea must state per-leg minimums and confirm the pair is balanceable; flag
   "min-order infeasible" instead of proposing dust arbs. (E.g. a ranked #1/#2 pair where the #2 leg
   trades at 0.1¢ forces a ~1,000-share order, but the #1 leg's book holds only a few shares — the
   pair can't be balanced at any size.)
