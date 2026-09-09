"""Reading several calendars at once, honestly (P09, P10, P11 — R4).

Three requirements that all pull the same direction: never present a partial
diary as a complete one.

* **P10 — one provider fails.** The successful results are kept and the failing
  provider is named. Dropping everything punishes the working provider; keeping
  everything silently turns an outage into a free afternoon.
* **P11 — the same outage repeats.** One standalone notice, then status only.
  A provider that is down for a day should not produce a message every sweep.
* **P09 — an event moved or was cancelled.** Its stale reminders are cancelled
  and the new occurrence carries the new revision, so an old reminder cannot
  fire for a meeting that is no longer there.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from loop.capabilities.calendar.adapters import (
    CalendarEvent,
    CalendarProvider,
    ProviderRead,
    ProviderStatus,
)
from loop.core.clock import Clock, SystemClock, to_micros

logger = logging.getLogger(__name__)

#: How long one outage stays "already reported" before it is worth saying again.
OUTAGE_COOLDOWN_SECONDS = 6 * 3600


@dataclass
class CalendarView:
    """What the diary looks like, and how much of it is actually known."""

    events: list[CalendarEvent] = field(default_factory=list)
    reads: list[ProviderRead] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True only when every configured provider answered."""
        return bool(self.reads) and all(read.usable for read in self.reads)

    @property
    def unavailable(self) -> dict[str, str]:
        return {read.provider: read.detail
                for read in self.reads if not read.usable}

    def describe_gaps(self) -> list[str]:
        """Wording that never implies the diary is empty rather than partial."""
        return [f"{provider} could not be read ({detail}); this view may be "
                f"missing events from it"
                for provider, detail in sorted(self.unavailable.items())]


@dataclass
class RescheduleResult:
    cancelled_reminders: int = 0
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


