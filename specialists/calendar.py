"""Calendar Specialist — meeting reminders and the morning briefing.

Reads Google Calendar and Outlook Calendar, then delivers 30-minute and
5-minute warnings before each meeting plus a daily morning briefing. Personal
events are processed locally only (their titles/participants never reach a
cloud LLM).

Phase 1 scope:
    - Fetch today's events from both calendar integrations.
    - Schedule 30-min + 5-min reminders per event via the scheduler.
    - Compose the morning briefing (today's meetings + flagged emails).
    - Route personal events through the privacy gate (local-only).

Conflict/double-booking detection lands in Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from config.settings import Settings, get_settings


@dataclass
class CalendarEvent:
    """A normalised calendar event from any provider."""

    event_id: str
    title: str
    start: datetime
    end: datetime
    attendees: list[str] = field(default_factory=list)
    location: str = ""
    is_personal: bool = False  # drives the privacy gate


class CalendarSpecialist:
    """Focused sub-agent for calendars and meeting reminders."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # TODO(phase1): accept google_calendar + outlook_calendar connectors,
        #               the Scheduler, and the delivery layer.

    def todays_events(self) -> list[CalendarEvent]:
        """Fetch and merge today's events from all connected calendars."""
        # TODO(phase1): pull from both providers, normalise, sort by start.
        raise NotImplementedError("CalendarSpecialist.todays_events is a Phase 1 stub.")

    def schedule_reminders(self, events: list[CalendarEvent]) -> None:
        """Register 30-min and 5-min reminder jobs for each event."""
        # TODO(phase1): add scheduler jobs; mark personal events local-only.
        raise NotImplementedError("CalendarSpecialist.schedule_reminders is a Phase 1 stub.")

    def morning_briefing(self) -> str:
        """Compose the 08:00 briefing text (meetings + flagged emails)."""
        # TODO(phase1): assemble today's schedule + Email Specialist flags.
        raise NotImplementedError("CalendarSpecialist.morning_briefing is a Phase 1 stub.")
