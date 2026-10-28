"""SHARED schemas. Imported everywhere; never redefine locally.

Every module's I/O is one of these dataclasses. Fields are append-only across the
codebase — do not rename fields (producers and consumers agree on names here).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Optional


# [price, size] level pair. Full ladder = list of these.
Level = List[float]


@dataclass
class BookSnapshot:
    """store -> fill_model. Full ladder; T5 = polled CLOB /book, hash-dedup write-on-change.

    store serves book@t via as-of lookup: latest row with capture_ts <= t.
    """
    market_id: str            # conditionId
    token_id: str             # asset_id (outcome token)
    capture_ts: int           # our poll time (unix s)
    exchange_ts: int          # book.timestamp from CLOB (unix s)
    hash: str                 # book.hash — dedup key
    bids: List[Level]         # full ladder, sorted price desc
    asks: List[Level]         # full ladder, sorted price asc
    best_bid: float
    best_ask: float
    mid: float
    spread: float             # best_ask - best_bid (single value)
    tick_size: float
    min_order_size: float
    neg_risk: bool
    last_trade_price: Optional[float]
    fee_rate_base: Optional[float] = None   # CLOB /fee-rate base_fee; per-trade fee = formula(rate,p,size)
    fidelity_tier: str = "T5"


@dataclass
class Trade:
    """Supplementary live print (Data /trades). A subset for validation / last-trade;
    it lacks maker/taker attribution and the absolute fee (those come from OnchainFill)."""
    market_id: str
    token_id: str
    ts: int
    price: float
    size: float
    side: str                 # BUY | SELL
    tx_hash: str
    wallet: str
    outcome: str
    fee_rate_bps: Optional[float] = None   # absent in Data /trades; derive via fee formula


@dataclass
class CaptureConfig:
    """Input to capture_select + capture."""
    tags: List[str] = field(default_factory=list)        # Gamma tag slugs, e.g. ["macro","sports"]
    min_volume24hr: float = 0.0
    end_within_days: Optional[int] = None                # only markets resolving within window
    explicit_slugs: List[str] = field(default_factory=list)  # hand-pick; always included
    lanes: List[dict] = field(default_factory=list)          # [{name, tag_ids:[int], count, min_volume24hr?, exclude_substrings?}]
    cap_patterns: List[dict] = field(default_factory=list)   # [{substring, max}] global cap on near-identical market families
    max_markets: int = 6
    poll_secs: float = 5.0                               # /book poll cadence
    trades_poll_secs: float = 15.0                       # /trades poll cadence
    fee_refresh_secs: float = 1800.0                     # /fee-rate refresh cadence (rarely changes)
    refresh_universe_secs: float = 3600.0                # re-select universe: drop resolved, add new
    flush_secs: float = 10.0                             # parquet flush + heartbeat cadence
    out_dir: str = "desk/data/capture/"
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    data_url: str = "https://data-api.polymarket.com"
    max_secs: Optional[float] = None                     # stop after N seconds (None = run forever)

    @staticmethod
    def from_json(d: dict) -> "CaptureConfig":
        known = {f for f in CaptureConfig.__dataclass_fields__}  # type: ignore[attr-defined]
        return CaptureConfig(**{k: v for k, v in d.items() if k in known})


@dataclass
class CaptureStats:
    """Run summary emitted on shutdown."""
    tokens: int = 0
    book_rows: int = 0
    trade_rows: int = 0
    polls: int = 0
    max_gap_s: float = 0.0
    bytes: int = 0
    started_ts: int = 0
    ended_ts: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Execution / backtest schemas. Appended (not interleaved) so the data-layer and
# execution-layer schemas never collide on edit. Strict producer-out = consumer-in:
# store->fill_model, engine(ExecReport)->portfolio/runner/tracker/recon.
# runner.run and executor.run share a signature.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Order:
    """strategy -> engine (sim AND live). Timestamped intent; engine is strategy-agnostic."""
    order_id: str
    strategy_id: str
    market_id: str
    token_id: str
    side: str                       # BUY | SELL
    submit_ts: int                  # decision time (unix s); store serves book as-of this
    limit_price: float              # worst acceptable price
    qty: float                      # shares
    tif: str = "GTC"                # GTC | IOC | FOK
    expiry_ts: Optional[int] = None # for GTC/maker validity window
    mode: str = "taker"             # taker (cross spread, IOC-style) | maker (rest at limit)
    # Optional recent-volatility / entry-state hint (price units) the strategy already knows at entry —
    # e.g. gap-from-fair. The cost model's vol-regime conditioning uses it directly (vs `meta.vol_edges`)
    # so a high-volatility entry is charged the wide-book cost even on a historical path where `fill_model.
    # _recent_vol` can't measure a 600s mid-range from coarse (hourly) prices-history. None -> fall back
    # to the series-inferred vol. Append-only; engine reads it but never requires it (strategy-agnostic).
    entry_vol: Optional[float] = None


@dataclass
class Fill:
    """One execution against an order. fee is the absolute pUSD fee for this fill."""
    ts: int
    price: float
    qty: float
    fee: float


@dataclass
class ExecReport:
    """engine -> portfolio/runner/tracker/recon. "Net impact on position" for one order.

    fidelity_tier records WHICH data tier produced the fill (T5 ladder-walk is exact;
    T1 mid is an estimate). degraded/warnings flag any fill below the recorded book so
    estimated fills are never silently blended with exact ladder-walks.
    """
    order_id: str
    strategy_id: str
    fills: List[Fill]
    filled_qty: float
    avg_fill_price: float
    fees: float
    slippage_bps_vs_mid: Optional[float]   # None = no true book mid at fill time (live marketable-limit / reconciled fill) — NOT 0.0, so an unmeasurable is never reported as a real 0-bps fill
    unfilled_qty: float
    d_position: float
    d_cash: float
    source: str                     # SIM | LIVE
    fidelity_tier: str              # T0..T5 used for this fill
    degraded: bool = False          # True if filled below the recorded-book tier
    warnings: List[str] = field(default_factory=list)
    # Append-only. A cost-ESTIMATE reliability label, NOT a trade signal:
    # the engine executes faithfully and reports the best cost it can; these say how much to
    # trust that number. est_confidence: "exact" real ladder-walk | "estimated" T1 priced by
    # the spread_model | "low" unpriced/degraded | "none" no fill. Whether to trade on a
    # low-confidence or negative-PnL fill is the STRATEGY's call, never the engine's.
    cost_est_c: Optional[float] = None     # inferred exec cost applied vs mid, in cents (T1+model)
    cost_p90_c: Optional[float] = None     # p90 of that cost, cents — the uncertainty band
    spread_source: Optional[str] = None    # market|peer|category|global|insufficient (which backoff priced it)
    est_confidence: Optional[str] = None
    slippage_c: Optional[float] = None     # per-market TEMPORAL slippage applied (book-move over the lag window), cents
    # Append-only (maker queue-ahead). Book depth AT-OR-BETTER than the resting limit at SUBMIT
    # (the volume that must trade through before our order is reached). Set on maker orders only; None on
    # taker or when the submit-book was missing (fail-closed unfilled). The ledger records it per TradeRow.
    queue_ahead: Optional[float] = None
    # Append-only. The EXCHANGE order id the live CLOB assigned (None in sim / when the post response
    # carried no id). The driver persists it so its reconciliation pass can re-poll THIS order for fills
    # that landed after the immediate post-response — the one snapshot a single post ever sees.
    exchange_order_id: Optional[str] = None


@dataclass
class OnchainFill:
    """Authoritative realized fill (OrderFilled/FeeCharged): real price/size/side +
    maker/taker + absolute fee — the golden-set truth. Distinct from `Trade` (the
    supplementary Data /trades print, which lacks maker/taker/fee)."""
    market_id: str
    token_id: str
    ts: int
    price: float
    size: float
    side: str                       # BUY | SELL (taker perspective)
    maker: str
    taker: str
    tx_hash: str
    fee: float                      # absolute pUSD fee charged (base + any builder fee)
    fee_rate_bps: Optional[float] = None
    builder: str = ""               # on-chain builder code; non-zero => app charged an extra builder fee


@dataclass
class Resolution:
    """T0 truth. payout = 1.0 if THIS token won, else 0.0 (binary CTF outcome)."""
    market_id: str
    token_id: str
    resolved_ts: int
    payout: float                   # 1.0 | 0.0 for this token
    outcome: str                    # human label, e.g. "Yes"
    source: str = "GAMMA"           # UMA | GAMMA


@dataclass
class PriceSeries:
    """T1 mid/last series (CLOB /prices-history). points = [[ts, price], ...]."""
    market_id: str
    token_id: str
    points: List[List[float]]
    interval: str = "1m"            # 1m/1h/6h/1d/1w/max


@dataclass
class Position:
    """Per (market,token) running position. avg_cost in pUSD/share."""
    market_id: str
    token_id: str
    qty: float = 0.0
    avg_cost: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class BacktestResult:
    """runner.run / executor.run output. equity_curve = [[ts, equity], ...].
    bracket = {opt, base, adv} P&L under optimistic/base/adversarial fill assumptions.
    data_quality = {total_fills, degraded_fills, by_tier} so estimated fills are visible."""
    equity_curve: List[List[float]] = field(default_factory=list)
    total_pnl: float = 0.0
    exec_pnl: float = 0.0
    resolution_pnl: float = 0.0
    fill_ratio: float = 0.0
    bracket: dict = field(default_factory=dict)
    exec_reports: List[ExecReport] = field(default_factory=list)
    data_quality: dict = field(default_factory=dict)


@dataclass
class LedgerSnapshot:
    """tracker/pnl_ledger persistent cross-run state. by_strategy[sid] = {realized,unrealized,fees}."""
    as_of: int
    by_strategy: dict = field(default_factory=dict)
    runs_logged: int = 0


@dataclass
class ImpactModel:
    """sim/impact.py output (DEFERRED): g(Q)=kappa*sqrt(Q) per liquidity tier. Historical
    no-depth fill backend only; not used for T5 ladder-walk paper trading."""
    kappa_by_tier: dict = field(default_factory=dict)
    beta: float = 0.5               # exponent (0.5 = sqrt-impact)
    fit_window: str = ""


@dataclass
class AutotuneResult:
    """eval/recon output. NOTE: per the evaluator design, `fitted` holds evidence-grounded
    per-(market,time) assumptions (latency_ms, queue fill-prob) with documented rationale —
    NOT mechanically optimized params. Fees are never tuned. deviation_bps before/after a fix."""
    fitted: dict = field(default_factory=dict)
    deviation_bps_before: float = 0.0
    deviation_bps_after: float = 0.0
    n_orders: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Strategy-container schemas. Additive; existing dataclasses above are frozen.
# Strategies stay pure functions emitting Order[]; identity is carried via
# Order.strategy_id. See desk/signals/.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ExitPolicy:
    """Explicit, model-based exit levels (in strategy params). All sweepable.
    hold_to_maturity disables exits (baseline). model_stop_edge is PRIMARY (thesis-revised cut)."""
    hold_to_maturity: bool = False
    tp_capture: float = 0.7
    model_stop_edge: float = 0.0
    price_stop: Optional[float] = None          # SL: exit on adverse move >= this (a.k.a. stop_dist)
    max_hold_days: Optional[int] = None
    tp_to_fair: bool = False                     # per-cycle TP: exit when price reaches the (recomputed) fair value
    tp_band: float = 0.02                        # "reached fair" tolerance (|edge| <= tp_band)

    @staticmethod
    def from_dict(d: Optional[dict]) -> "ExitPolicy":
        d = d or {}
        known = {f for f in ExitPolicy.__dataclass_fields__}  # type: ignore[attr-defined]
        return ExitPolicy(**{k: v for k, v in d.items() if k in known})


@dataclass
class StrategyConfig:
    """signals/strategy_config.json row (pure data)."""
    name: str
    enabled: bool = True
    schedule: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)

    @staticmethod
    def from_json(d: dict) -> "StrategyConfig":
        known = {f for f in StrategyConfig.__dataclass_fields__}  # type: ignore[attr-defined]
        return StrategyConfig(**{k: v for k, v in d.items() if k in known})


@dataclass
class StrategySpec:
    """signals/registry.py entry. name == strategy_id; generate_orders bound at registration."""
    name: str
    schedule: dict = field(default_factory=dict)
    enabled: bool = True
    params: dict = field(default_factory=dict)
    generate_orders: Optional[object] = None


# ─────────────────────────────────────────────────────────────────────────────
# ONE ledger: TradeRow event log + RunMeta. Append-only. tracker/ledger.json keeps
# the aggregates; tracker/trades.jsonl gets one TradeRow per ORDER EVENT
# (open/resting/closed) for backtest·paper·live so the dashboard reads one contract
# per --source. decomp + slug are logged AT TRADE TIME (no renderer/adapter ever
# fetches the network — Gamma drops resolved markets, so a closed trade's pricing
# can't be reconstructed later). Every row carries code_ref / config_hash / env so a
# misconfigured span can be identified and excluded, not reconstructed by archaeology.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class TradeRow:
    """One order EVENT in tracker/trades.jsonl (append-only). Schema is the dashboard's
    single contract across backtest|paper|live. status: open (taker/marketable filled) |
    resting (maker order placed, unfilled) | closed (settled — carries pnl_realized).

    Field provenance: qty/price/fees/status from the ExecReport; edge/fair/mid_entry/decomp/
    slug joined from the strategy's out_signals at log time (never re-fetched); pnl_mtm marks
    an open position at live_mid; url built from slug. `extras` carries per-source detail
    (settle source, sim-vs-live deviation, run_id linkage) without widening the core schema."""
    run_id: str
    source: str                     # backtest | paper | live
    strategy: str
    config_hash: str
    order_id: str
    market_id: str
    token_id: str
    slug: Optional[str]
    url: Optional[str]              # https://polymarket.com/market/<slug> | None
    ts: int                        # event unix s
    side: str                      # BUY | SELL
    qty: float                     # filled qty for open/resting; position qty for closed
    price: float                   # avg fill price (entry) for open; settle payout context for closed
    limit_price: Optional[float]
    mode: str                      # taker | maker | marketable
    status: str                    # open | resting | closed
    fees: float
    edge: Optional[float] = None
    fair: Optional[float] = None
    mid_entry: Optional[float] = None
    live_mid: Optional[float] = None
    pnl_realized: Optional[float] = None    # closed only
    pnl_mtm: Optional[float] = None         # open only (mark at live_mid)
    queue_ahead: Optional[float] = None
    fidelity_tier: Optional[str] = None
    degraded: bool = False
    decomp: Optional[dict] = None
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunMeta:
    """desk/eval/runs/<run_id>/run.json — one per backtest run_id (an ablation grid = many).
    Pins the run to its code + config + environment so a headline number can never silently
    drift off its config-of-record. config_hash reuses registry.config_hash shape but is
    computed on the config dict. gate = the recon PASS marker contents if present, else None."""
    run_id: str
    source: str
    strategy: str
    config: dict
    config_hash: str
    starting_cash: float = 0.0
    budget: float = 0.0
    window: dict = field(default_factory=dict)          # {min_ts, max_ts}
    cost_band: dict = field(default_factory=dict)        # bracket {opt, base, adv}
    code_ref: str = "unknown"                            # git rev-parse HEAD (fail-soft)
    env: dict = field(default_factory=dict)              # captured env flags, e.g. {LIVE_TRADING_ENABLED}
    gate: Optional[str] = None                           # recon PASS marker contents | None
    settle_source_counts: dict = field(default_factory=dict)   # {GAMMA: n, CLOB: n}
    created_ts: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# The weather dataclasses (EnsembleForecast, ForecastSnapshot, WeatherMarketSpec) moved into the
# weather tenant's package (pre-flip-generality P2): they serialize only inside tenant substrate
# files, so ownership follows the data — this module stays market-generic. Their field schemas are
# unchanged there (append-only in effect).
