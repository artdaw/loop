"""Calendar transports and normalization (P09–P11 — R4).

Two providers, one shape. Transport is injected and separate from
normalization, exactly as the weather adapters do it, so the parsing is pure
and testable against recorded payloads and the only thing needing credentials
is the transport.

**Missing credentials are a reported status, not a silent empty diary.** An
adapter with no token returns `unavailable` and makes *zero* requests. A free
afternoon that is really an unauthenticated API is the worst possible output:
it is indistinguishable from a genuine gap, and the owner books over a meeting.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from loop.capabilities.weather.adapters.base import AdapterError, HttpTransport

logger = logging.getLogger(__name__)


class ProviderStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    UNAUTHENTICATED = "unauthenticated"


@dataclass(frozen=True)
class CalendarEvent:
    """One occurrence, normalized across providers."""

    provider: str
    event_id: str
    title: str
    start: dt.datetime
    end: dt.datetime
    #: The provider's own version/etag. A reschedule changes it, which is how
    #: a stale reminder is recognised without comparing every field.
    revision: str = ""
    cancelled: bool = False
    all_day: bool = False
    timezone: str = ""
    #: Recurrence master id, when this is one instance of a series.
    series_id: str = ""

    @property
    def identity(self) -> tuple[str, str]:
        return (self.provider, self.event_id)


@dataclass
class ProviderRead:
    """What one provider returned, including the reason it returned nothing."""

    provider: str
    status: ProviderStatus
    events: list[CalendarEvent] = field(default_factory=list)
    detail: str = ""
    fetched_at: int = 0

    @property
    def usable(self) -> bool:
        return self.status is ProviderStatus.OK


class CalendarProvider(Protocol):
    @property
    def name(self) -> str: ...

    def read(self, *, start: dt.datetime, end: dt.datetime,
             now: int) -> ProviderRead: ...


def _parse(value: Any, *, timezone: str = "") -> dt.datetime | None:
    """Parse a provider timestamp into an **aware** datetime, always.

    Microsoft Graph returns `dateTime` with no offset and names the zone in a
    sibling `timeZone` field; Google returns an offset inline. Mixing the two
    yields naive and aware datetimes in one list, which cannot even be sorted
    — and a diary sorted wrongly across providers puts meetings in the wrong
    order without any error to notice.
    """
    if not value:
        return None
    text = str(value)
    try:
        if len(text) == 10:                      # an all-day date
            parsed = dt.datetime.fromisoformat(text)
        else:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable calendar time %r", value)
        return None

    if parsed.tzinfo is not None:
        return parsed.astimezone(dt.UTC)
    if timezone:
        try:
            from zoneinfo import ZoneInfo

            return parsed.replace(tzinfo=ZoneInfo(timezone)).astimezone(dt.UTC)
        except Exception:                          # noqa: BLE001 — reported
            logger.warning("Unknown calendar timezone %r; assuming UTC",
                           timezone)
    # UTC is the documented default for a zone-less Graph value, and an
    # explicit assumption beats a naive datetime travelling onward.
    return parsed.replace(tzinfo=dt.UTC)


class GoogleCalendarAdapter:
    """Google Calendar events, read through an injected transport.

    `singleEvents=true` is requested so the provider expands a recurrence into
    instances. Expanding it here would mean reimplementing RFC 5545 — and
    getting a recurrence rule subtly wrong shows up as a meeting on the wrong
    day, which is worse than not having the feature.
    """

    BASE = "https://www.googleapis.com/calendar/v3"

    def __init__(self, *, token: str = "", calendar_id: str = "primary",
                 transport: HttpTransport | None = None) -> None:
        self.token = token.strip()
        self.calendar_id = calendar_id
        self._transport = transport or HttpTransport()

    @property
    def name(self) -> str:
        return "google"

    def read(self, *, start: dt.datetime, end: dt.datetime,
             now: int) -> ProviderRead:
        if not self.token:
            # No request is made at all. An unauthenticated call would either
            # 401 or, worse, succeed against the wrong account.
            return ProviderRead(self.name, ProviderStatus.UNAUTHENTICATED,
                                detail="no Google credentials are configured",
                                fetched_at=now)
        try:
            response = self._transport.get(
                f"{self.BASE}/calendars/{self.calendar_id}/events",
                params={"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                        "singleEvents": "true", "orderBy": "startTime",
                        "access_token": self.token})
        except AdapterError as exc:
            return ProviderRead(self.name, ProviderStatus.UNAVAILABLE,
                                detail=exc.message, fetched_at=now)
        if response.status_code == 401:
            return ProviderRead(self.name, ProviderStatus.UNAUTHENTICATED,
                                detail="the Google token was rejected",
                                fetched_at=now)
        if response.status_code != 200:
            return ProviderRead(self.name, ProviderStatus.UNAVAILABLE,
                                detail=f"HTTP {response.status_code}",
                                fetched_at=now)
        try:
            payload = response.json()
        except Exception as exc:                       # noqa: BLE001 — reported
            return ProviderRead(self.name, ProviderStatus.UNAVAILABLE,
                                detail=f"unreadable response: {exc}",
                                fetched_at=now)
        return ProviderRead(self.name, ProviderStatus.OK,
                            events=self.parse(payload), fetched_at=now)

    def parse(self, payload: Any) -> list[CalendarEvent]:
        if not isinstance(payload, dict):
            raise AdapterError(self.name, "response was not a JSON object")
        events: list[CalendarEvent] = []
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            start_block = item.get("start") or {}
            end_block = item.get("end") or {}
            all_day = "date" in start_block
            start = _parse(start_block.get("dateTime") or start_block.get("date"),
                           timezone=str(start_block.get("timeZone", "")))
            end = _parse(end_block.get("dateTime") or end_block.get("date"),
                         timezone=str(end_block.get("timeZone", "")))
            if start is None or end is None:
                continue
            events.append(CalendarEvent(
                provider=self.name, event_id=str(item.get("id", "")),
                title=str(item.get("summary", "(no title)")),
                start=start, end=end,
                revision=str(item.get("etag", "")),
                cancelled=str(item.get("status", "")) == "cancelled",
                all_day=all_day,
                timezone=str(start_block.get("timeZone", "")),
                series_id=str(item.get("recurringEventId", ""))))
        return events


class OutlookCalendarAdapter:
    """Microsoft Graph calendarView, read through an injected transport.

    `calendarView` rather than `events`, because it is the endpoint that
    expands recurrences into occurrences over a window — the same reason
    Google gets `singleEvents=true`.
    """

    BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self, *, token: str = "",
                 transport: HttpTransport | None = None) -> None:
        self.token = token.strip()
        self._transport = transport or HttpTransport()

    @property
    def name(self) -> str:
        return "outlook"

    def read(self, *, start: dt.datetime, end: dt.datetime,
             now: int) -> ProviderRead:
        if not self.token:
            return ProviderRead(self.name, ProviderStatus.UNAUTHENTICATED,
                                detail="no Microsoft credentials are configured",
                                fetched_at=now)
        try:
            response = self._transport.get(
                f"{self.BASE}/me/calendarView",
                params={"startDateTime": start.isoformat(),
                        "endDateTime": end.isoformat(),
                        "access_token": self.token})
        except AdapterError as exc:
            return ProviderRead(self.name, ProviderStatus.UNAVAILABLE,
                                detail=exc.message, fetched_at=now)
        if response.status_code == 401:
            return ProviderRead(self.name, ProviderStatus.UNAUTHENTICATED,
                                detail="the Microsoft token was rejected",
                                fetched_at=now)
        if response.status_code != 200:
            return ProviderRead(self.name, ProviderStatus.UNAVAILABLE,
                                detail=f"HTTP {response.status_code}",
                                fetched_at=now)
        try:
            payload = response.json()
        except Exception as exc:                       # noqa: BLE001 — reported
            return ProviderRead(self.name, ProviderStatus.UNAVAILABLE,
                                detail=f"unreadable response: {exc}",
                                fetched_at=now)
        return ProviderRead(self.name, ProviderStatus.OK,
                            events=self.parse(payload), fetched_at=now)

    def parse(self, payload: Any) -> list[CalendarEvent]:
        if not isinstance(payload, dict):
            raise AdapterError(self.name, "response was not a JSON object")
        events: list[CalendarEvent] = []
        for item in payload.get("value", []):
            if not isinstance(item, dict):
                continue
            start_block = item.get("start") or {}
            end_block = item.get("end") or {}
            start = _parse(start_block.get("dateTime"),
                           timezone=str(start_block.get("timeZone", "")))
            end = _parse(end_block.get("dateTime"),
                         timezone=str(end_block.get("timeZone", "")))
            if start is None or end is None:
                continue
            events.append(CalendarEvent(
                provider=self.name, event_id=str(item.get("id", "")),
                title=str(item.get("subject", "(no title)")),
                start=start, end=end,
                revision=str(item.get("changeKey", "")),
                cancelled=bool(item.get("isCancelled", False)),
                all_day=bool(item.get("isAllDay", False)),
                timezone=str(start_block.get("timeZone", "")),
                series_id=str(item.get("seriesMasterId", "")))) 
        return events
