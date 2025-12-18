from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class PositionSide(str, Enum):
    long = "long"
    short = "short"
    unknown = "unknown"


class ContractKind(str, Enum):
    usdt_perp = "usdt_perp"


class ExecutionFill(BaseModel):
    """
    A single execution (成交). Trades (持仓生命周期) can have many fills due to scaling in/out.
    This is the core traceable evidence for a lifecycle.
    """

    ts: datetime = Field(..., description="Execution timestamp (exchange-reported if possible).")
    symbol: str = Field(..., description='e.g. "BTC/USDT:USDT"')
    side: Literal["buy", "sell"] = Field(..., description="Execution side on the exchange.")
    price: Decimal = Field(..., ge=0)
    amount: Decimal = Field(..., ge=0, description="Contract amount or base amount as returned by exchange.")

    fee_cost: Decimal = Field(default=Decimal("0"), description="Fee cost for this fill (USDT).")
    fee_currency: str | None = Field(default=None)
    maker_taker: Literal["maker", "taker", "unknown"] = "unknown"

    # Derivatives context (Bitget hedge mode etc.)
    trade_side: Literal["open", "close", "unknown"] = "unknown"
    pos_mode: str | None = None  # e.g. "hedge_mode"
    reported_profit_usdt: Decimal | None = None  # exchange-reported profit for this fill (if provided)
    hold_side: PositionSide = PositionSide.unknown  # long/short when exchange provides it

    # Traceability
    exchange: str = "bitget"
    trade_id: str | None = None
    order_id: str | None = None
    raw: dict[str, Any] | None = Field(default=None, description="Raw exchange payload (optional).")


class TradePlan(BaseModel):
    """
    Subjective / planned inputs used for behavioral review.
    Not used for backtesting.
    """

    thesis: str | None = Field(default=None, description="What problem was this trade trying to solve?")
    setup: str | None = Field(default=None, description="Setup name / pattern name (user-defined).")

    planned_entry: Decimal | None = Field(default=None, description="Planned entry price (if any).")
    planned_stop_loss: Decimal | None = Field(default=None, description="Planned stop loss price.")
    planned_take_profit: Decimal | None = Field(default=None, description="Planned take profit price.")
    planned_risk_usdt: Decimal | None = Field(
        default=None, description="Planned max loss in USDT (if explicitly defined)."
    )

    leverage: Decimal | None = Field(default=None, description="Planned leverage. If None, use actual observed.")
    max_position_notional_usdt: Decimal | None = Field(
        default=None, description="Planned max notional size cap (USDT)."
    )

    intended_holding: str | None = Field(default=None, description="e.g. 'scalp', 'intraday', 'swing'")
    rules: list[str] = Field(default_factory=list, description="Rules intended to follow.")

    note: str | None = Field(default=None, description="Trader's note at the time / after the trade.")


class TradeMetrics(BaseModel):
    """
    Derived metrics for review. All metrics must be explainable from fills + snapshots.
    """

    entry_ts: datetime | None = None
    exit_ts: datetime | None = None
    holding_seconds: int | None = None

    entry_avg_price: Decimal | None = None
    exit_avg_price: Decimal | None = None

    # Size / exposure (approximate, exchange-specific)
    max_abs_position_amount: Decimal | None = None
    max_abs_notional_usdt: Decimal | None = None

    # PnL and costs
    realized_pnl_usdt: Decimal | None = None
    realized_pnl_pct: Decimal | None = None  # relative to max_abs_notional_usdt
    realized_pnl_pct_of_available_margin: Decimal | None = None  # pnl / available_margin_at_entry * 100
    total_fees_usdt: Decimal | None = None
    total_funding_usdt: Decimal | None = None

    # Account snapshots for rule-based event detection (optional but required for some events)
    # Preferred: available margin balance at entry (可用保证金余额)
    available_margin_usdt_at_entry: Decimal | None = None
    # Backward-compat: total equity at entry (keep optional)
    equity_usdt_at_entry: Decimal | None = None

    # Behavior counts
    fills_count: int = 0
    adds_count: int = 0
    reductions_count: int = 0


