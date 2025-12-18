from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

import ccxt  # type: ignore

from ai_trading_coach.config import AppConfig, require_credentials
from ai_trading_coach.domain.trade_lifecycle import ExecutionFill


def _ms_to_dt(ms: int | None) -> datetime:
    if not ms:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _to_decimal(x: Any) -> Decimal:
    if x is None:
        return Decimal("0")
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


@dataclass(frozen=True)
class BitgetReadonlyClient:
    """
    Read-only Bitget client (USDT perpetual) via ccxt.

    Guardrails:
    - We only expose fetch_* methods.
    - No order placement / modification / cancellation is implemented.
    """

    exchange: Any

    @classmethod
    def from_config(cls, cfg: AppConfig) -> "BitgetReadonlyClient":
        require_credentials(cfg)
        ex = ccxt.bitget(
            {
                "apiKey": cfg.bitget_api_key,
                "secret": cfg.bitget_api_secret,
                "password": cfg.bitget_api_password,
                "enableRateLimit": True,
                "options": {
                    # USDT-margined perpetual
                    "defaultType": "swap",
                },
            }
        )
        return cls(exchange=ex)

    def load_markets(self) -> None:
        self.exchange.load_markets()

    def discover_usdt_perp_symbols(self) -> list[str]:
        """
        Auto-detect USDT-margined perpetual swap symbols available on Bitget in ccxt format.
        """
        self.load_markets()
        out: list[str] = []
        for sym, m in (self.exchange.markets or {}).items():
            try:
                if not m.get("active", True):
                    continue
                if not m.get("swap", False):
                    continue
                # USDT perpetual (linear) typically
                if m.get("quote") != "USDT":
                    continue
                # Some ccxt schemas expose settle currency
                settle = m.get("settle")
                if settle and settle != "USDT":
                    continue
                out.append(sym)
            except Exception:
                continue
        # stable order for reproducibility
        out.sort()
        return out

    def fetch_my_trades(
        self,
        *,
        symbol: str,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[ExecutionFill]:
        since_ms = int(since.timestamp() * 1000) if since else None
        raw_trades = self.exchange.fetch_my_trades(symbol=symbol, since=since_ms, limit=limit)
        fills: list[ExecutionFill] = []
        for t in raw_trades:
            fee = t.get("fee") or {}
            info = t.get("info") or {}
            trade_side = "unknown"
            pos_mode = None
            profit = None
            if isinstance(info, dict):
                ts = info.get("tradeSide")
                if isinstance(ts, str):
                    tsl = ts.lower()
                    # Bitget sometimes returns values like "burst_close_long".
                    if "open" in tsl:
                        trade_side = "open"
                    elif "close" in tsl:
                        trade_side = "close"
                pm = info.get("posMode")
                if isinstance(pm, str):
                    pos_mode = pm
                # Bitget mix fills often contain profit per fill
                if "profit" in info:
                    profit = _to_decimal(info.get("profit"))
            fills.append(
                ExecutionFill(
                    ts=_ms_to_dt(t.get("timestamp")),
                    symbol=t.get("symbol") or symbol,
                    side=t.get("side"),
                    price=_to_decimal(t.get("price")),
                    amount=_to_decimal(t.get("amount")),
                    fee_cost=_to_decimal(fee.get("cost")),
                    fee_currency=fee.get("currency"),
                    maker_taker=(t.get("takerOrMaker") or "unknown"),
                    trade_side=trade_side,  # open/close if available
                    pos_mode=pos_mode,
                    reported_profit_usdt=profit,
                    exchange="bitget",
                    trade_id=str(t.get("id")) if t.get("id") is not None else None,
                    order_id=str(t.get("order")) if t.get("order") is not None else None,
                    raw=t,
                )
            )
        return fills

    def fetch_balance_usdt(self) -> dict[str, Any]:
        """
        Fetch current balance snapshot. Bitget does not guarantee historical available margin from this endpoint.
        We keep it as snapshot evidence at pull time.
        """
        return self.exchange.fetch_balance()

    def fetch_my_trades_paginated(
        self,
        *,
        symbol: str,
        since: datetime,
        limit: int = 500,
        max_pages: int = 40,
    ) -> tuple[list[ExecutionFill], list[str]]:
        """
        Fetch trades with a time cursor to avoid missing data.

        Strategy:
        - Call ccxt.fetch_my_trades(symbol, since_ms, limit)
        - Advance since_ms to (max_timestamp + 1ms)
        - Stop when:
          - returned list is empty
          - returned list size < limit (likely exhausted)
          - cursor does not advance (safety)
          - pages >= max_pages (safety)
        """
        warnings: list[str] = []
        out: list[ExecutionFill] = []

        cursor_ms = int(since.astimezone(timezone.utc).timestamp() * 1000)
        seen_ids: set[str] = set()

        for page in range(max_pages):
            fills = self.fetch_my_trades(
                symbol=symbol,
                since=datetime.fromtimestamp(cursor_ms / 1000.0, tz=timezone.utc),
                limit=limit,
            )
            if not fills:
                break

            # Deduplicate by trade_id when possible
            for f in fills:
                if f.trade_id:
                    if f.trade_id in seen_ids:
                        continue
                    seen_ids.add(f.trade_id)
                out.append(f)

            # Advance cursor
            max_ts = max(int(f.ts.timestamp() * 1000) for f in fills)
            next_cursor_ms = max_ts + 1
            if next_cursor_ms <= cursor_ms:
                warnings.append(
                    f"{symbol}: pagination cursor did not advance (cursor_ms={cursor_ms}, max_ts={max_ts})."
                )
                break
            cursor_ms = next_cursor_ms

            if len(fills) < limit:
                # Likely no more pages.
                break

        if max_pages and len(out) > 0 and len(out) % (limit * max_pages) == 0:
            warnings.append(f"{symbol}: reached max_pages={max_pages}. Data may be incomplete.")

        # Stable order
        out.sort(key=lambda f: f.ts)
        return out, warnings



