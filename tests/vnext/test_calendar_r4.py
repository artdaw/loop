"""Calendar providers, partial failure and reschedules (P09–P11 — R4).

There was no calendar adapter under `loop/` at all, so these three scenarios
had nothing to be true of. The recorded payloads below are the shapes Google
Calendar and Microsoft Graph actually return; nothing here touches a network.

Every test is a variation on one rule: **a diary that could not be fully read
must never look empty.** A missing meeting and a missing provider produce the
same blank afternoon on screen, and only one of them is safe to book over.
"""

from __future__ import annotations

import datetime as dt
import json

from loop.capabilities.calendar.adapters import (
    CalendarEvent,
    GoogleCalendarAdapter,
    OutlookCalendarAdapter,
    ProviderStatus,
)
from loop.capabilities.calendar.service import CalendarService
from loop.capabilities.weather.adapters.base import HttpResponse, HttpTransport

NOW = 1_788_800_000
START = dt.datetime(2026, 9, 10, 0, 0, tzinfo=dt.UTC)
END = dt.datetime(2026, 9, 11, 0, 0, tzinfo=dt.UTC)

GOOGLE_PAYLOAD = {
    "items": [
        {"id": "g1", "summary": "Standup", "etag": '"rev-1"', "status": "confirmed",
         "start": {"dateTime": "2026-09-10T09:00:00Z", "timeZone": "Europe/Berlin"},
         "end": {"dateTime": "2026-09-10T09:15:00Z"}},
        {"id": "g2", "summary": "Cancelled thing", "etag": '"rev-9"',
         "status": "cancelled",
         "start": {"dateTime": "2026-09-10T11:00:00Z"},
         "end": {"dateTime": "2026-09-10T12:00:00Z"}},
    ]
}

OUTLOOK_PAYLOAD = {
    "value": [
        {"id": "o1", "subject": "Client review", "changeKey": "ck-1",
         "isCancelled": False, "isAllDay": False,
         "start": {"dateTime": "2026-09-10T14:00:00", "timeZone": "UTC"},
         "end": {"dateTime": "2026-09-10T15:00:00", "timeZone": "UTC"}},
    ]
}


class Fake(HttpTransport):
    def __init__(self, payload=None, *, status: int = 200) -> None:
        super().__init__()
        self.payload = payload
        self.status = status
        self.calls: list[str] = []

    def get(self, url: str, *, params=None) -> HttpResponse:
        self.calls.append(url)
        body = json.dumps(self.payload or {}).encode("utf-8")
        return HttpResponse(self.status, body)


def google(**kw) -> GoogleCalendarAdapter:
    return GoogleCalendarAdapter(token="g-token",
                                 transport=Fake(GOOGLE_PAYLOAD), **kw)


def outlook(**kw) -> OutlookCalendarAdapter:
    return OutlookCalendarAdapter(token="o-token",
                                  transport=Fake(OUTLOOK_PAYLOAD), **kw)


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
def test_google_events_are_normalized_with_their_revision():
    read = google().read(start=START, end=END, now=NOW)

    assert read.usable
    standup = next(e for e in read.events if e.event_id == "g1")
    assert standup.title == "Standup"
    assert standup.start == dt.datetime(2026, 9, 10, 9, 0, tzinfo=dt.UTC)
    assert standup.revision == '"rev-1"'
    assert standup.timezone == "Europe/Berlin"


def test_a_cancelled_google_event_is_marked_not_dropped():
    """Dropping it loses the fact that it *was* there and is now gone."""
    read = google().read(start=START, end=END, now=NOW)

    cancelled = next(e for e in read.events if e.event_id == "g2")
    assert cancelled.cancelled is True


def test_outlook_events_are_normalized_to_the_same_shape():
    read = outlook().read(start=START, end=END, now=NOW)

    assert read.usable
    event = read.events[0]
    assert isinstance(event, CalendarEvent)
    assert event.title == "Client review"
    assert event.revision == "ck-1"