class RiskMetrics(BaseModel):
    """
    Risk-focused metrics for review. These can start as None and be filled when market data is available.
    """

    planned_stop_loss: Decimal | None = None
    actual_stop_loss: Decimal | None = None

    initial_risk_usdt: Decimal | None = None  # based on planned_stop_loss if available
    r_multiple: Decimal | None = None  # realized_pnl_usdt / initial_risk_usdt

    mae_usdt: Decimal | None = None  # max adverse excursion during holding (requires OHLCV)
    mfe_usdt: Decimal | None = None  # max favorable excursion
    mae_pct: Decimal | None = None
    mfe_pct: Decimal | None = None


class TradeLifecycle(BaseModel):
    """
    One complete position lifecycle: from first open fill to final close fill.

    Design goals:
    - Supports multiple executions and scaling in/out.
    - Traceable: keep identifiers and (optional) raw payloads.
    - Review-oriented: includes plan, notes, discipline tags, and explainable metrics.
    - NOT a backtest structure: we don't model order book simulation or strategy logic.
    """

    # Identity
    lifecycle_id: str = Field(..., description="Stable ID for this lifecycle (generated by our app).")
    exchange: str = "bitget"
    contract_kind: ContractKind = ContractKind.usdt_perp
    symbol: str = Field(..., description='e.g. "BTC/USDT:USDT"')
    position_side: PositionSide = Field(..., description="Long/short direction of the lifecycle.")

    # Account / environment
    margin_currency: str = "USDT"
    leverage: Decimal | None = Field(default=None, description="Observed leverage (if available).")

    # Evidence
    fills: list[ExecutionFill] = Field(default_factory=list)
    funding_payments_usdt: list[tuple[datetime, Decimal]] = Field(
        default_factory=list, description="(ts, amount) in USDT; negative means paid, positive received."
    )

    # Subjective inputs for retrospective (optional)
    plan: TradePlan = Field(default_factory=TradePlan)

    # Review outputs (populated by analyzers later)
    emotion_tags: list[str] = Field(default_factory=list, description="e.g. ['FOMO', 'REVENGE']")
    discipline_violations: list[str] = Field(default_factory=list, description="Human-readable violation labels.")
    pattern_summary: str | None = Field(default=None, description="Short behavioral pattern summary.")

    # Derived numbers
    metrics: TradeMetrics = Field(default_factory=TradeMetrics)
    risk: RiskMetrics = Field(default_factory=RiskMetrics)

    # State
    status: Literal["open", "closed"] = "open"

    # Traceability hooks
    source_range: dict[str, Any] | None = Field(
        default=None,
        description="Extra pointers to source data used (e.g., first/last trade id, query window).",
    )

    def recompute(self) -> None:
        """
        Compute deterministic metrics from fills/funding only.
        Market-dependent metrics (MAE/MFE) should be computed elsewhere with OHLCV.
        """
        if not self.fills:
            self.metrics = TradeMetrics(fills_count=0)
            return

        fills_sorted = sorted(self.fills, key=lambda f: f.ts)

        entry_ts = fills_sorted[0].ts
        exit_ts = fills_sorted[-1].ts if self.status == "closed" else None
        holding_seconds = int((exit_ts - entry_ts).total_seconds()) if exit_ts else None

        total_fees = sum((f.fee_cost for f in fills_sorted), Decimal("0"))
        total_funding = sum((amt for _, amt in self.funding_payments_usdt), Decimal("0"))

        # Exposure approximations:
        notionals = [abs(f.amount * f.price) for f in fills_sorted]
        max_abs_notional = max(notionals) if notionals else None

        # Position tracking for max abs position & realized pnl:
        # - If Bitget provides reported_profit_usdt, prefer it for realized PnL (direction-safe).
        # - Otherwise fall back to a simple linear contract approximation (direction-dependent).
        pos = Decimal("0")
        max_abs_pos = Decimal("0")
        avg_entry = Decimal("0")
        realized = Decimal("0")
        realized_reported = Decimal("0")
        has_reported = False

        for f in fills_sorted:
            if f.reported_profit_usdt is not None:
                realized_reported += f.reported_profit_usdt
                has_reported = True
            qty = f.amount
            px = f.price
            delta = qty if f.side == "buy" else -qty
            prev_pos = pos

            # Same direction add
            if prev_pos == 0 or (prev_pos > 0 and delta > 0) or (prev_pos < 0 and delta < 0):
                new_pos = prev_pos + delta
                # update average entry price (for current position direction)
                if prev_pos == 0:
                    avg_entry = px
                else:
                    # weighted average by abs position
                    avg_entry = (avg_entry * abs(prev_pos) + px * abs(delta)) / abs(new_pos)
                pos = new_pos
            else:
                # Reducing or reversing
                # close_qty is the portion that offsets existing position
                close_qty = min(abs(prev_pos), abs(delta))
                # realized pnl for closed portion
                if prev_pos > 0 and delta < 0:
                    # closing long with sell
                    realized += (px - avg_entry) * close_qty
                elif prev_pos < 0 and delta > 0:
                    # closing short with buy
                    realized += (avg_entry - px) * close_qty

                # update position after close portion
                if prev_pos > 0:
                    pos = prev_pos - close_qty
                else:
                    pos = prev_pos + close_qty

                # remaining portion opens new reverse position (if any)
                remaining = abs(delta) - close_qty
                if remaining > 0:
                    # new position direction is sign of delta
                    pos = (Decimal("1") if delta > 0 else Decimal("-1")) * remaining
                    avg_entry = px  # new entry at this price for remaining

            max_abs_pos = max(max_abs_pos, abs(pos))

        max_abs_amount = max_abs_pos if max_abs_pos != 0 else None

        # Entry/exit avg price:
        # Prefer trade_side=open/close when available (Bitget mix fills).
        def wavg_price_by_trade_side(ts: Literal["open", "close"]) -> Decimal | None:
            px_qty = Decimal("0")
            qty = Decimal("0")
            for f in fills_sorted:
                if f.trade_side != ts:
                    continue
                px_qty += f.price * f.amount
                qty += f.amount
            if qty == 0:
                return None
            return px_qty / qty

        entry_avg = wavg_price_by_trade_side("open")
        exit_avg = wavg_price_by_trade_side("close") if self.status == "closed" else None

        realized_pnl = None
        realized_pct = None
        realized_pct_of_margin = None

        if self.status == "closed":
            base_realized = realized_reported if has_reported else realized
            realized_pnl = base_realized - total_fees + total_funding
            if max_abs_notional and max_abs_notional != 0:
                realized_pct = (realized_pnl / max_abs_notional) * Decimal("100")
            if self.metrics.available_margin_usdt_at_entry and self.metrics.available_margin_usdt_at_entry != 0:
                realized_pct_of_margin = (
                    realized_pnl / self.metrics.available_margin_usdt_at_entry * Decimal("100")
                )

        # Counts (heuristic)
        adds = 0
        reductions = 0
        for f in fills_sorted:
            if f.trade_side == "open":
                adds += 1
            elif f.trade_side == "close":
                reductions += 1
            else:
                # unknown - don't guess
                pass

        self.metrics = TradeMetrics(
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            holding_seconds=holding_seconds,
            entry_avg_price=entry_avg,
            exit_avg_price=exit_avg,
            max_abs_position_amount=max_abs_amount,
            max_abs_notional_usdt=max_abs_notional,
            realized_pnl_usdt=realized_pnl,
            realized_pnl_pct=realized_pct,
            realized_pnl_pct_of_available_margin=realized_pct_of_margin,
            total_fees_usdt=total_fees,
            total_funding_usdt=total_funding,
            fills_count=len(fills_sorted),
            adds_count=adds,
            reductions_count=reductions,
            available_margin_usdt_at_entry=self.metrics.available_margin_usdt_at_entry,
            equity_usdt_at_entry=self.metrics.equity_usdt_at_entry,
        )

        # Risk: tie planned stop into risk metrics for downstream analyzers
        self.risk.planned_stop_loss = self.plan.planned_stop_loss


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


