"""Scheduler — cron-style background jobs for Loop.

Drives all periodic work: polling calendars, polling inboxes, firing meeting
reminders (30-min + 5-min), and the daily morning briefing. Built on APScheduler.

Phase 1 scope:
    - Wrap an APScheduler background scheduler.
    - Register the core recurring jobs (calendar poll, email poll, briefing).
    - Provide start()/shutdown() lifecycle methods used by the orchestrator.
"""

from __future__ import annotations

from collections.abc import Callable

from apscheduler.schedulers.background import BackgroundScheduler

from config.settings import Settings, get_settings


class Scheduler:
    """Registers and runs Loop's periodic jobs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._scheduler = BackgroundScheduler(timezone=self.settings.timezone)

    def add_interval_job(self, func: Callable[[], None], *, minutes: int,
                         job_id: str) -> None:
        """Register a job that runs every ``minutes`` minutes."""
        # TODO(phase1): register calendar poll (5 min) and email poll (15 min) here.
        self._scheduler.add_job(func, "interval", minutes=minutes, id=job_id,
                                replace_existing=True)

    def add_daily_job(self, func: Callable[[], None], *, hour: int, minute: int,
                      job_id: str) -> None:
        """Register a job that runs once per day at a fixed local time."""
        # TODO(phase1): register the morning briefing at settings.briefing_time.
        self._scheduler.add_job(func, "cron", hour=hour, minute=minute, id=job_id,
                                replace_existing=True)

    def start(self) -> None:
        """Start the background scheduler."""
        # TODO(phase1): wire calendar/email/briefing callbacks before starting.
        self._scheduler.start()

    def shutdown(self) -> None:
        """Stop the scheduler cleanly."""
        self._scheduler.shutdown(wait=False)
