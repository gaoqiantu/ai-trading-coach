from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ai_trading_coach.domain.trade_lifecycle import TradeLifecycle


@dataclass(frozen=True)
class SqliteStore:
    path: Path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS lifecycles (
                    lifecycle_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    position_side TEXT NOT NULL,
                    entry_ts TEXT,
                    exit_ts TEXT,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_lifecycles_entry_ts ON lifecycles(entry_ts);
                CREATE INDEX IF NOT EXISTS idx_lifecycles_exit_ts ON lifecycles(exit_ts);

                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY,
                    report_type TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    content_md TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_sides (
                    order_id TEXT PRIMARY KEY,
                    symbol_raw TEXT,
                    pos_side TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                -- Runtime config store (for platforms without env var UI).
                -- Values may include secrets; treat the sqlite file as sensitive.
                CREATE TABLE IF NOT EXISTS app_kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    is_secret INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def clear_lifecycles(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM lifecycles")
            conn.commit()

    def clear_caches(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM order_sides")
            conn.execute("DELETE FROM sync_state")
            conn.execute("DELETE FROM app_kv")
            conn.commit()

    def get_order_pos_side(self, *, order_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT pos_side FROM order_sides WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if not row:
            return None
        return row["pos_side"]

    def upsert_order_pos_side(self, *, order_id: str, symbol_raw: str, pos_side: str) -> None:
        now = datetime.utcnow().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO order_sides (order_id, symbol_raw, pos_side, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    symbol_raw=excluded.symbol_raw,
                    pos_side=excluded.pos_side,
                    updated_at=excluded.updated_at
                """,
                (order_id, symbol_raw, pos_side, now),
            )
            conn.commit()

    def get_state(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return row["value"]

    def set_state(self, key: str, value: str) -> None:
        now = datetime.utcnow().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (key, value, now),
            )
            conn.commit()

    def get_kv(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_kv WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return row["value"]

    def set_kv(self, *, key: str, value: str, is_secret: bool = False) -> None:
        now = datetime.utcnow().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_kv (key, value, is_secret, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    is_secret=excluded.is_secret,
                    updated_at=excluded.updated_at
                """,
                (key, value, 1 if is_secret else 0, now),
            )
            conn.commit()

    def upsert_lifecycle(self, lc: TradeLifecycle) -> None:
        lc.recompute()
        now = datetime.utcnow().isoformat()
        entry_ts = lc.metrics.entry_ts.isoformat() if lc.metrics.entry_ts else None
        exit_ts = lc.metrics.exit_ts.isoformat() if lc.metrics.exit_ts else None
        payload = lc.model_dump(mode="json")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO lifecycles (lifecycle_id, symbol, position_side, entry_ts, exit_ts, data_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lifecycle_id) DO UPDATE SET
                    symbol=excluded.symbol,
                    position_side=excluded.position_side,
                    entry_ts=excluded.entry_ts,
                    exit_ts=excluded.exit_ts,
                    data_json=excluded.data_json,
                    updated_at=excluded.updated_at
                """,
                (
                    lc.lifecycle_id,
                    lc.symbol,
                    lc.position_side.value,
                    entry_ts,
                    exit_ts,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()

    @staticmethod
    def _normalize_dt(dt: datetime) -> datetime:
        """
        Ensure timezone-aware datetimes for safe comparisons.
        If a datetime is naive, treat it as UTC.
        """
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def get_lifecycles_between(self, start_ts: datetime, end_ts: datetime) -> list[TradeLifecycle]:
        """
        Returns lifecycles whose entry_ts is within [start_ts, end_ts).
        Legacy helper (open-day view).
        Prefer `get_lifecycles_with_activity_between` for "any action today" semantics.
        """
        start_s = start_ts.isoformat()
        end_s = end_ts.isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT data_json
                FROM lifecycles
                WHERE entry_ts >= ? AND entry_ts < ?
                ORDER BY entry_ts ASC
                """,
                (start_s, end_s),
            ).fetchall()
        return [TradeLifecycle.model_validate(json.loads(r["data_json"])) for r in rows]

    def get_lifecycles_with_activity_between(self, start_ts: datetime, end_ts: datetime) -> list[TradeLifecycle]:
        """
        "Any action today" semantics:
        Returns lifecycles that have ANY execution fill timestamp within [start_ts, end_ts).

        Implementation:
        - First, query lifecycles whose (entry_ts, exit_ts) overlaps the window to narrow scan.
        - Then, parse JSON and filter by fills timestamps deterministically.
        """
        start_ts_n = self._normalize_dt(start_ts)
        end_ts_n = self._normalize_dt(end_ts)
        start_s = start_ts.isoformat()
        end_s = end_ts.isoformat()

        # Overlap condition:
        # entry_ts < end AND (exit_ts IS NULL OR exit_ts >= start)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT data_json
                FROM lifecycles
                WHERE (entry_ts IS NULL OR entry_ts < ?)
                  AND (exit_ts IS NULL OR exit_ts >= ?)
                ORDER BY entry_ts ASC
                """,
                (end_s, start_s),
            ).fetchall()

        out: list[TradeLifecycle] = []
        for r in rows:
            lc = TradeLifecycle.model_validate(json.loads(r["data_json"]))
            for f in lc.fills:
                fts = self._normalize_dt(f.ts)
                if start_ts_n <= fts < end_ts_n:
                    out.append(lc)
                    break
        return out

    def save_report(
        self,
        *,
        report_id: str,
        report_type: str,
        period_start: datetime,
        period_end: datetime,
        content_md: str,
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO reports (report_id, report_type, period_start, period_end, content_md, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                    report_type=excluded.report_type,
                    period_start=excluded.period_start,
                    period_end=excluded.period_end,
                    content_md=excluded.content_md,
                    created_at=excluded.created_at
                """,
                (
                    report_id,
                    report_type,
                    period_start.isoformat(),
                    period_end.isoformat(),
                    content_md,
                    now,
                ),
            )
            conn.commit()


