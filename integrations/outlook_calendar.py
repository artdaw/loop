"""Outlook Calendar integration — read events via Microsoft Graph.

Uses ``msgraph-sdk`` with OAuth 2.0 (shares the Azure app registration with the
Outlook mail connector).

Phase 1 scope:
    - Authenticate against Microsoft Graph (Calendars.Read).
    - todays_events(): return today's events, normalised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from config.settings import Settings, get_settings


@dataclass
class OutlookEvent:
    """A normalised Outlook Calendar event."""

    event_id: str
    title: str
    start: datetime
    end: datetime
    attendees: list[str] = field(default_factory=list)
    location: str = ""


class OutlookCalendarClient:
    """Thin wrapper around Microsoft Graph calendar endpoints."""

    SCOPES = ["Calendars.Read"]

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = None

    def _ensure_client(self) -> None:
        """Authenticate and build the Graph client."""
        # TODO(phase1): reuse the Outlook azure.identity credential.
        raise NotImplementedError("OutlookCalendarClient._ensure_client is a Phase 1 stub.")

    def todays_events(self) -> list[OutlookEvent]:
        """Return today's events ordered by start time."""
        # TODO(phase1): GET /me/calendarView with start/end covering today.
        raise NotImplementedError("OutlookCalendarClient.todays_events is a Phase 1 stub.")
