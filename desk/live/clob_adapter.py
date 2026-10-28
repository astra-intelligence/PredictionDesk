"""live/clob_adapter.py — real CLOB execution via py-clob-client-v2.

GATED + capped. Returns a FillResult(source="LIVE") so engine.execute treats live and
sim identically. Safety, in order: geo gate -> LIVE_TRADING_ENABLED -> evaluator PASS
(unless allow_pre_pass) -> $ cap -> (optional dry_run signs-but-doesn't-post). UNTESTED against
a live account until you run it — so start read-only: `preflight()` proves auth+balance+geo with
NO order.

py-clob-client method names can vary by version; calls are defensive and fail with a clear
message so a mismatch surfaces during the read-only preflight, never mid-order.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from desk.types import Order, Fill
from desk.sim.fill_source import FillResult
from desk.live.geo import geo_gate


@dataclass
class OrderFills:
    """poll_fills_for_order result: every recorded fill of ONE live order + the exchange's order status,
    so the driver's reconciliation pass can book newly-discovered fills and retire the order when it is
    MATCHED/CANCELED (done). `status` is None when the status probe failed."""
    order_id: str
    fills: List[Fill] = field(default_factory=list)
    status: Optional[str] = None            # LIVE | MATCHED | CANCELED | ... | None
    degraded: bool = False
    warnings: List[str] = field(default_factory=list)

    @property
    def filled_qty(self) -> float:
        return round(sum(f.qty for f in self.fills), 6)


class ClobAdapter:
    def __init__(self):
        self.host = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")
        self.chain_id = int(os.environ.get("CHAIN_ID", "137"))
        self.enabled = os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"
        self.max_order_usd = float(os.environ.get("MAX_ORDER_USD", "5"))
        self.sig_type = int(os.environ.get("SIGNATURE_TYPE", "2"))
        # Fee unit: sim/fees.py works in USDC DOLLARS, so the LIVE fee must be dollars too for a
        # sim-vs-live fee comparison to mean anything. POLY_FEE_DIVISOR pins raw→dollars. DEFAULT 1e6:
        # the CLOB API returns every USDC amount in 6-decimal base units, so the `fee` field is
        # 6-decimal too → /1e6. A deterministic default replaces the old magnitude heuristic (which
        # flagged every live fill degraded out of the box). A zero fee is unit-unambiguous (0/x = 0)
        # so it never degrades regardless. Override only if a confirmed NONZERO fill proves otherwise.
        self.fee_divisor = float(os.environ.get("POLY_FEE_DIVISOR", "1000000"))
        self._client = None

    # ── connection / auth (read-only) ─────────────────────────────────────────
    def _connect(self):
        if self._client is not None:
            return self._client
        try:
            # CLOB V2 (exchange migrated 2026-04-28; EIP-712 domain v2). The archived py-clob-client
            # 0.34.x signs V1 orders the exchange rejects with "invalid order version".
            from py_clob_client_v2 import ClobClient
        except ImportError as e:
            raise RuntimeError("pip install py-clob-client-v2") from e
        key = os.environ.get("POLY_PRIVATE_KEY")
        funder = os.environ.get("POLY_FUNDER_ADDRESS")
        if not key or not funder:
            raise RuntimeError("POLY_PRIVATE_KEY / POLY_FUNDER_ADDRESS missing in .env")
        client = ClobClient(self.host, key=key, chain_id=self.chain_id,
                            signature_type=self.sig_type, funder=funder)
        # derive L2 API creds in-code (frontend keys fail v2 proxy auth)
        derive = getattr(client, "create_or_derive_api_creds", None) or \
            getattr(client, "create_or_derive_api_key", None)
        if derive is None:
            raise RuntimeError("py-clob-client: no create_or_derive_api_creds/key method")
        client.set_api_creds(derive())
        self._client = client
        return client

    def sync_balance_allowance(self) -> bool:
        """Refresh the CLOB's SERVER-SIDE balance/allowance cache (POST /balance-allowance/update).
        The order endpoint checks a CACHED collateral balance that goes stale after a deposit/transfer
        the CLOB never observed -> every order rejects 'not enough balance / allowance: balance: 0'
        despite real on-chain funds. This is a read/refresh, NOT an on-chain tx (no gas). Fail-soft:
        any error -> False, the run proceeds (the order just fails as it would have anyway)."""
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            c = self._connect()
            c.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                                                              signature_type=self.sig_type))
            return True
        except Exception:
            return False

    def preflight(self) -> dict:
        """READ-ONLY: geo + auth + balance. NO order placed. Run this first, solo."""
        g = geo_gate()
        out = {"geo_ok": g.ok, "egress": g.place, "country": g.country, "geo_reason": g.reason,
               "live_enabled": self.enabled, "max_order_usd": self.max_order_usd}
        if not g.ok:
            out["status"] = "BLOCKED by geo — do not trade"
            return out
        client = self._connect()
        out["auth"] = "ok (API creds derived)"
        try:
            # COLLATERAL + signature_type required: a bare BalanceAllowanceParams() 400s
            # ("Invalid asset type") — same call shape as sync_balance_allowance/_conditional_balance.
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            bal = client.get_balance_allowance(BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL, signature_type=self.sig_type))
            out["balance"] = bal
        except Exception as e:
            out["balance"] = f"(could not fetch: {e!r})"
        out["status"] = "preflight OK — auth + geo good"
        return out

    # ── order placement (gated, capped) ───────────────────────────────────────
    def execute(self, order: Order, dry_run: bool = False, allow_pre_pass: bool = False) -> FillResult:
        """Send a real (capped) order. DOUBLE-GATE enforced HERE, at the money-sending layer:
        geo → LIVE_TRADING_ENABLED → evaluator PASS → $ cap. `allow_pre_pass=True` is the ONE
        documented exception — the small sim-vs-live bootstrap, which CANNOT require PASS because
        it's the test that earns PASS. Every other caller (incl. production `executor` via
        `LiveFillSource`) leaves it False, so PASS is asserted defense-in-depth even though the
        executor already gates on it."""
        g = geo_gate()
        if not g.ok:
            raise RuntimeError(f"GEO BLOCK: egress {g.place} — {g.reason}. VPN off + re-check.")
        if not self.enabled:
            raise RuntimeError("LIVE_TRADING_ENABLED=false — refusing to send a real order")
        if not allow_pre_pass:
            from desk.live.executor import pass_scope   # lazy: executor imports this module
            if pass_scope() is None:
                raise RuntimeError(
                    "live blocked: no evaluator PASS marker. Production orders require PASS "
                    "(eval/recon/skills.md). Only the small sim-vs-live bootstrap may set "
                    "allow_pre_pass=True.")
        # $ cap on WORST-CASE COLLATERAL — SIDE-CORRECT: a BUY risks the premium qty*price; a SELL/short
        # posts the qty*(1-price) leg. (A symmetric max(price, 1-price) bound over-rejects a converted
        # BUY-NO that shorts a high-priced YES: a real $1 buy reads as its ~$1-of-NO short leg.)
        # Enforced pre-sign (defense in depth).
        worst_case = order.qty * (order.limit_price if order.side == "BUY" else 1.0 - order.limit_price)
        if worst_case > self.max_order_usd:
            raise RuntimeError(f"order worst-case collateral ${worst_case:.2f} > MAX_ORDER_USD "
                               f"${self.max_order_usd} (qty {order.qty} × {'price' if order.side=='BUY' else '1-price'} "
                               f"{order.limit_price if order.side=='BUY' else 1.0-order.limit_price:.3f})")

        client = self._connect()
        mid = self._book_mid(client, order.token_id)    # true send-time mid for slippage; None if unavailable
        from py_clob_client_v2 import MarketOrderArgs, OrderArgs, OrderType, Side
        otype = OrderType.FOK if order.tif == "FOK" else (
            OrderType.FAK if order.tif in ("IOC", "FAK") else OrderType.GTC)
        side = Side.BUY if order.side == "BUY" else Side.SELL
        if otype in (OrderType.FOK, OrderType.FAK):
            # marketable orders are "market orders" to the CLOB: BUY amount = USDC dollars (max 2dp),
            # SELL amount = shares (max 4dp). Signing a plain limit order (size×price → up to 4dp
            # maker amount) gets rejected: 'market buy orders maker amount supports a max accuracy of
            # 2 decimals'). create_market_order rounds amounts per the market's tick config; price
            # still caps the marketable limit.
            amt = (int(order.qty * order.limit_price * 100) / 100.0 if order.side == "BUY"
                   else round(order.qty, 4))
            margs = MarketOrderArgs(token_id=order.token_id, amount=amt, side=side,
                                    price=order.limit_price, order_type=otype)
            signed = client.create_market_order(margs)  # signs locally
        else:
            args = OrderArgs(price=order.limit_price, size=order.qty, side=side,
                             token_id=order.token_id)
            signed = client.create_order(args)          # signs locally
        if dry_run:
            return FillResult([], "LIVE_DRYRUN", mid, order.qty, "LIVE",
                              degraded=True, warnings=["dry_run: order signed, NOT posted"])
        resp = client.post_order(signed, otype)         # REAL ORDER
        return self._map_fills(client, order, resp, mid)

    def _conditional_balance(self, client, token_id: str):
        """Shares of the outcome (conditional) token currently held (6-dec base units -> shares), or **None
        on error**. Callers MUST treat None as 'unknown' and FAIL-CLOSED (leave a SELL as-is), NOT as zero —
        else a transient balance-query error while CLOSING a held long would look like a short-to-open and
        get wrongly converted into a brand-new position (doubling exposure)."""
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            b = client.get_balance_allowance(BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=self.sig_type))
            return float((b or {}).get("balance") or 0.0) / 1e6
        except Exception:
            return None

    def _complement_token(self, client, market_id: str, token_id: str):
        """The OTHER outcome token in the same market/condition (the NO to our YES). Cached per token.
        None if it can't be resolved."""
        cache = getattr(self, "_complement_cache", None)
        if cache is None:
            cache = self._complement_cache = {}
        if token_id in cache:
            return cache[token_id]
        other = None
        try:
            m = client.get_market(market_id)
            for t in (m or {}).get("tokens", []):
                tid = t.get("token_id")
                if tid and str(tid) != str(token_id):
                    other = str(tid); break
        except Exception:
            other = None
        cache[token_id] = other
        return other

    def _book_mid(self, client, token_id: str):
        """True book midpoint for `token_id` at send time, or None. engine._assemble computes
        slippage_bps_vs_mid off FillResult.mid; passing order.limit_price there made the field
        meaningless for a marketable limit (it measured fill-vs-limit, not fill-vs-mid). Fetch the real
        /midpoint pre-send; on any failure return None so the engine reports slippage=None (unmeasurable),
        never a fake 0-bps. Best-effort + fail-soft: a midpoint fetch must NEVER block or fail an order."""
        try:
            m = client.get_midpoint(token_id)          # /midpoint → {"mid": "0.55"} (str); None-safe below
            mid = m.get("mid") if isinstance(m, dict) else m
            return float(mid) if mid is not None else None
        except Exception:
            return None

    def _fee_to_usdc(self, raw_fee: float):
        """raw exchange fee → USDC dollars. Returns (fee_dollars, warning_or_None)."""
        if self.fee_divisor > 0:                       # pinned from a confirmed fill — trusted
            return raw_fee / self.fee_divisor, None
        div = 1e6 if raw_fee > 100 else 1.0            # UNVERIFIED heuristic — flag it
        return raw_fee / div, (f"FEE UNIT UNVERIFIED: raw_fee={raw_fee} assumed /{div:g}. "
                               f"Pin POLY_FEE_DIVISOR from this fill before trusting fee_diff.")

    def _fills_from_trades(self, trades, oid: str, warns: list) -> list:
        """Every Fill of OUR order id `oid` across a CLOB /trades list, matching BOTH sides: our wallet is
        the TAKER (`taker_order_id == oid`, size/price/fee at the trade root) OR a MAKER (`oid` appears in
        `maker_orders[]`, whose element carries THIS leg's `matched_amount`/`price`/`fee`). A GTC/maker order
        fills as maker, so a taker-only match (`t['order_id']`) would silently miss every maker fill — this
        matches both. Older/simple response shapes (`order_id`/`size`/`fee` at root) still match via the
        taker branch's fallback keys."""
        out = []
        for t in trades or []:
            troot = str(t.get("taker_order_id") or t.get("order_id") or t.get("orderID") or "")
            ts = int(t.get("match_time", 0) or 0)
            if oid and troot == str(oid):                    # we were the taker
                px, sz = float(t.get("price", 0) or 0), float(t.get("size", 0) or 0)
                raw_fee = float(t.get("fee", 0) or 0)
                fee, w = self._fee_to_usdc(raw_fee)
                if w and w not in warns:
                    warns.append(w)
                if sz > 0:
                    out.append(Fill(ts=ts, price=px, qty=sz, fee=fee))
                continue
            for mo in (t.get("maker_orders") or []):         # we rested as a maker on this trade
                if str(mo.get("order_id") or "") != str(oid):
                    continue
                px = float(mo.get("price", t.get("price", 0)) or 0)
                sz = float(mo.get("matched_amount", 0) or 0)
                raw_fee = float(mo.get("fee", 0) or 0)
                fee, w = self._fee_to_usdc(raw_fee)
                if w and w not in warns:
                    warns.append(w)
                if sz > 0:
                    out.append(Fill(ts=ts, price=px, qty=sz, fee=fee))
        return out

    def _map_fills(self, client, order: Order, resp: dict, mid=None) -> FillResult:
        oid = (resp or {}).get("orderID") or (resp or {}).get("orderId")
        warns = [f"live order {oid}"]
        if mid is None:                                 # no send-time mid → engine reports slippage=None
            warns.append("no send-time book mid — slippage_bps_vs_mid unmeasurable")
        try:
            from py_clob_client_v2 import TradeParams
            trades = client.get_trades(TradeParams(maker_address=os.environ.get("POLY_FUNDER_ADDRESS"),
                                                   asset_id=order.token_id))
            fills = self._fills_from_trades(trades, str(oid) if oid else "", warns)
        except Exception as e:
            return FillResult([], "LIVE", mid, order.qty, "LIVE",
                              degraded=True, warnings=[f"order posted (resp={resp}); fill fetch failed: {e!r}"])
        filled = sum(f.qty for f in fills)
        return FillResult(fills, "LIVE", mid, round(order.qty - filled, 4), "LIVE",
                          degraded=self.fee_divisor <= 0, warnings=warns,
                          exchange_order_id=str(oid) if oid else None)

    # ── reconciliation / lifecycle ────────────────────────────────────────────
    def poll_fills_for_order(self, order_id: str, token_id: str = "",
                             known_qty: float = 0.0) -> "OrderFills":
        """READ-ONLY: the fills of ONE resting/marketable order the immediate post-response missed.
        A GTC/maker order (or a marketable limit that only partially crossed) keeps filling AFTER `execute`
        returned its one instant `get_trades` snapshot — those later fills were never recorded. Re-poll /trades
        for `order_id`, return every fill of it. Bounded by the client's own HTTP timeout; NO order is placed.
        Returns fills + the exchange's order status (LIVE|MATCHED|CANCELED|None) so the driver can retire a
        fully-done order. `known_qty` = qty already booked locally, so the caller can append only the NEW tail."""
        warns: list = []
        client = self._connect()
        from py_clob_client_v2 import TradeParams
        trades = client.get_trades(TradeParams(maker_address=os.environ.get("POLY_FUNDER_ADDRESS"),
                                               asset_id=token_id or None))
        fills = self._fills_from_trades(trades, str(order_id), warns)
        status = None
        try:
            od = client.get_order(str(order_id)) or {}
            status = od.get("status")
        except Exception:
            pass                                             # status best-effort; fills are the source of truth
        return OrderFills(order_id=str(order_id), fills=fills, status=status,
                          degraded=self.fee_divisor <= 0, warnings=warns)

    def cancel(self, order_id: str) -> dict:
        """Cancel ONE live order (the maker-expiry prerequisite). Uses cancel_order(OrderPayload) per
        py-clob-client-v2. GATED like execute at the money-touching layer: geo → LIVE_TRADING_ENABLED
        (a cancel is a real signed authenticated exchange write, so it must never fire from a disabled bot).
        No PASS gate — cancelling REDUCES exposure, so it's the one live write we WANT available whenever the
        bot is live, independent of the maker-scope PASS that still blocks submission."""
        g = geo_gate()
        if not g.ok:
            raise RuntimeError(f"GEO BLOCK: egress {g.place} — {g.reason}. cancel refused.")
        if not self.enabled:
            raise RuntimeError("LIVE_TRADING_ENABLED=false — refusing to send a real cancel")
        client = self._connect()
        from py_clob_client_v2 import OrderPayload
        return client.cancel_order(OrderPayload(orderID=str(order_id)))
