from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ai_trading_coach.config import AppConfig, load_config, normalize_symbols
from ai_trading_coach.integrations.bitget_readonly import BitgetReadonlyClient
from ai_trading_coach.integrations.bitget_rest import BitgetRestClient, fetch_fills_windowed
from ai_trading_coach.pipeline.aggregate_lifecycles import aggregate_fills_to_lifecycles
from ai_trading_coach.storage.sqlite_store import SqliteStore
from ai_trading_coach.domain.trade_lifecycle import PositionSide


@dataclass(frozen=True)
class SyncResult:
    lifecycles_upserted: int
    warnings: list[str]


def sync_bitget_trades_to_sqlite(
    *,
    cfg: AppConfig,
    since: datetime,
    per_call_limit: int = 500,
    max_pages_per_symbol: int = 40,
    stop_after_lifecycles: int = 0,
) -> SyncResult:
    """
    Read-only sync:
    - fetch my trades per symbol since `since`
    - aggregate fills into TradeLifecycle objects
    - upsert lifecycles into sqlite
    """
    symbols = normalize_symbols(cfg.symbols)

    store = SqliteStore(cfg.sqlite_path)
    store.ensure_schema()

    client = BitgetReadonlyClient.from_config(cfg)
    client.load_markets()

    use_rest = int(__import__("os").getenv("BITGET_USE_REST_FILLS", "1")) == 1
    product_type = __import__("os").getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")

    # Best-effort available margin snapshot (current). Used only when we don't have a better at-entry value.
    available_margin_usdt: float | None = None
    try:
        bal = client.fetch_balance_usdt()
        # ccxt balance shapes vary; try common paths
        usdt = None
        if isinstance(bal, dict):
            usdt = (bal.get("USDT") or {}).get("free")
            if usdt is None:
                usdt = (bal.get("free") or {}).get("USDT")
        if usdt is not None:
            available_margin_usdt = float(usdt)
    except Exception:
        available_margin_usdt = None

    # If user doesn't provide SYMBOLS, auto-detect all USDT perpetual markets.
    if not symbols:
        symbols = client.discover_usdt_perp_symbols()
        if cfg.max_symbols and cfg.max_symbols > 0:
            symbols = symbols[: cfg.max_symbols]
        if not symbols:
            raise RuntimeError("Auto-detect returned 0 symbols. Provide SYMBOLS manually.")

    warnings: list[str] = []
    upserted = 0

    if use_rest:
        import os

        # REST fills: need order detail to map to posSide (long/short).
        rest = BitgetRestClient.from_config(cfg)

        # Incremental sync to avoid "卡死" backfills on every run:
        # If we have last sync timestamp, start from there minus a small buffer.
        end = datetime.now(timezone.utc)
        last_ms = store.get_state("bitget_rest_last_sync_ms")
        if last_ms and last_ms.isdigit():
            last_dt = datetime.fromtimestamp(int(last_ms) / 1000.0, tz=timezone.utc)
            # 2h buffer for late-arriving fills
            since_eff = min(end, last_dt - timedelta(hours=2))
            if since_eff > since:
                since = since_eff

        window_days = int(os.getenv("BITGET_REST_WINDOW_DAYS", "2"))
        page_limit = int(os.getenv("BITGET_REST_PAGE_LIMIT", "100"))
        max_pages = int(os.getenv("BITGET_REST_MAX_PAGES_PER_WINDOW", "10"))

        fills = fetch_fills_windowed(
            rest,
            product_type=product_type,
            start=since,
            end=end,
            window_days=window_days,
            page_limit=page_limit,
            max_pages_per_window=max_pages,
        )

        # Enrich fills with posSide (long/short) via order detail; persistent cache in SQLite.
        for f in fills:
            if not f.order_id or not isinstance(f.raw, dict):
                continue
            sym_raw = str(f.raw.get("symbol") or "")
            if not sym_raw:
                continue

            ps_cached = store.get_order_pos_side(order_id=f.order_id)
            if ps_cached in ("long", "short"):
                f.hold_side = PositionSide(ps_cached)
                continue

            try:
                od = rest.fetch_mix_order_detail(product_type=product_type, symbol_raw=sym_raw, order_id=f.order_id)
                ps = str(od.get("posSide") or "").lower()
                if ps in ("long", "short"):
                    store.upsert_order_pos_side(order_id=f.order_id, symbol_raw=sym_raw, pos_side=ps)
                    f.hold_side = PositionSide(ps)
            except Exception:
                continue

        # group by symbol
        by_symbol: dict[str, list] = {}
        for f in fills:
            by_symbol.setdefault(f.symbol, []).append(f)

        for sym, sfills in by_symbol.items():
            agg = aggregate_fills_to_lifecycles(exchange="bitget", symbol=sym, fills=sfills)
            warnings.extend(agg.warnings)
            for lc in agg.lifecycles:
                if lc.metrics.available_margin_usdt_at_entry is None and available_margin_usdt is not None:
                    from decimal import Decimal

                    lc.metrics.available_margin_usdt_at_entry = Decimal(str(available_margin_usdt))
                store.upsert_lifecycle(lc)
                upserted += 1
                if stop_after_lifecycles and upserted >= stop_after_lifecycles:
                    return SyncResult(lifecycles_upserted=upserted, warnings=warnings)

        # record last successful sync timestamp
        store.set_state("bitget_rest_last_sync_ms", str(int(end.timestamp() * 1000)))
    else:
        for sym in symbols:
            fills, w2 = client.fetch_my_trades_paginated(
                symbol=sym,
                since=since,
                limit=per_call_limit,
                max_pages=max_pages_per_symbol,
            )
            warnings.extend(w2)
            agg = aggregate_fills_to_lifecycles(exchange="bitget", symbol=sym, fills=fills)
            warnings.extend(agg.warnings)
            for lc in agg.lifecycles:
                # Populate margin snapshot if missing (best-effort).
                if lc.metrics.available_margin_usdt_at_entry is None and available_margin_usdt is not None:
                    from decimal import Decimal

                    lc.metrics.available_margin_usdt_at_entry = Decimal(str(available_margin_usdt))
                store.upsert_lifecycle(lc)
                upserted += 1
                if stop_after_lifecycles and upserted >= stop_after_lifecycles:
                    return SyncResult(lifecycles_upserted=upserted, warnings=warnings)

    return SyncResult(lifecycles_upserted=upserted, warnings=warnings)


def main() -> None:
    cfg = load_config()
    cfg.ensure_dirs()
    lookback_days = int(__import__("os").getenv("SYNC_LOOKBACK_DAYS", "7"))
    per_call_limit = int(__import__("os").getenv("SYNC_LIMIT", "500"))
    max_pages = int(__import__("os").getenv("SYNC_MAX_PAGES", "40"))
    stop_after = int(__import__("os").getenv("SYNC_STOP_AFTER_LIFECYCLES", "0"))
    reset = int(__import__("os").getenv("SYNC_RESET", "0"))

    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    if reset:
        store = SqliteStore(cfg.sqlite_path)
        store.ensure_schema()
        store.clear_lifecycles()
        store.clear_caches()
        print("reset=1: cleared lifecycles table")

    res = sync_bitget_trades_to_sqlite(
        cfg=cfg,
        since=since,
        per_call_limit=per_call_limit,
        max_pages_per_symbol=max_pages,
        stop_after_lifecycles=stop_after,
    )
    print(f"upserted_lifecycles={res.lifecycles_upserted}")
    if res.warnings:
        print("warnings:")
        for w in res.warnings:
            print(f"- {w}")


if __name__ == "__main__":
    main()