class CalendarService:
    """Merges providers and tracks what changed since last time."""

    def __init__(self, *, providers: list[CalendarProvider] | None = None,
                 sessions: sessionmaker[Session] | None = None,
                 clock: Clock | None = None) -> None:
        self.providers = list(providers or [])
        self._sessions = sessions
        self._clock = clock or SystemClock()
        if self._sessions is not None:
            self._ensure_tables()

    def _ensure_tables(self) -> None:
        assert self._sessions is not None
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS calendar_occurrences ("
                " provider TEXT NOT NULL,"
                " event_id TEXT NOT NULL,"
                " revision TEXT NOT NULL,"
                " starts_at INTEGER NOT NULL,"
                " title TEXT NOT NULL,"
                " cancelled INTEGER NOT NULL DEFAULT 0,"
                " PRIMARY KEY (provider, event_id))"))
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS calendar_outages ("
                " provider TEXT PRIMARY KEY,"
                " first_seen_at INTEGER NOT NULL,"
                " last_notified_at INTEGER NOT NULL,"
                " detail TEXT NOT NULL)"))
            session.commit()

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def read(self, *, start: dt.datetime, end: dt.datetime) -> CalendarView:
        """Ask every provider; keep whatever answers (P10)."""
        now = int(self._clock.now().timestamp())
        view = CalendarView()
        for provider in self.providers:
            try:
                read = provider.read(start=start, end=end, now=now)
            except Exception as exc:                   # noqa: BLE001 — reported
                logger.exception("Calendar provider %s raised", provider.name)
                read = ProviderRead(provider.name, ProviderStatus.UNAVAILABLE,
                                    detail=str(exc), fetched_at=now)
            view.reads.append(read)
            # The successful provider's events are kept even when another
            # failed. This is the whole of P10.
            view.events.extend(event for event in read.events
                               if not event.cancelled)
        view.events.sort(key=lambda event: event.start)
        return view

    # ------------------------------------------------------------------ #
    # P11 — an outage is worth saying once
    # ------------------------------------------------------------------ #
    def outages_to_report(self, view: CalendarView) -> list[str]:
        """Providers whose failure has not already been reported recently."""
        if self._sessions is None:
            return sorted(view.unavailable)
        now = to_micros(self._clock.now())
        cooldown = OUTAGE_COOLDOWN_SECONDS * 1_000_000
        report: list[str] = []

        with self._sessions() as session:
            for provider, detail in sorted(view.unavailable.items()):
                row = session.execute(text(
                    "SELECT last_notified_at FROM calendar_outages"
                    " WHERE provider = :provider"),
                    {"provider": provider}).first()
                if row is not None and now - int(row[0]) < cooldown:
                    continue        # same outage, already announced
                session.execute(text(
                    "INSERT INTO calendar_outages (provider, first_seen_at,"
                    " last_notified_at, detail) VALUES (:p, :now, :now, :d)"
                    " ON CONFLICT(provider) DO UPDATE SET"
                    " last_notified_at = excluded.last_notified_at,"
                    " detail = excluded.detail"),
                    {"p": provider, "now": now, "d": detail})
                report.append(provider)
            # A provider that answered is no longer in an outage, so the next
            # failure is a new one rather than a continuation.
            for read in view.reads:
                if read.usable:
                    session.execute(text(
                        "DELETE FROM calendar_outages WHERE provider = :p"),
                        {"p": read.provider})
            session.commit()
        return report

    # ------------------------------------------------------------------ #
    # P09 — a moved or cancelled meeting
    # ------------------------------------------------------------------ #
    def reconcile(self, view: CalendarView, *, triggers: Any = None,
                  outbox: Any = None) -> RescheduleResult:
        """Record occurrences and cancel reminders for what moved or vanished.

        Only providers that actually answered are reconciled. Treating an
        outage as "every meeting was deleted" would cancel every reminder the
        moment a provider hiccuped.
        """
        result = RescheduleResult()
        if self._sessions is None:
            return result

        answered = {read.provider for read in view.reads if read.usable}
        if not answered:
            return result

        seen: set[tuple[str, str]] = set()
        with self._sessions() as session:
            known = {
                (row[0], row[1]): (row[2], row[3])
                for row in session.execute(text(
                    "SELECT provider, event_id, revision, starts_at"
                    " FROM calendar_occurrences")).all()
                if row[0] in answered}

            for event in view.events:
                if event.provider not in answered:
                    continue
                seen.add(event.identity)
                previous = known.get(event.identity)
                starts_at = to_micros(event.start)
                if previous is not None and previous[0] != event.revision:
                    result.updated.append(event.event_id)
                    result.cancelled_reminders += self._cancel_reminders(
                        event, triggers=triggers, outbox=outbox)
                session.execute(text(
                    "INSERT INTO calendar_occurrences (provider, event_id,"
                    " revision, starts_at, title, cancelled)"
                    " VALUES (:p, :e, :r, :s, :t, 0)"
                    " ON CONFLICT(provider, event_id) DO UPDATE SET"
                    " revision = excluded.revision, starts_at = excluded.starts_at,"
                    " title = excluded.title, cancelled = 0"),
                    {"p": event.provider, "e": event.event_id,
                     "r": event.revision, "s": starts_at, "t": event.title})

            # Gone from a provider that answered: cancelled or deleted.
            for identity in known:
                if identity in seen:
                    continue
                result.removed.append(identity[1])
                result.cancelled_reminders += self._cancel_reminders(
                    CalendarEvent(provider=identity[0], event_id=identity[1],
                                  title="", start=self._clock.now(),
                                  end=self._clock.now()),
                    triggers=triggers, outbox=outbox)
                session.execute(text(
                    "DELETE FROM calendar_occurrences WHERE provider = :p"
                    " AND event_id = :e"),
                    {"p": identity[0], "e": identity[1]})
            session.commit()
        return result

    def _cancel_reminders(self, event: CalendarEvent, *, triggers: Any,
                          outbox: Any) -> int:
        """Stale reminders go with the occurrence they were about (P09)."""
        subject = f"calendar:{event.provider}:{event.event_id}"
        cancelled = 0
        if outbox is not None:
            cancelled += int(outbox.cancel_for_subject(subject) or 0)
        if triggers is not None:
            for trigger in triggers.for_subject("calendar", event.event_id):
                if triggers.disable(trigger.id):
                    cancelled += 1
        return cancelled
