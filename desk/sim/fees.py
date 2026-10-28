"""sim/fees.py — exact, date-aware trading fees.

Fees are NEVER tuned. Exact by Polymarket's OFFICIAL formula, validated to the cent
against on-chain fees in golden_replay.

OFFICIAL formula (docs.polymarket.com/trading/fees, verified June 2026):
    fee = C · feeRate · p · (1 - p)          # C = shares, p = price
  - Symmetric in dollars around p=0.50 (fee@0.30 == fee@0.70).
  - `feeRate` is the per-category PEAK rate (the value at p=0.50, where fee = C·feeRate·0.25).
  - Docs example: Crypto 100 sh @ 0.30 -> 100·0.07·0.30·0.70 = $1.47. ✓ matches on-chain.
  - MAKER orders are free (+ rebate, not modeled as a cost).
  - Takers only; live from 2026-03-30; min charge 0.00001 USDC; rounded to 5 dp.

NOTE on the earlier bug: a prior version used `effective_rate·C·price·4p(1-p)` (extra ·price
and ·4, and "effective" rates = feeRate/4). That under-charged ~2×. Corrected here.
"""
from __future__ import annotations

import os

# Taker fees live date (Sports first). Unix s.
FEE_START_TS = 1774828800  # 2026-03-30 00:00:00 UTC — the day Polymarket taker fees went live

# Official per-category feeRate = peak rate at p=0.50 (fee = C·feeRate·p·(1-p)).
CATEGORY_RATES = {
    "crypto": 0.07,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "tech": 0.04,
    "mentions": 0.04,
    "sports": 0.03,
    "geopolitics": 0.0,
    "world": 0.0,
}
DEFAULT_RATE = 0.05    # "Other/General" per docs
MIN_FEE = 0.00001


def taker_fees_enabled() -> bool:
    """Single knob: are Polymarket taker fees currently CHARGED?

    Polymarket announced per-category taker fees (schedule live 2026-03-30) but is not currently
    enforcing them - every observed live fill, including taker SELLs, carries fee_rate_bps=0
    (exchange-verified). A sim that charges the analytic feeRate*p*(1-p) books a phantom cost live
    never pays, and it over-charges SELLs vs BUYs (sells tend to execute nearer p=0.5, where the
    fee peaks).

    DEFAULT 'true' charges the documented rates (conservative for backtests). Set
    DESK_TAKER_FEES_ENABLED=false to align sim fees to the observed-live zero. Flip back the moment
    the exchange enforces taker fees again - one place, no code edit."""
    return os.environ.get("DESK_TAKER_FEES_ENABLED", "true").strip().lower() == "true"


def fee_rate(category: str | None) -> float:
    if not category:
        return DEFAULT_RATE
    return CATEGORY_RATES.get(category.strip().lower(), DEFAULT_RATE)


def fee_for(shares: float, price: float, *, mode: str = "taker",
            category: str | None = None, ts: int | None = None,
            rate: float | None = None) -> float:
    """Absolute USDC fee for one fill: fee = C·feeRate·p·(1-p).

    feeRate comes from `rate` if given (e.g. the market's on-chain-derived rate),
    else from `category`. mode="maker" -> 0. ts before FEE_START_TS -> 0.
    """
    if mode == "maker":
        return 0.0
    if ts is not None and ts < FEE_START_TS:
        return 0.0
    if not taker_fees_enabled():          # knob: align sim fees to observed-live zero
        return 0.0
    rate = fee_rate(category) if rate is None else rate
    if rate <= 0 or shares <= 0:
        return 0.0
    fee = rate * shares * price * (1.0 - price)
    if fee <= 0:
        return 0.0
    return round(max(fee, MIN_FEE), 5)
