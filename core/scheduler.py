"""Scheduler — cron-style background jobs for Loop.

Drives all periodic work: polling calendars, polling inboxes, firing meeting
reminders (30-min + 5-min), the daily morning briefing, and the end-of-day
task summary. Built on APScheduler.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable

from apscheduler.schedulers.background import BackgroundScheduler

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# When the end-of-day task summary fires (local time).
END_OF_DAY_HOUR = 17
END_OF_DAY_MINUTE = 30


class Scheduler:
    """Registers and runs Loop's periodic jobs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._scheduler = BackgroundScheduler(timezone=self.settings.timezone)

    def add_interval_job(self, func: Callable[[], None], *, minutes: int,
                         job_id: str) -> None:
        """Register a job that runs every ``minutes`` minutes."""
        self._scheduler.add_job(func, "interval", minutes=minutes, id=job_id,
                                replace_existing=True)

    def add_daily_job(self, func: Callable[[], None], *, hour: int, minute: int,
                      job_id: str) -> None:
        """Register a job that runs once per day at a fixed local time."""
        self._scheduler.add_job(func, "cron", hour=hour, minute=minute, id=job_id,
                                replace_existing=True)

    def schedule_end_of_day(self, task_specialist, deliveries: Iterable,
                            *, hour: int = END_OF_DAY_HOUR,
                            minute: int = END_OF_DAY_MINUTE) -> None:
        """Register the 17:30 end-of-day summary job.

        Args:
            task_specialist: an object exposing ``end_of_day_summary() -> str``.
            deliveries: iterable of delivery objects exposing ``send(text)``
                (e.g. ``TelegramDelivery`` and ``TeamsDelivery``).
        """
        delivery_list = list(deliveries)

        def _job() -> None:
            try:
                summary = task_specialist.end_of_day_summary()
            except Exception:  # noqa: BLE001 - never let a job crash the scheduler
                logger.exception("Failed to build end-of-day summary")
                return
            for delivery in delivery_list:
                try:
                    delivery.send(summary)
                except Exception:  # noqa: BLE001 - one channel failing must not block others
                    logger.exception("Failed to deliver end-of-day summary via %s",
                                     type(delivery).__name__)

        self.add_daily_job(_job, hour=hour, minute=minute, job_id="end_of_day_summary")

    def schedule_calendar_conflicts(self, calendar_specialist, *,
                                    minutes: int = 15,
                                    run_immediately: bool = True) -> None:
        """Register the recurring calendar conflict/double-booking check.

        Runs ``calendar_specialist.check_and_warn_conflicts()`` (an async
        coroutine) every ``minutes`` minutes, and optionally once right away at
        startup. The coroutine is driven with :func:`asyncio.run` because
        APScheduler executes sync callables in its worker threads.

        Args:
            calendar_specialist: object exposing an async
                ``check_and_warn_conflicts()`` method.
            minutes: polling interval (defaults to every 15 minutes).
            run_immediately: also run one check at startup.
        """
        def _job() -> None:
            try:
                asyncio.run(calendar_specialist.check_and_warn_conflicts())
            except Exception:  # noqa: BLE001 - never let a job crash the scheduler
                logger.exception("Calendar conflict check failed")

        self.add_interval_job(_job, minutes=minutes, job_id="calendar_conflicts")
        if run_immediately:
            _job()

    def start(self) -> None:
        """Start the background scheduler."""
        self._scheduler.start()

    def shutdown(self) -> None:
        """Stop the scheduler cleanly."""
        self._scheduler.shutdown(wait=False)