def test_an_unconfigured_provider_makes_no_request_at_all():
    """An unauthenticated call either 401s or hits the wrong account."""
    transport = Fake(GOOGLE_PAYLOAD)
    adapter = GoogleCalendarAdapter(token="", transport=transport)

    read = adapter.read(start=START, end=END, now=NOW)

    assert read.status is ProviderStatus.UNAUTHENTICATED
    assert read.events == []
    assert transport.calls == [], "a request was made without credentials"


def test_a_rejected_token_is_reported_as_unauthenticated():
    adapter = GoogleCalendarAdapter(token="stale",
                                    transport=Fake(status=401))

    read = adapter.read(start=START, end=END, now=NOW)

    assert read.status is ProviderStatus.UNAUTHENTICATED
    assert "rejected" in read.detail


def test_a_provider_error_is_unavailable_not_an_empty_diary():
    adapter = GoogleCalendarAdapter(token="t", transport=Fake(status=503))

    read = adapter.read(start=START, end=END, now=NOW)

    assert read.status is ProviderStatus.UNAVAILABLE
    assert read.usable is False
    assert "503" in read.detail


# --------------------------------------------------------------------------- #
# P10 — one of two providers fails
# --------------------------------------------------------------------------- #
def test_a_working_provider_is_kept_when_the_other_fails(sessions, clock):
    broken = GoogleCalendarAdapter(token="t", transport=Fake(status=500))
    service = CalendarService(providers=[broken, outlook()],
                              sessions=sessions, clock=clock)

    view = service.read(start=START, end=END)

    assert [e.event_id for e in view.events] == ["o1"], (
        "the working provider's events were discarded")
    assert view.complete is False
    assert "google" in view.unavailable


def test_the_gap_is_described_rather_than_implied(sessions, clock):
    """The difference between "you are free" and "I could not look"."""
    broken = GoogleCalendarAdapter(token="t", transport=Fake(status=500))
    service = CalendarService(providers=[broken, outlook()],
                              sessions=sessions, clock=clock)

    view = service.read(start=START, end=END)
    gaps = view.describe_gaps()

    assert len(gaps) == 1
    assert "may be missing events" in gaps[0]


def test_a_view_is_complete_only_when_everyone_answered(sessions, clock):
    service = CalendarService(providers=[google(), outlook()],
                              sessions=sessions, clock=clock)

    view = service.read(start=START, end=END)

    assert view.complete
    assert {e.event_id for e in view.events} == {"g1", "o1"}


def test_a_provider_that_raises_does_not_lose_the_others(sessions, clock):
    class Explodes:
        name = "explodes"

        def read(self, *, start, end, now):
            raise RuntimeError("provider blew up")

    service = CalendarService(providers=[Explodes(), outlook()],
                              sessions=sessions, clock=clock)

    view = service.read(start=START, end=END)

    assert [e.event_id for e in view.events] == ["o1"]
    assert "explodes" in view.unavailable


# --------------------------------------------------------------------------- #
# P11 — the same outage, repeatedly
# --------------------------------------------------------------------------- #
def test_a_repeated_outage_is_reported_once(sessions, clock):
    broken = GoogleCalendarAdapter(token="t", transport=Fake(status=500))
    service = CalendarService(providers=[broken, outlook()],
                              sessions=sessions, clock=clock)

    first = service.outages_to_report(service.read(start=START, end=END))
    second = service.outages_to_report(service.read(start=START, end=END))
    third = service.outages_to_report(service.read(start=START, end=END))

    assert first == ["google"]
    assert second == [], "the same outage was announced twice"
    assert third == []


def test_a_recovered_provider_makes_the_next_failure_new_again(sessions, clock):
    broken = GoogleCalendarAdapter(token="t", transport=Fake(status=500))
    service = CalendarService(providers=[broken], sessions=sessions, clock=clock)
    assert service.outages_to_report(service.read(start=START, end=END)) == \
        ["google"]

    service.providers = [google()]                   # it comes back
    service.outages_to_report(service.read(start=START, end=END))

    service.providers = [broken]                     # and fails again later
    assert service.outages_to_report(service.read(start=START, end=END)) == \
        ["google"]


