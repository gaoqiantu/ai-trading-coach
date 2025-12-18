from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Literal

try:
    # Python 3.9+: zoneinfo
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from pydantic import BaseModel, Field

from ai_trading_coach.domain.trade_lifecycle import (
    ExecutionFill,
    PositionSide,
    TradeLifecycle,
)


class EventLevel(str, Enum):
    """
    P0: 必须复盘（严重违规/高风险）
    P1: 建议复盘（明显问题/潜在风险）
    P2: 可选复盘（信息性事件/轻微偏离）
    """

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class TradeEventType(str, Enum):
    # Lifecycle markers
    open_completed = "open_completed"
    close_completed = "close_completed"

    # Risk / discipline
    stop_loss_triggered = "stop_loss_triggered"
    big_loss_pct_equity = "big_loss_pct_equity"
    consecutive_losses = "consecutive_losses"
    high_leverage_used = "high_leverage_used"
    night_trading_us_eastern = "night_trading_us_eastern"


class EvidenceFillRef(BaseModel):
    ts: datetime
    side: Literal["buy", "sell"]
    price: Decimal
    amount: Decimal
    trade_id: str | None = None
    order_id: str | None = None


class TradeEvent(BaseModel):
    """
    Structured, deterministic event output.
    No fuzzy judgments: every event includes thresholds and the exact values compared.
    """

    event_id: str = Field(..., description="Stable event id: <lifecycle_id>:<event_type>[:<index>]")
    event_type: TradeEventType
    level: EventLevel

    # Chinese definition & logic for auditability
    name_zh: str
    definition_zh: str
    logic_zh: str

    # Scope
    lifecycle_id: str
    symbol: str
    position_side: PositionSide

    # When did the event happen (deterministic)
    occurred_at: datetime

    # Evidence payload: all inputs used for decision
    evidence: dict[str, Any]


EVENT_CATALOG_ZH: dict[TradeEventType, dict[str, str]] = {
    TradeEventType.open_completed: {
        "name_zh": "开仓完成",
        "definition_zh": "一笔持仓生命周期中，首次出现使持仓从 0 变为非 0 的成交，视为开仓完成。",
        "logic_zh": "取该生命周期 fills 按时间排序后的第一笔成交作为开仓证据。",
    },
    TradeEventType.close_completed: {
        "name_zh": "平仓完成",
        "definition_zh": "一笔持仓生命周期中，最后一次使持仓回到 0 的成交，视为平仓完成。",
        "logic_zh": "取该生命周期 fills 按时间排序后的最后一笔成交作为平仓证据。",
    },
    TradeEventType.stop_loss_triggered: {
        "name_zh": "止损触发（基于计划止损价）",
        "definition_zh": "当你提供了计划止损价后，若出现与减仓/平仓方向一致的成交价格触及或穿越计划止损价，则判定止损触发。",
        "logic_zh": "若 position_side=long：存在卖出成交 price <= planned_stop_loss；若 position_side=short：存在买入成交 price >= planned_stop_loss。证据包含触发成交与计划止损价。",
    },
    TradeEventType.big_loss_pct_equity: {
        "name_zh": "单笔大亏（按可用保证金余额比例）",
        "definition_zh": "若单笔交易的已实现亏损绝对值超过入场时可用保证金余额的一定比例（默认 5%），判定为单笔大亏。",
        "logic_zh": "优先使用 metrics.available_margin_usdt_at_entry（若缺失则回退 metrics.equity_usdt_at_entry）。若 realized_pnl_usdt < 0 且 abs(realized_pnl_usdt) / base_balance * 100 >= threshold_pct，则触发。证据包含 pnl、base_balance、阈值与计算结果。",
    },
    TradeEventType.consecutive_losses: {
        "name_zh": "连续亏损",
        "definition_zh": "在一段连续的已平仓交易中，连续出现 N 笔亏损（默认 N=3），判定为连续亏损事件。",
        "logic_zh": "按 exit_ts 升序，对每个已平仓 lifecycle 取 realized_pnl_usdt。若最近 N 笔均 < 0，则触发；证据包含这 N 笔的 lifecycle_id、pnl 与时间。",
    },
    TradeEventType.high_leverage_used: {
        "name_zh": "实际高杠杆使用（按有效杠杆）",
        "definition_zh": "若有效杠杆（最大名义仓位/入场时可用保证金余额）超过阈值（默认 10x），判定为实际高杠杆使用。",
        "logic_zh": "需要 metrics.max_abs_notional_usdt 与 metrics.available_margin_usdt_at_entry（若缺失则回退 metrics.equity_usdt_at_entry）。effective_leverage = max_abs_notional_usdt / base_balance。若 >= threshold，则触发。证据包含 notional、base_balance、effective_leverage、阈值。",
    },
    TradeEventType.night_trading_us_eastern: {
        "name_zh": "夜间交易（美国东部时间）",
        "definition_zh": "若开仓时间落在美国东部时间夜盘窗口（默认 22:00-次日06:00，跨天），判定为夜间交易。",
        "logic_zh": "将 entry_ts 转换为 America/New_York 时区。若 local_time >= 22:00 或 local_time <= 06:00（跨天窗口，包含边界 06:00），则触发。证据包含转换后的时间与窗口边界。",
    },
}


