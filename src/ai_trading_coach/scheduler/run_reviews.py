from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from ai_trading_coach.config import AppConfig
from ai_trading_coach.discord_webhook import DiscordWebhook
from ai_trading_coach.pipeline.sync_bitget import sync_bitget_trades_to_sqlite
from ai_trading_coach.reports.discord_preview import make_review_preview
from ai_trading_coach.reports.generator import (
    compute_discipline_score,
    generate_daily_report_md,
    generate_periodic_report_md,
)
from ai_trading_coach.storage.sqlite_store import SqliteStore


def _parse_hhmm(s: str) -> tuple[int, int]:
    hh, mm = s.split(":")
    return int(hh), int(mm)


def _start_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _is_last_day_of_month(local_dt: datetime) -> bool:
    next_day = (local_dt + timedelta(days=1)).date()
    return next_day.day == 1


@dataclass(frozen=True)
class ReviewRunner:
    cfg: AppConfig

    def _tz(self):
        if ZoneInfo is None:
            raise RuntimeError("zoneinfo not available; need Python 3.9+")
        return ZoneInfo(self.cfg.timezone)

    def _store(self) -> SqliteStore:
        store = SqliteStore(self.cfg.sqlite_path)
        store.ensure_schema()
        return store

    def _discord(self) -> DiscordWebhook:
        return DiscordWebhook(url=self.cfg.discord_webhook_url, username=self.cfg.discord_username)

    def run_daily(self, now: datetime | None = None) -> str:
        tz = self._tz()
        now_local = (now or datetime.now(tz)).astimezone(tz)
        period_start = _start_of_day(now_local)
        period_end = now_local

        # Sync before reviewing (read-only).
        # IMPORTANT: daily review needs enough lookback to include trades opened earlier but closed today.
        # Use SYNC_LOOKBACK_DAYS if set; otherwise default 90 (Bitget common history window).
        import os

        lookback_days = int(os.getenv("SYNC_LOOKBACK_DAYS", "90"))
        sync_since_utc = (now_local - timedelta(days=lookback_days)).astimezone(timezone.utc)
        try:
            sync_bitget_trades_to_sqlite(cfg=self.cfg, since=sync_since_utc)
        except Exception:
            # Deterministic behavior: if sync fails, we still generate a report from existing DB.
            pass

        store = self._store()
        lifecycles = store.get_lifecycles_with_activity_between(period_start, period_end)
        md = generate_daily_report_md(period_start=period_start, period_end=period_end, lifecycles=lifecycles)

        # Short discord preview: title + stats + top issues (with event_id pointers)
        from ai_trading_coach.analysis.events import detect_events_for_lifecycles

        events = detect_events_for_lifecycles(lifecycles)
        score = compute_discipline_score(events)
        preview = make_review_preview(
            kind_zh="每日复盘",
            date_label=period_start.date().isoformat(),
            events=events,
            discipline_score=score.score,
        ).render()

        report_id = f"daily:{period_start.date().isoformat()}"
        store.save_report(
            report_id=report_id,
            report_type="daily",
            period_start=period_start,
            period_end=period_end,
            content_md=md,
        )

        if self.cfg.discord_webhook_url:
            self._discord().send_markdown_file(
                filename=f"daily-{period_start.date().isoformat()}.md",
                content_md=md,
                content_preview=preview,
            )
        return md

    def run_weekly(self, now: datetime | None = None) -> str:
        tz = self._tz()
        now_local = (now or datetime.now(tz)).astimezone(tz)
        period_end = now_local
        period_start = now_local - timedelta(days=7)

        # Pre-sync (read-only) for the review window.
        sync_since_utc = (period_start - timedelta(hours=2)).astimezone(timezone.utc)
        try:
            sync_bitget_trades_to_sqlite(cfg=self.cfg, since=sync_since_utc)
        except Exception:
            pass

        store = self._store()
        lifecycles = store.get_lifecycles_with_activity_between(period_start, period_end)
        md = generate_periodic_report_md(
            title_zh="每周复盘（周六23:00触发）",
            period_start=period_start,
            period_end=period_end,
            lifecycles=lifecycles,
        )

        from ai_trading_coach.analysis.events import detect_events_for_lifecycles

        events = detect_events_for_lifecycles(lifecycles)
        score = compute_discipline_score(events)
        preview = make_review_preview(
            kind_zh="每周复盘",
            date_label=f"{period_start.date().isoformat()}~{period_end.date().isoformat()}",
            events=events,
            discipline_score=score.score,
        ).render()

        report_id = f"weekly:{period_end.date().isoformat()}"
        store.save_report(
            report_id=report_id,
            report_type="weekly",
            period_start=period_start,
            period_end=period_end,
            content_md=md,
        )

        if self.cfg.discord_webhook_url:
            self._discord().send_markdown_file(
                filename=f"weekly-{period_end.date().isoformat()}.md",
                content_md=md,
                content_preview=preview,
            )
        return md

    def run_monthly_if_last_day(self, now: datetime | None = None) -> str | None:
        tz = self._tz()
        now_local = (now or datetime.now(tz)).astimezone(tz)
        if not _is_last_day_of_month(now_local):
            return None

        # Month window: from first day of this month 00:00 to now
        period_end = now_local
        period_start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        sync_since_utc = (period_start - timedelta(hours=2)).astimezone(timezone.utc)
        try:
            sync_bitget_trades_to_sqlite(cfg=self.cfg, since=sync_since_utc)
        except Exception:
            pass

        store = self._store()
        lifecycles = store.get_lifecycles_with_activity_between(period_start, period_end)
        md = generate_periodic_report_md(
            title_zh="每月复盘（当月最后一天23:00触发）",
            period_start=period_start,
            period_end=period_end,
            lifecycles=lifecycles,
        )

        from ai_trading_coach.analysis.events import detect_events_for_lifecycles

        events = detect_events_for_lifecycles(lifecycles)
        score = compute_discipline_score(events)
        preview = make_review_preview(
            kind_zh="每月复盘",
            date_label=f"{period_start.date().isoformat()}~{period_end.date().isoformat()}",
            events=events,
            discipline_score=score.score,
        ).render()

        report_id = f"monthly:{period_end.date().isoformat()}"
        store.save_report(
            report_id=report_id,
            report_type="monthly",
            period_start=period_start,
            period_end=period_end,
            content_md=md,
        )

        if self.cfg.discord_webhook_url:
            self._discord().send_markdown_file(
                filename=f"monthly-{period_end.date().isoformat()}.md",
                content_md=md,
                content_preview=preview,
            )
        return md


