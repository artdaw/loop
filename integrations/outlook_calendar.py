"""Outlook Calendar via Microsoft Graph.

Uses ``/me/calendarView``, which — unlike ``/me/events`` — expands recurring
series into individual occurrences inside a time window. That distinction is
the whole reason this endpoint exists: ``/me/events`` would return the recurring
*master* once and Loop would miss every occurrence after the first.

Graph returns event times in the timezone named by the ``Prefer`` header, so the
client asks for UTC explicitly rather than accepting whatever the mailbox
default happens to be.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: Event fields Loop actually reads.
EVENT_FIELDS = ("id,subject,start,end,location,attendees,isCancelled,"
                "sensitivity,showAs")

#: Graph sensitivity values that mean "do not show the title around".
PRIVATE_SENSITIVITIES = frozenset({"private", "confidential"})


@dataclass
class OutlookEvent:
    """A normalised Outlook Calendar event."""

    event_id: str
    title: str
    start: datetime
    end: datetime
    attendees: list[str] = field(default_factory=list)
    location: str = ""
    all_day: bool = False
    is_personal: bool = False


def _parse_graph_time(raw: Any) -> datetime | None:
    """Parse Graph's ``{"dateTime": ..., "timeZone": ...}`` shape."""
    if not isinstance(raw, dict):
        return None
    text = raw.get("dateTime")
    if not text:
        return None
    # Graph emits fractional seconds with more digits than fromisoformat took
    # before 3.11, and no offset when the zone is given separately.
    cleaned = str(text).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        try:
            parsed = datetime.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None and str(raw.get("timeZone", "")).upper() == "UTC":
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _attendees(entries: Any) -> list[str]:
    addresses: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        address = (entry.get("emailAddress") or {}).get("address")
        if address:
            addresses.append(str(address))
    return addresses


def _to_event(raw: dict, *, treat_private_as_personal: bool = True
              ) -> OutlookEvent | None:
    """Normalise one Graph calendarView entry, or ``None`` if unusable."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None

    start = _parse_graph_time(raw.get("start"))
    if start is None:
        return None
    end = _parse_graph_time(raw.get("end"))
    all_day = bool(raw.get("isAllDay"))
    if end is None:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))

    location = ""
    location_holder = raw.get("location")
    if isinstance(location_holder, dict):
        location = str(location_holder.get("displayName") or "")

    sensitivity = str(raw.get("sensitivity") or "").lower()
    is_personal = treat_private_as_personal and sensitivity in PRIVATE_SENSITIVITIES

    return OutlookEvent(
        event_id=str(raw["id"]),
        title=str(raw.get("subject") or "(no title)"),
        start=start,
        end=end,
        attendees=_attendees(raw.get("attendees")),
        location=location,
        all_day=all_day,
        is_personal=is_personal,
    )


class OutlookCalendarClient:
    """Thin wrapper around Graph's calendarView."""

    def __init__(self, settings: Settings | None = None,
                 graph: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._graph = graph

    def _get_graph(self) -> Any:
        if self._graph is None:
            from integrations.ms_graph import GraphClient

            self._graph = GraphClient(self.settings)
        return self._graph

    @property
    def configured(self) -> bool:
        return bool(self._get_graph().configured)

    def _window(self, start: datetime, end: datetime) -> list[OutlookEvent]:
        response = self._get_graph().get(
            "/me/calendarView",
            **{
                "startDateTime": start.isoformat(),
                "endDateTime": end.isoformat(),
                "$select": EVENT_FIELDS,
                "$orderby": "start/dateTime",
                "$top": 50,
            },
        )

        treat_private = bool(self.settings.private_personal_calendar)
        events: list[OutlookEvent] = []
        for raw in response.get("value") or []:
            if raw.get("isCancelled"):
                continue
            event = _to_event(raw, treat_private_as_personal=treat_private)
            if event is not None:
                events.append(event)

        events.sort(key=lambda event: event.start.replace(tzinfo=None)
                    if event.start.tzinfo else event.start)
        return events

    def todays_events(self, day: date | None = None) -> list[OutlookEvent]:
        """Return today's events ordered by start time."""
        target = day or date.today()
        start = datetime.combine(target, time.min).astimezone()
        end = datetime.combine(target, time.max).astimezone()
        return self._window(start, end)

    def upcoming_events(self, *, hours: int = 24) -> list[OutlookEvent]:
        """Return events starting within the next ``hours`` (for reminders)."""
        now = datetime.now(UTC)
        return self._window(now, now + timedelta(hours=hours))