def _catalog(t: TradeEventType) -> dict[str, str]:
    return EVENT_CATALOG_ZH[t]


def _fills_sorted(lc: TradeLifecycle) -> list[ExecutionFill]:
    return sorted(lc.fills, key=lambda f: f.ts)


def _fill_ref(f: ExecutionFill) -> EvidenceFillRef:
    return EvidenceFillRef(
        ts=f.ts, side=f.side, price=f.price, amount=f.amount, trade_id=f.trade_id, order_id=f.order_id
    )


def detect_events_for_lifecycle(
    lc: TradeLifecycle,
    *,
    big_loss_threshold_pct_equity: Decimal = Decimal("5"),
    consecutive_losses_n: int = 3,
    high_leverage_threshold: Decimal = Decimal("10"),
    us_eastern_night_window: tuple[str, str] = ("22:00", "06:00"),
) -> list[TradeEvent]:
    """
    Detect events using ONLY completed (historical) lifecycle data.
    Deterministic: emits an event only if required inputs exist.
    """
    events: list[TradeEvent] = []
    if not lc.fills:
        return events

    lc.recompute()
    fills = _fills_sorted(lc)

    # Open completed (P2 informational)
    first = fills[0]
    c = _catalog(TradeEventType.open_completed)
    events.append(
        TradeEvent(
            event_id=f"{lc.lifecycle_id}:{TradeEventType.open_completed.value}",
            event_type=TradeEventType.open_completed,
            level=EventLevel.P2,
            name_zh=c["name_zh"],
            definition_zh=c["definition_zh"],
            logic_zh=c["logic_zh"],
            lifecycle_id=lc.lifecycle_id,
            symbol=lc.symbol,
            position_side=lc.position_side,
            occurred_at=first.ts,
            evidence={
                "fill": _fill_ref(first).model_dump(),
            },
        )
    )

    # Close completed (P2 informational) - only when lifecycle is actually closed
    last = fills[-1]
    if lc.status == "closed":
        c = _catalog(TradeEventType.close_completed)
        events.append(
            TradeEvent(
                event_id=f"{lc.lifecycle_id}:{TradeEventType.close_completed.value}",
                event_type=TradeEventType.close_completed,
                level=EventLevel.P2,
                name_zh=c["name_zh"],
                definition_zh=c["definition_zh"],
                logic_zh=c["logic_zh"],
                lifecycle_id=lc.lifecycle_id,
                symbol=lc.symbol,
                position_side=lc.position_side,
                occurred_at=last.ts,
                evidence={
                    "fill": _fill_ref(last).model_dump(),
                },
            )
        )

    # Stop loss triggered (requires planned stop)
    planned_sl = lc.plan.planned_stop_loss
    if planned_sl is not None:
        entry_side: Literal["buy", "sell"] = "buy" if lc.position_side == PositionSide.long else "sell"
        exit_side: Literal["buy", "sell"] = "sell" if entry_side == "buy" else "buy"
        trigger_fill: ExecutionFill | None = None
        for f in fills:
            if f.side != exit_side:
                continue
            if lc.position_side == PositionSide.long and f.price <= planned_sl:
                trigger_fill = f
                break
            if lc.position_side == PositionSide.short and f.price >= planned_sl:
                trigger_fill = f
                break

        if trigger_fill is not None:
            c = _catalog(TradeEventType.stop_loss_triggered)
            events.append(
                TradeEvent(
                    event_id=f"{lc.lifecycle_id}:{TradeEventType.stop_loss_triggered.value}",
                    event_type=TradeEventType.stop_loss_triggered,
                    level=EventLevel.P0,
                    name_zh=c["name_zh"],
                    definition_zh=c["definition_zh"],
                    logic_zh=c["logic_zh"],
                    lifecycle_id=lc.lifecycle_id,
                    symbol=lc.symbol,
                    position_side=lc.position_side,
                    occurred_at=trigger_fill.ts,
                    evidence={
                        "planned_stop_loss": str(planned_sl),
                        "trigger_fill": _fill_ref(trigger_fill).model_dump(),
                        "comparison": (
                            "price <= planned_stop_loss"
                            if lc.position_side == PositionSide.long
                            else "price >= planned_stop_loss"
                        ),
                    },
                )
            )

    # Big loss (requires equity snapshot and pnl)
    pnl = lc.metrics.realized_pnl_usdt
    base_balance = lc.metrics.available_margin_usdt_at_entry or lc.metrics.equity_usdt_at_entry
    base_balance_source = (
        "available_margin_usdt_at_entry"
        if lc.metrics.available_margin_usdt_at_entry is not None
        else ("equity_usdt_at_entry" if lc.metrics.equity_usdt_at_entry is not None else None)
    )
    if pnl is not None and base_balance is not None and base_balance > 0:
        loss_pct = (abs(pnl) / base_balance) * Decimal("100")
        if pnl < 0 and loss_pct >= big_loss_threshold_pct_equity:
            c = _catalog(TradeEventType.big_loss_pct_equity)
            events.append(
                TradeEvent(
                    event_id=f"{lc.lifecycle_id}:{TradeEventType.big_loss_pct_equity.value}",
                    event_type=TradeEventType.big_loss_pct_equity,
                    level=EventLevel.P0,
                    name_zh=c["name_zh"],
                    definition_zh=c["definition_zh"],
                    logic_zh=c["logic_zh"],
                    lifecycle_id=lc.lifecycle_id,
                    symbol=lc.symbol,
                    position_side=lc.position_side,
                    occurred_at=lc.metrics.exit_ts or last.ts,
                    evidence={
                        "realized_pnl_usdt": str(pnl),
                        "base_balance_usdt_at_entry": str(base_balance),
                        "base_balance_source": base_balance_source,
                        "loss_pct_of_base_balance": str(loss_pct),
                        "threshold_pct": str(big_loss_threshold_pct_equity),
                        "comparison": "loss_pct_of_base_balance >= threshold_pct AND realized_pnl_usdt < 0",
                    },
                )
            )

    # High leverage used (requires equity snapshot and max notional)
    notional = lc.metrics.max_abs_notional_usdt
    if notional is not None and base_balance is not None and base_balance > 0:
        effective_lev = notional / base_balance
        if effective_lev >= high_leverage_threshold:
            c = _catalog(TradeEventType.high_leverage_used)
            events.append(
                TradeEvent(
                    event_id=f"{lc.lifecycle_id}:{TradeEventType.high_leverage_used.value}",
                    event_type=TradeEventType.high_leverage_used,
                    level=EventLevel.P1,
                    name_zh=c["name_zh"],
                    definition_zh=c["definition_zh"],
                    logic_zh=c["logic_zh"],
                    lifecycle_id=lc.lifecycle_id,
                    symbol=lc.symbol,
                    position_side=lc.position_side,
                    occurred_at=lc.metrics.entry_ts or first.ts,
                    evidence={
                        "max_abs_notional_usdt": str(notional),
                        "base_balance_usdt_at_entry": str(base_balance),
                        "base_balance_source": base_balance_source,
                        "effective_leverage": str(effective_lev),
                        "threshold": str(high_leverage_threshold),
                        "comparison": "effective_leverage >= threshold",
                    },
                )
            )

    # Night trading US Eastern (requires timezone conversion)
    entry_ts = lc.metrics.entry_ts or first.ts
    if ZoneInfo is not None:
        try:
            eastern = ZoneInfo("America/New_York")
            entry_eastern = entry_ts.astimezone(eastern)
            start_s, end_s = us_eastern_night_window
            start_h, start_m = (int(x) for x in start_s.split(":"))
            end_h, end_m = (int(x) for x in end_s.split(":"))
            local_h = entry_eastern.hour
            local_m = entry_eastern.minute

            # Cross-day window: [22:00, 24:00) U [00:00, 06:00]
            in_late = (local_h > start_h) or (local_h == start_h and local_m >= start_m)
            in_early = (local_h < end_h) or (local_h == end_h and local_m <= end_m)
            if in_late or in_early:
                c = _catalog(TradeEventType.night_trading_us_eastern)
                events.append(
                    TradeEvent(
                        event_id=f"{lc.lifecycle_id}:{TradeEventType.night_trading_us_eastern.value}",
                        event_type=TradeEventType.night_trading_us_eastern,
                        level=EventLevel.P1,
                        name_zh=c["name_zh"],
                        definition_zh=c["definition_zh"],
                        logic_zh=c["logic_zh"],
                        lifecycle_id=lc.lifecycle_id,
                        symbol=lc.symbol,
                        position_side=lc.position_side,
                        occurred_at=entry_ts,
                        evidence={
                            "entry_ts_utc": entry_ts.isoformat(),
                            "entry_ts_us_eastern": entry_eastern.isoformat(),
                            "night_window_local": {"start": start_s, "end": end_s, "timezone": "America/New_York"},
                            "comparison": "local_time >= start OR local_time <= end (cross-day window, inclusive end)",
                        },
                    )
                )
        except Exception:
            # deterministic behavior: if timezone conversion fails, we emit no event.
            pass

    # NOTE: consecutive losses is a cross-lifecycle detector; implemented in batch API below.
    return events


