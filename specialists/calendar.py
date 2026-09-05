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
from difflib import SequenceMatcher

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


@dataclass
class ConflictWarning:
    """Two events whose time ranges overlap (a double-booking)."""

    event1: CalendarEvent
    event2: CalendarEvent
    overlap_minutes: int

    def describe(self) -> str:
        """Human-readable warning line for delivery."""

        def _fmt(ev: CalendarEvent) -> str:
            title = "(private)" if ev.is_personal else ev.title
            return f"{ev.start:%H:%M}-{ev.end:%H:%M} {title}"

        return (
            f"\u26a0\ufe0f Double-booking ({self.overlap_minutes} min overlap): "
            f"{_fmt(self.event1)}  \u2194  {_fmt(self.event2)}"
        )


class CalendarSpecialist:
    """Focused sub-agent for calendars and meeting reminders."""

    def __init__(self, settings: Settings | None = None,
                 google_calendar: object | None = None,
                 outlook_calendar: object | None = None,
                 delivery: object | None = None) -> None:
        self.settings = settings or get_settings()
        self._google = google_calendar
        self._outlook = outlook_calendar
        self._delivery = delivery
        # TODO(phase1): accept the Scheduler for reminder jobs.

    # ------------------------------------------------------------------ #
    # Conflict detection (Phase 3)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _overlap_minutes(a: CalendarEvent, b: CalendarEvent) -> int:
        """Return the overlap between two events in whole minutes (0 if none)."""
        latest_start = max(a.start, b.start)
        earliest_end = min(a.end, b.end)
        delta = (earliest_end - latest_start).total_seconds()
        return int(delta // 60) if delta > 0 else 0

    def detect_conflicts(self, events: list[CalendarEvent]) -> list[ConflictWarning]:
        """Return every pair of events whose time ranges overlap.

        Events are compared pairwise after sorting by start time. Any positive
        overlap is reported as a :class:`ConflictWarning`. Personal events are
        included (double-bookings matter regardless of privacy), but their
        titles are masked when the warning is rendered.
        """
        ordered = sorted(events, key=lambda e: e.start)
        conflicts: list[ConflictWarning] = []
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                if b.start >= a.end:
                    # Sorted by start: nothing after b can overlap a either.
                    break
                overlap = self._overlap_minutes(a, b)
                if overlap > 0:
                    conflicts.append(ConflictWarning(event1=a, event2=b,
                                                     overlap_minutes=overlap))
        return conflicts

    @staticmethod
    def _same_event(a: CalendarEvent, b: CalendarEvent) -> bool:
        """Heuristic dedup: same start and a very similar title."""
        if abs((a.start - b.start).total_seconds()) > 60:
            return False
        ratio = SequenceMatcher(None, a.title.lower(), b.title.lower()).ratio()
        return ratio >= 0.85

    def _merge_dedup(self, events: list[CalendarEvent]) -> list[CalendarEvent]:
        """Merge events from multiple providers, dropping near-duplicates."""
        merged: list[CalendarEvent] = []
        for ev in sorted(events, key=lambda e: e.start):
            if any(self._same_event(ev, kept) for kept in merged):
                continue
            merged.append(ev)
        return merged

    async def get_all_events_today(self) -> list[CalendarEvent]:
        """Fetch today's events from every connected calendar, merged + deduped.

        Each connector is expected to expose an async ``todays_events()``
        returning :class:`CalendarEvent` objects. Missing connectors are
        skipped so the method degrades gracefully in partial deployments.
        """
        collected: list[CalendarEvent] = []
        for connector in (self._google, self._outlook):
            if connector is None:
                continue
            fetch = getattr(connector, "todays_events", None)
            if fetch is None:
                continue
            result = fetch()
            if hasattr(result, "__await__"):
                result = await result
            collected.extend(result or [])
        return self._merge_dedup(collected)

    async def check_and_warn_conflicts(self) -> list[ConflictWarning]:
        """Detect conflicts across all calendars and push a warning immediately.

        Returns the conflicts found (possibly empty). When a delivery layer is
        wired in, each conflict is sent right away so the user can resolve the
        double-booking before either meeting starts.
        """
        events = await self.get_all_events_today()
        conflicts = self.detect_conflicts(events)
        if conflicts and self._delivery is not None:
            send = getattr(self._delivery, "send", None)
            if send is not None:
                lines = ["You have overlapping meetings today:"]
                lines.extend(c.describe() for c in conflicts)
                message = "\n".join(lines)
                result = send(message)
                if hasattr(result, "__await__"):
                    await result
        return conflicts

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
