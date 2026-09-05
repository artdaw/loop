"""Google Calendar integration — read events via the Google Calendar API.

Uses ``google-api-python-client`` with OAuth 2.0 (shares the Google credentials
flow with the Gmail connector).

Phase 1 scope:
    - OAuth bootstrap (read-only scope).
    - todays_events(): return today's events, normalised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from config.settings import Settings, get_settings


@dataclass
class GoogleEvent:
    """A normalised Google Calendar event."""

    event_id: str
    title: str
    start: datetime
    end: datetime
    attendees: list[str] = field(default_factory=list)
    location: str = ""


class GoogleCalendarClient:
    """Thin wrapper around the Google Calendar API."""

    SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._service = None

    def _ensure_service(self) -> None:
        """Load OAuth credentials and build the Calendar service object."""
        # TODO(phase1): reuse the Google OAuth flow; cache token alongside Gmail.
        raise NotImplementedError("GoogleCalendarClient._ensure_service is a Phase 1 stub.")

    def todays_events(self) -> list[GoogleEvent]:
        """Return today's events ordered by start time."""
        # TODO(phase1): call events().list() with timeMin/timeMax for today.
        raise NotImplementedError("GoogleCalendarClient.todays_events is a Phase 1 stub.")
