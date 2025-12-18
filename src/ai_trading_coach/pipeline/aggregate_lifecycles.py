from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from ai_trading_coach.domain.trade_lifecycle import ExecutionFill, PositionSide, TradeLifecycle


@dataclass(frozen=True)
class AggregationResult:
    lifecycles: list[TradeLifecycle]
    warnings: list[str]


def aggregate_fills_to_lifecycles(
    *,
    exchange: str,
    symbol: str,
    fills: Iterable[ExecutionFill],
) -> AggregationResult:
    """
    Deterministic aggregation (review-oriented, not a backtest engine).

    Direction handling:
    - If fills include hold_side (long/short), we aggregate two separate lifecycles per symbol:
      (symbol,long) and (symbol,short).
    - Else we fall back to a direction-unknown lifecycle (still avoids false "open" if tradeSide is correct).
    """
    fs = sorted(list(fills), key=lambda f: f.ts)
    lifecycles: list[TradeLifecycle] = []
    warnings: list[str] = []

    qty_by_side: dict[PositionSide, Decimal] = {PositionSide.long: Decimal("0"), PositionSide.short: Decimal("0"), PositionSide.unknown: Decimal("0")}
    current_by_side: dict[PositionSide, TradeLifecycle | None] = {PositionSide.long: None, PositionSide.short: None, PositionSide.unknown: None}

    def _start(ps: PositionSide, f: ExecutionFill) -> TradeLifecycle:
        return TradeLifecycle(
            lifecycle_id=f"{exchange}:{symbol}:{ps.value}:{f.ts.isoformat()}",
            exchange=exchange,
            symbol=symbol,
            position_side=ps,
            fills=[f],
            status="open",
        )

    def _append(ps: PositionSide, f: ExecutionFill, delta_qty: Decimal) -> None:
        prev = qty_by_side[ps]
        qty_by_side[ps] = qty_by_side[ps] + delta_qty
        cur = current_by_side[ps]
        if cur is None:
            if prev == 0 and delta_qty > 0 and qty_by_side[ps] > 0:
                current_by_side[ps] = _start(ps, f)
                return
            # If the first action we see is a close, it means the position was opened earlier
            # outside this dataset/window. Create a placeholder lifecycle and immediately close it.
            if delta_qty < 0:
                warnings.append(
                    f"Symbol {symbol} ({ps.value}): saw close before open at {f.ts.isoformat()} (window incomplete)."
                )
                tmp = _start(ps, f)
                tmp.status = "closed"
                tmp.recompute()
                lifecycles.append(tmp)
                current_by_side[ps] = None
                qty_by_side[ps] = Decimal("0")
                return

            warnings.append(f"Symbol {symbol} ({ps.value}): lifecycle started mid-position at {f.ts.isoformat()}.")
            current_by_side[ps] = _start(ps, f)
            return
        cur.fills.append(f)
        if qty_by_side[ps] <= 0:
            cur.status = "closed"
            cur.recompute()
            lifecycles.append(cur)
            current_by_side[ps] = None
            qty_by_side[ps] = Decimal("0")

    for f in fs:
        ts = getattr(f, "trade_side", "unknown")
        ps = getattr(f, "hold_side", PositionSide.unknown)
        if ts == "open":
            _append(ps, f, f.amount)
        elif ts == "close":
            _append(ps, f, -f.amount)
        else:
            warnings.append(f"Symbol {symbol}: missing tradeSide; cannot reliably aggregate (hedge_mode).")
            # Best-effort: treat as open to avoid hiding actions.
            _append(ps, f, f.amount)

    # If last lifecycle is still open, keep it (unfinished). It still matters for "actions today".
    for ps, cur in current_by_side.items():
        if cur is not None:
            cur.status = "open"
            cur.recompute()
            lifecycles.append(cur)

    return AggregationResult(lifecycles=lifecycles, warnings=warnings)