def detect_events_for_lifecycles(
    lifecycles: Iterable[TradeLifecycle],
    *,
    big_loss_threshold_pct_equity: Decimal = Decimal("5"),
    consecutive_losses_n: int = 3,
    high_leverage_threshold: Decimal = Decimal("10"),
    us_eastern_night_window: tuple[str, str] = ("22:00", "06:00"),
) -> list[TradeEvent]:
    """
    Batch detector that also emits cross-trade events (e.g. consecutive losses).
    """
    lcs = list(lifecycles)
    for lc in lcs:
        lc.recompute()

    # Per-lifecycle events
    out: list[TradeEvent] = []
    for lc in lcs:
        out.extend(
            detect_events_for_lifecycle(
                lc,
                big_loss_threshold_pct_equity=big_loss_threshold_pct_equity,
                consecutive_losses_n=consecutive_losses_n,
                high_leverage_threshold=high_leverage_threshold,
                us_eastern_night_window=us_eastern_night_window,
            )
        )

    # Cross-lifecycle: consecutive losses
    closed = [lc for lc in lcs if lc.metrics.exit_ts is not None and lc.metrics.realized_pnl_usdt is not None]
    closed_sorted = sorted(closed, key=lambda x: x.metrics.exit_ts)  # type: ignore[arg-type]

    if consecutive_losses_n >= 2 and len(closed_sorted) >= consecutive_losses_n:
        window = closed_sorted[-consecutive_losses_n:]
        if all((w.metrics.realized_pnl_usdt or Decimal("0")) < 0 for w in window):
            last = window[-1]
            c = _catalog(TradeEventType.consecutive_losses)
            out.append(
                TradeEvent(
                    event_id=f"{last.lifecycle_id}:{TradeEventType.consecutive_losses.value}:{consecutive_losses_n}",
                    event_type=TradeEventType.consecutive_losses,
                    level=EventLevel.P0,
                    name_zh=c["name_zh"],
                    definition_zh=c["definition_zh"],
                    logic_zh=c["logic_zh"],
                    lifecycle_id=last.lifecycle_id,
                    symbol=last.symbol,
                    position_side=last.position_side,
                    occurred_at=last.metrics.exit_ts,  # type: ignore[arg-type]
                    evidence={
                        "n": consecutive_losses_n,
                        "window": [
                            {
                                "lifecycle_id": w.lifecycle_id,
                                "symbol": w.symbol,
                                "position_side": w.position_side.value,
                                "exit_ts": (w.metrics.exit_ts.isoformat() if w.metrics.exit_ts else None),
                                "realized_pnl_usdt": str(w.metrics.realized_pnl_usdt),
                            }
                            for w in window
                        ],
                        "comparison": "last_n_realized_pnl_usdt_all < 0",
                    },
                )
            )

    return out


