from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ai_trading_coach.config import load_config
from ai_trading_coach.scheduler.run_reviews import ReviewRunner


def _parse_hhmm(s: str) -> tuple[int, int]:
    hh, mm = s.split(":")
    return int(hh), int(mm)


def configure_jobs(*, sched, runner: ReviewRunner, timezone: str) -> None:
    """
    Register daily/weekly/monthly review jobs onto a scheduler instance.
    Works for both BlockingScheduler and BackgroundScheduler.
    """
    cfg = runner.cfg
    hh, mm = _parse_hhmm(cfg.daily_at)
    wh, wm = _parse_hhmm(cfg.weekly_at)
    mh, mm2 = _parse_hhmm(cfg.monthly_at)

    # Daily review (US/Eastern 23:00)
    sched.add_job(
        func=lambda: runner.run_daily(),
        trigger=CronTrigger(hour=hh, minute=mm, timezone=timezone),
        id="daily_review",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # Weekly review (Saturday 23:00)
    sched.add_job(
        func=lambda: runner.run_weekly(),
        trigger=CronTrigger(day_of_week=cfg.weekly_dow, hour=wh, minute=wm, timezone=timezone),
        id="weekly_review",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=1800,
    )

    # Monthly review: implemented as a daily check at 23:00 on "is last day of month"
    sched.add_job(
        func=lambda: runner.run_monthly_if_last_day(),
        trigger=CronTrigger(hour=mh, minute=mm2, timezone=timezone),
        id="monthly_review_check",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=1800,
    )


def create_background_scheduler(*, runner: ReviewRunner) -> BackgroundScheduler:
    """
    Create a non-blocking scheduler suitable for running inside a FastAPI/Uvicorn process.
    """
    sched = BackgroundScheduler(timezone=runner.cfg.timezone)
    configure_jobs(sched=sched, runner=runner, timezone=runner.cfg.timezone)
    return sched


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    runner = ReviewRunner(cfg)

    sched = BlockingScheduler(timezone=cfg.timezone)
    configure_jobs(sched=sched, runner=runner, timezone=cfg.timezone)

    logging.info("Scheduler started (timezone=%s).", cfg.timezone)
    logging.info("Daily @ %s", cfg.daily_at)
    logging.info("Weekly (%s) @ %s", cfg.weekly_dow, cfg.weekly_at)
    logging.info("Monthly check @ %s (last-day only)", cfg.monthly_at)
    sched.start()


if __name__ == "__main__":
    main()


