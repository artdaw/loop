"""The Google Calendar connector."""

from __future__ import annotations

from datetime import date

from config.settings import Settings
from integrations.google_calendar import (
    GoogleCalendarClient,
    _declined_by_me,
    _parse_boundary,
    _to_event,
)


def _timed(event_id="e1", summary="Standup", start="2026-09-05T09:30:00Z",
           end="2026-09-05T10:00:00Z", **extra) -> dict:
    return {"id": event_id, "summary": summary,
            "start": {"dateTime": start}, "end": {"dateTime": end}, **extra}


# --------------------------------------------------------------------------- #
# Boundary parsing
# --------------------------------------------------------------------------- #
def test_parses_a_zoned_datetime():
    parsed, all_day = _parse_boundary({"dateTime": "2026-09-05T09:30:00Z"})
    assert parsed.hour == 9 and all_day is False


def test_parses_an_all_day_date():
    parsed, all_day = _parse_boundary({"date": "2026-09-05"})
    assert parsed.date() == date(2026, 9, 5)
    assert all_day is True


def test_unparseable_boundary_returns_none():
    assert _parse_boundary({"dateTime": "nonsense"}) == (None, False)
    assert _parse_boundary({}) == (None, False)
    assert _parse_boundary("not a dict") == (None, False)


# --------------------------------------------------------------------------- #
# Event normalisation
# --------------------------------------------------------------------------- #
def test_normalises_a_timed_event():
    event = _to_event(_timed(location="Room 3",
                             attendees=[{"email": "a@b.c"}, {"email": "d@e.f"}]))

    assert event.event_id == "e1"
    assert event.title == "Standup"
    assert event.location == "Room 3"
    assert event.attendees == ["a@b.c", "d@e.f"]
    assert event.all_day is False


def test_event_without_an_id_is_dropped():
    assert _to_event({"summary": "no id"}) is None


def test_event_without_a_start_is_dropped():
    assert _to_event({"id": "e1", "summary": "x"}) is None


def test_missing_end_gets_a_one_hour_default():
    event = _to_event({"id": "e1", "summary": "x",
                       "start": {"dateTime": "2026-09-05T09:00:00Z"}})
    assert (event.end - event.start).total_seconds() == 3600


def test_all_day_event_without_end_spans_a_day():
    event = _to_event({"id": "e1", "summary": "Holiday",
                       "start": {"date": "2026-09-05"}})
    assert event.all_day is True
    assert (event.end - event.start).days == 1


def test_untitled_event_gets_a_placeholder():
    assert _to_event({"id": "e1", "start": {"dateTime": "2026-09-05T09:00:00Z"}}
                     ).title == "(no title)"


def test_attendees_without_an_email_are_skipped():
    event = _to_event(_timed(attendees=[{"email": "a@b.c"}, {"displayName": "Room"}]))
    assert event.attendees == ["a@b.c"]


def test_personal_flag_is_propagated():
    assert _to_event(_timed(), personal_calendar=True).is_personal is True


# --------------------------------------------------------------------------- #
# Declined invitations
# --------------------------------------------------------------------------- #
def test_declined_by_me_is_detected():
    raw = _timed(attendees=[{"email": "me@x.com", "self": True,
                             "responseStatus": "declined"}])
    assert _declined_by_me(raw) is True


def test_accepted_by_me_is_not_declined():
    raw = _timed(attendees=[{"email": "me@x.com", "self": True,
                             "responseStatus": "accepted"}])
    assert _declined_by_me(raw) is False


def test_someone_elses_decline_does_not_count():
    raw = _timed(attendees=[{"email": "other@x.com", "responseStatus": "declined"}])
    assert _declined_by_me(raw) is False


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
class FakeExec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeEvents:
    def __init__(self, items):
        self._items = items
        self.kwargs = None

    def list(self, **kwargs):
        self.kwargs = kwargs
        return FakeExec({"items": self._items})


class FakeService:
    def __init__(self, items):
        self._events = FakeEvents(items)

    def events(self):
        return self._events


def test_todays_events_returns_normalised_events(settings):
    client = GoogleCalendarClient(settings, service=FakeService([_timed()]))
    events = client.todays_events()

    assert len(events) == 1
    assert events[0].title == "Standup"


def test_cancelled_events_are_skipped(settings):
    items = [_timed(status="cancelled"), _timed("e2", "Real")]
    events = GoogleCalendarClient(settings, service=FakeService(items)).todays_events()

    assert [e.title for e in events] == ["Real"]


def test_declined_events_are_skipped(settings):
    declined = _timed("e1", "Skip me", attendees=[
        {"email": "me@x.com", "self": True, "responseStatus": "declined"}])
    items = [declined, _timed("e2", "Keep me")]

    events = GoogleCalendarClient(settings, service=FakeService(items)).todays_events()

    assert [e.title for e in events] == ["Keep me"]


def test_events_are_sorted_by_start(settings):
    items = [
        _timed("e1", "Late", "2026-09-05T16:00:00Z", "2026-09-05T17:00:00Z"),
        _timed("e2", "Early", "2026-09-05T08:00:00Z", "2026-09-05T09:00:00Z"),
    ]
    events = GoogleCalendarClient(settings, service=FakeService(items)).todays_events()

    assert [e.title for e in events] == ["Early", "Late"]


def test_recurring_events_are_expanded_via_single_events(settings):
    service = FakeService([])
    GoogleCalendarClient(settings, service=service).todays_events()

    assert service._events.kwargs["singleEvents"] is True


def test_a_secondary_calendar_is_flagged_personal(tmp_path):
    settings = Settings(_env_file=None, obsidian_vault_path=str(tmp_path),
                        private_personal_calendar=True)
    client = GoogleCalendarClient(settings, service=FakeService([_timed()]),
                                  calendar_id="personal@group.calendar.google.com")

    assert client.todays_events()[0].is_personal is True


def test_primary_calendar_is_not_personal(settings):
    client = GoogleCalendarClient(settings, service=FakeService([_timed()]))
    assert client.todays_events()[0].is_personal is False


def test_empty_calendar_returns_empty_list(settings):
    assert GoogleCalendarClient(settings, service=FakeService([])).todays_events() == []
