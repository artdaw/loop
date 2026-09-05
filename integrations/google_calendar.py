"""Google Calendar integration — read today's events.

Shares the OAuth credentials and cached token with the Gmail connector (see
:mod:`integrations.google_auth`), so connecting one connects both.

As in the Gmail connector, event normalisation is a module-level pure function
so the fiddly parts — all-day events, floating vs. zoned times, self-declined
invitations — are tested without a Google service or a token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class GoogleEvent:
    """A normalised Google Calendar event."""

    event_id: str
    title: str
    start: datetime
    end: datetime
    attendees: list[str] = field(default_factory=list)
    location: str = ""
    all_day: bool = False
    is_personal: bool = False


def _parse_boundary(raw: dict) -> tuple[datetime | None, bool]:
    """Parse a Google ``start``/``end`` object. Returns (datetime, all_day)."""
    if not isinstance(raw, dict):
        return None, False

    if raw.get("dateTime"):
        text = str(raw["dateTime"]).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None, False
        # Naive means "floating local time"; treat it as local, not UTC.
        return parsed, False

    if raw.get("date"):
        try:
            day = date.fromisoformat(str(raw["date"]))
        except ValueError:
            return None, False
        return datetime.combine(day, time.min), True

    return None, False


def _to_event(raw: dict, *, personal_calendar: bool = False) -> GoogleEvent | None:
    """Normalise one Google Calendar event resource, or ``None`` if unusable."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None

    start, all_day = _parse_boundary(raw.get("start") or {})
    end, _ = _parse_boundary(raw.get("end") or {})
    if start is None:
        return None
    if end is None:
        # A missing end is legal for point-in-time entries; assume one hour so
        # conflict detection still has a range to work with.
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))

    attendees = [
        str(person.get("email"))
        for person in (raw.get("attendees") or [])
        if isinstance(person, dict) and person.get("email")
    ]

    return GoogleEvent(
        event_id=str(raw["id"]),
        title=str(raw.get("summary") or "(no title)"),
        start=start,
        end=end,
        attendees=attendees,
        location=str(raw.get("location") or ""),
        all_day=all_day,
        is_personal=personal_calendar,
    )


def _declined_by_me(raw: dict) -> bool:
    """True when the user declined the invitation — not really on the diary."""
    for person in raw.get("attendees") or []:
        if isinstance(person, dict) and person.get("self"):
            return str(person.get("responseStatus", "")).lower() == "declined"
    return False


class GoogleCalendarClient:
    """Thin wrapper around the Google Calendar API."""

    #: Kept for backwards compatibility; real scopes live in google_auth.
    SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

    def __init__(self, settings: Settings | None = None,
                 service: Any | None = None,
                 auth: Any | None = None,
                 calendar_id: str = "primary") -> None:
        self.settings = settings or get_settings()
        self.calendar_id = calendar_id
        self._service = service
        self._auth = auth

    def _get_auth(self) -> Any:
        if self._auth is None:
            from integrations.google_auth import GoogleAuth

            self._auth = GoogleAuth(self.settings)
        return self._auth

    @property
    def configured(self) -> bool:
        if self._service is not None:
            return True
        return bool(self._get_auth().configured)

    def _ensure_service(self) -> Any:
        if self._service is None:
            self._service = self._get_auth().build("calendar", "v3")
        return self._service

    def todays_events(self, day: date | None = None) -> list[GoogleEvent]:
        """Return today's events ordered by start time.

        Personal-calendar handling: when ``settings.private_personal_calendar``
        is set, events on a non-primary calendar are flagged ``is_personal`` so
        downstream code masks their titles and keeps them off cloud models.
        """
        target = day or date.today()
        service = self._ensure_service()

        # Local midnight-to-midnight, expressed with the machine's offset.
        start_of_day = datetime.combine(target, time.min).astimezone()
        end_of_day = datetime.combine(target, time.max).astimezone()

        response = (
            service.events()
            .list(
                calendarId=self.calendar_id,
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                singleEvents=True,      # expand recurring series into instances
                orderBy="startTime",
                maxResults=50,
            )
            .execute()
        )

        personal = (self.calendar_id != "primary"
                    and self.settings.private_personal_calendar)

        events: list[GoogleEvent] = []
        for raw in response.get("items") or []:
            if str(raw.get("status", "")).lower() == "cancelled":
                continue
            if _declined_by_me(raw):
                continue
            event = _to_event(raw, personal_calendar=personal)
            if event is not None:
                events.append(event)

        events.sort(key=lambda event: event.start.replace(tzinfo=None)
                    if event.start.tzinfo else event.start)
        return events

    def upcoming_events(self, *, hours: int = 24) -> list[GoogleEvent]:
        """Return events starting within the next ``hours`` (for reminders)."""
        service = self._ensure_service()
        now = datetime.now(UTC)
        response = (
            service.events()
            .list(
                calendarId=self.calendar_id,
                timeMin=now.isoformat(),
                timeMax=(now + timedelta(hours=hours)).isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            )
            .execute()
        )
        events = [_to_event(raw) for raw in response.get("items") or []]
        return [event for event in events if event is not None]
