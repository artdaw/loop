"""Merging events across calendar providers.

Two failure modes this pins down:

* One connector failing must not lose the other's events. Before this, a single
  stubbed provider raised out of the loop and the whole diary came back empty —
  indistinguishable from a free day.
* Providers disagree about timezone awareness (Google all-day events are naive,
  Graph UTC events are aware). Sorting or subtracting a mix raises TypeError,
  so everything is normalised at the boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from specialists.calendar import CalendarEvent, CalendarSpecialist, _ensure_aware


class Boom:
    """A connector that always fails, like a stubbed provider."""

    def todays_events(self):
        raise NotImplementedError("stub")


class Works:
    def __init__(self, events):
        self._events = events

    def todays_events(self):
        return list(self._events)


class AsyncWorks:
    def __init__(self, events):
        self._events = events

    async def todays_events(self):
        return list(self._events)


def _event(title, hour, *, aware=True, event_id=None):
    start = datetime(2026, 9, 5, hour, 0)
    if aware:
        start = start.replace(tzinfo=UTC)
    return CalendarEvent(
        event_id=event_id or title,
        title=title,
        start=start,
        end=start + timedelta(hours=1),
    )


# --------------------------------------------------------------------------- #
# Timezone normalisation
# --------------------------------------------------------------------------- #
def test_ensure_aware_leaves_aware_datetimes_alone():
    aware = datetime(2026, 9, 5, 9, tzinfo=UTC)
    assert _ensure_aware(aware) is aware


def test_ensure_aware_attaches_local_zone_to_naive():
    result = _ensure_aware(datetime(2026, 9, 5, 9))
    assert result.tzinfo is not None


def test_ensure_aware_passes_none_through():
    assert _ensure_aware(None) is None


# --------------------------------------------------------------------------- #
# Per-connector isolation
# --------------------------------------------------------------------------- #
async def test_a_failing_connector_does_not_lose_the_other(settings):
    good = Works([_event("Standup", 9)])
    specialist = CalendarSpecialist(settings, google_calendar=good,
                                    outlook_calendar=Boom())

    events = await specialist.get_all_events_today()

    assert [e.title for e in events] == ["Standup"]


async def test_all_connectors_failing_returns_empty(settings):
    specialist = CalendarSpecialist(settings, google_calendar=Boom(),
                                    outlook_calendar=Boom())
    assert await specialist.get_all_events_today() == []


async def test_failed_providers_are_reported(settings):
    specialist = CalendarSpecialist(settings, google_calendar=Works([]),
                                    outlook_calendar=Boom())
    await specialist.get_all_events_today()

    assert specialist.last_errors, "expected the failure to be recorded"


async def test_a_clean_run_records_no_errors(settings):
    specialist = CalendarSpecialist(settings, google_calendar=Works([]))
    await specialist.get_all_events_today()

    assert specialist.last_errors == []


async def test_async_connectors_are_awaited(settings):
    specialist = CalendarSpecialist(
        settings, google_calendar=AsyncWorks([_event("Async standup", 9)]))

    events = await specialist.get_all_events_today()

    assert [e.title for e in events] == ["Async standup"]


# --------------------------------------------------------------------------- #
# Mixed awareness across providers
# --------------------------------------------------------------------------- #
async def test_mixing_naive_and_aware_events_does_not_raise(settings):
    """Google all-day events are naive; Graph UTC events are aware."""
    google = Works([_event("All day", 0, aware=False, event_id="g1")])
    outlook = Works([_event("Standup", 9, aware=True, event_id="o1")])
    specialist = CalendarSpecialist(settings, google_calendar=google,
                                    outlook_calendar=outlook)

    events = await specialist.get_all_events_today()

    assert len(events) == 2
    assert all(e.start.tzinfo is not None for e in events)


async def test_events_are_sorted_across_providers(settings):
    google = Works([_event("Later", 15, event_id="g1")])
    outlook = Works([_event("Earlier", 8, event_id="o1")])
    specialist = CalendarSpecialist(settings, google_calendar=google,
                                    outlook_calendar=outlook)

    events = await specialist.get_all_events_today()

    assert [e.title for e in events] == ["Earlier", "Later"]


async def test_the_same_meeting_from_both_providers_is_deduped(settings):
    google = Works([_event("Design review", 11, event_id="g1")])
    outlook = Works([_event("Design review", 11, event_id="o1")])
    specialist = CalendarSpecialist(settings, google_calendar=google,
                                    outlook_calendar=outlook)

    events = await specialist.get_all_events_today()

    assert len(events) == 1


# --------------------------------------------------------------------------- #
# Adapting provider event objects
# --------------------------------------------------------------------------- #
async def test_provider_events_are_adapted_to_calendar_events(settings):
    from integrations.google_calendar import GoogleEvent

    google_event = GoogleEvent(
        event_id="g1", title="Standup",
        start=datetime(2026, 9, 5, 9, tzinfo=UTC),
        end=datetime(2026, 9, 5, 10, tzinfo=UTC),
        location="Room 1", is_personal=True,
    )
    specialist = CalendarSpecialist(settings,
                                    google_calendar=Works([google_event]))

    events = await specialist.get_all_events_today()

    assert isinstance(events[0], CalendarEvent)
    assert events[0].title == "Standup"
    assert events[0].is_personal is True
    assert events[0].location == "Room 1"


def test_conflict_detection_still_works_on_adapted_events(settings):
    a = _event("A", 9)
    b = _event("B", 9)
    conflicts = CalendarSpecialist(settings).detect_conflicts([a, b])

    assert len(conflicts) == 1
    assert conflicts[0].overlap_minutes == 60