def test_the_outage_record_survives_a_restart(sessions, clock):
    broken = GoogleCalendarAdapter(token="t", transport=Fake(status=500))
    first = CalendarService(providers=[broken], sessions=sessions, clock=clock)
    assert first.outages_to_report(first.read(start=START, end=END)) == ["google"]

    restarted = CalendarService(providers=[broken], sessions=sessions,
                                clock=clock)

    assert restarted.outages_to_report(
        restarted.read(start=START, end=END)) == []


# --------------------------------------------------------------------------- #
# P09 — a meeting moves or is cancelled
# --------------------------------------------------------------------------- #
class Recording:
    """Captures what would have been cancelled."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel_for_subject(self, subject_ref: str) -> int:
        self.cancelled.append(subject_ref)
        return 1


def test_a_rescheduled_meeting_cancels_its_stale_reminders(sessions, clock):
    moved = dict(GOOGLE_PAYLOAD)
    service = CalendarService(providers=[google()], sessions=sessions,
                              clock=clock)
    service.reconcile(service.read(start=START, end=END))     # first sighting

    # The same event, at a new time, with a new revision.
    rescheduled = {"items": [
        {"id": "g1", "summary": "Standup", "etag": '"rev-2"',
         "status": "confirmed",
         "start": {"dateTime": "2026-09-10T10:30:00Z"},
         "end": {"dateTime": "2026-09-10T10:45:00Z"}}]}
    service.providers = [GoogleCalendarAdapter(token="t",
                                               transport=Fake(rescheduled))]
    outbox = Recording()

    result = service.reconcile(service.read(start=START, end=END),
                               outbox=outbox)

    assert result.updated == ["g1"]
    assert result.cancelled_reminders == 1
    assert outbox.cancelled == ["calendar:google:g1"]
    del moved


def test_an_unchanged_meeting_cancels_nothing(sessions, clock):
    service = CalendarService(providers=[google()], sessions=sessions,
                              clock=clock)
    service.reconcile(service.read(start=START, end=END))
    outbox = Recording()

    result = service.reconcile(service.read(start=START, end=END),
                               outbox=outbox)

    assert result.updated == []
    assert outbox.cancelled == []


def test_a_meeting_that_disappears_cancels_its_reminders(sessions, clock):
    service = CalendarService(providers=[google()], sessions=sessions,
                              clock=clock)
    service.reconcile(service.read(start=START, end=END))

    service.providers = [GoogleCalendarAdapter(token="t",
                                               transport=Fake({"items": []}))]
    outbox = Recording()
    result = service.reconcile(service.read(start=START, end=END),
                               outbox=outbox)

    assert result.removed == ["g1"]
    assert outbox.cancelled == ["calendar:google:g1"]


def test_an_outage_is_not_treated_as_every_meeting_being_deleted(sessions,
                                                                 clock):
    """The failure that would cancel a whole diary on one bad response."""
    service = CalendarService(providers=[google()], sessions=sessions,
                              clock=clock)
    service.reconcile(service.read(start=START, end=END))

    service.providers = [GoogleCalendarAdapter(token="t",
                                               transport=Fake(status=503))]
    outbox = Recording()
    result = service.reconcile(service.read(start=START, end=END),
                               outbox=outbox)

    assert result.removed == []
    assert outbox.cancelled == [], "an outage cancelled real reminders"


def test_reconciliation_only_touches_providers_that_answered(sessions, clock):
    service = CalendarService(providers=[google(), outlook()],
                              sessions=sessions, clock=clock)
    service.reconcile(service.read(start=START, end=END))

    # Google answers with nothing; Outlook is down.
    service.providers = [
        GoogleCalendarAdapter(token="t", transport=Fake({"items": []})),
        OutlookCalendarAdapter(token="t", transport=Fake(status=500))]
    outbox = Recording()
    result = service.reconcile(service.read(start=START, end=END),
                               outbox=outbox)

    assert result.removed == ["g1"], "only the answering provider is reconciled"
    assert "calendar:outlook:o1" not in outbox.cancelled


def test_every_provider_time_arrives_as_aware_utc():
    """Naive and aware datetimes in one diary cannot even be sorted.

    Graph omits the offset and names the zone alongside; Google carries it
    inline. Normalising at the boundary is what lets the two be merged at all,
    and a mis-sorted diary has no error to notice.
    """
    berlin = {"value": [
        {"id": "o2", "subject": "Berlin meeting", "changeKey": "ck-2",
         "start": {"dateTime": "2026-09-10T16:00:00", "timeZone": "Europe/Berlin"},
         "end": {"dateTime": "2026-09-10T17:00:00", "timeZone": "Europe/Berlin"}}]}
    adapter = OutlookCalendarAdapter(token="t", transport=Fake(berlin))

    event = adapter.read(start=START, end=END, now=NOW).events[0]

    assert event.start.tzinfo is not None, "a naive time escaped the adapter"
    # 16:00 Berlin in September is 14:00 UTC.
    assert event.start == dt.datetime(2026, 9, 10, 14, 0, tzinfo=dt.UTC)


def test_events_from_both_providers_sort_into_one_diary(sessions, clock):
    service = CalendarService(providers=[google(), outlook()],
                              sessions=sessions, clock=clock)

    view = service.read(start=START, end=END)

    assert [e.event_id for e in view.events] == ["g1", "o1"]
    assert all(e.start.tzinfo is not None for e in view.events)


def test_the_working_provider_is_kept_whichever_order_they_are_read_in(
        sessions, clock):
    """Order-independence, because a bug here is invisible in one arrangement.

    A "clear everything on failure" mistake looks harmless when the failing
    provider is read first — there is nothing to clear yet. It only loses data
    when the good provider came first.
    """
    broken = GoogleCalendarAdapter(token="t", transport=Fake(status=500))

    working_first = CalendarService(providers=[outlook(), broken],
                                    sessions=sessions, clock=clock)
    view = working_first.read(start=START, end=END)

    assert [e.event_id for e in view.events] == ["o1"]
    assert view.complete is False


def test_a_zoneless_provider_time_uses_the_zone_the_provider_named(sessions,
                                                                   clock):
    """The load-bearing half of the timezone fix.

    Graph sends `2026-09-10T16:00:00` with `timeZone: Europe/Berlin`. Reading
    that as UTC puts the meeting two hours early — an error with no symptom
    except a reminder at the wrong time.
    """
    berlin = {"value": [
        {"id": "o3", "subject": "Berlin", "changeKey": "c",
         "start": {"dateTime": "2026-09-10T16:00:00", "timeZone": "Europe/Berlin"},
         "end": {"dateTime": "2026-09-10T17:00:00", "timeZone": "Europe/Berlin"}}]}
    utc = {"value": [
        {"id": "o4", "subject": "UTC", "changeKey": "c",
         "start": {"dateTime": "2026-09-10T16:00:00", "timeZone": "UTC"},
         "end": {"dateTime": "2026-09-10T17:00:00", "timeZone": "UTC"}}]}

    berlin_event = OutlookCalendarAdapter(
        token="t", transport=Fake(berlin)).read(start=START, end=END,
                                                now=NOW).events[0]
    utc_event = OutlookCalendarAdapter(
        token="t", transport=Fake(utc)).read(start=START, end=END,
                                             now=NOW).events[0]

    assert berlin_event.start != utc_event.start, (
        "the named timezone was ignored; both were read as the same instant")
    assert (utc_event.start - berlin_event.start) == dt.timedelta(hours=2)
# The composition root must expose this service and its trusted operation; a
# component-only implementation is not reachable from CLI, bot, or API.
def test_calendar_is_wired_through_the_shared_application(settings, clock):
    from loop.app import build_application

    app = build_application(settings, clock=clock)

    assert isinstance(app.calendar, CalendarService)
    assert "calendar.read" in app.invoker.handlers

