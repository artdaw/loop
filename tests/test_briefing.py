"""The daily briefing behind `loop briefing`.

Mirrors the weekly review's split: `collect()` is pure data from local state,
`compose()` renders it. Meetings are the one section that needs a calendar
connector, so its absence is reported as a configuration state rather than
silently rendering an empty agenda.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from specialists.briefing import DailyBriefing


class FakeCalendar:
    """Stand-in for CalendarSpecialist."""

    def __init__(self, events=None, boom: bool = False) -> None:
        self.events = events or []
        self.boom = boom

    async def get_all_events_today(self):
        if self.boom:
            raise RuntimeError("graph api down")
        return list(self.events)


class Event:
    def __init__(self, title, start, end=None, is_personal=False, location=""):
        self.title = title
        self.start = start
        self.end = end or (start + timedelta(hours=1))
        self.is_personal = is_personal
        self.location = location


def _briefing(settings, memory_store, calendar=None) -> DailyBriefing:
    return DailyBriefing(settings, memory=memory_store, calendar=calendar)


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
def test_collects_tasks_due_today(settings, memory_store):
    memory_store.add_task("today", due_date=date.today())
    memory_store.add_task("later", due_date=date.today() + timedelta(days=5))

    agenda = _briefing(settings, memory_store).collect()

    assert [t.description for t in agenda.due_today] == ["today"]


def test_collects_overdue_tasks(settings, memory_store):
    memory_store.add_task("late", due_date=date.today() - timedelta(days=2))

    agenda = _briefing(settings, memory_store).collect()

    assert len(agenda.overdue) == 1


def test_collects_follow_ups_ranked_by_triage(settings, memory_store):
    memory_store.add_follow_up(thread_id="low", subject="low", triage_score=2)
    memory_store.add_follow_up(thread_id="high", subject="high", triage_score=20)

    agenda = _briefing(settings, memory_store).collect()

    assert agenda.follow_ups[0].subject == "high"


def test_no_calendar_means_calendar_unavailable(settings, memory_store):
    agenda = _briefing(settings, memory_store).collect()

    assert agenda.calendar_connected is False
    assert agenda.meetings == []


def test_calendar_events_are_collected(settings, memory_store):
    event = Event("Standup", datetime.now().replace(hour=9, minute=0))
    agenda = _briefing(settings, memory_store, FakeCalendar([event])).collect()

    assert agenda.calendar_connected is True
    assert len(agenda.meetings) == 1


def test_meetings_are_sorted_by_start_time(settings, memory_store):
    base = datetime.now().replace(minute=0, second=0, microsecond=0)
    late = Event("Late", base.replace(hour=16))
    early = Event("Early", base.replace(hour=9))

    agenda = _briefing(settings, memory_store, FakeCalendar([late, early])).collect()

    assert [m.title for m in agenda.meetings] == ["Early", "Late"]


def test_a_failing_calendar_is_reported_not_raised(settings, memory_store):
    agenda = _briefing(settings, memory_store, FakeCalendar(boom=True)).collect()

    assert agenda.calendar_connected is False
    assert agenda.calendar_error


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_compose_includes_the_date(settings, memory_store):
    text = _briefing(settings, memory_store).compose()
    assert str(date.today().year) in text


def test_compose_says_calendars_are_not_connected(settings, memory_store):
    text = _briefing(settings, memory_store).compose()
    assert "not connected" in text.lower()


def test_compose_lists_meetings(settings, memory_store):
    event = Event("Standup", datetime.now().replace(hour=9, minute=30))
    text = _briefing(settings, memory_store, FakeCalendar([event])).compose()

    assert "Standup" in text
    assert "09:30" in text


def test_personal_meeting_titles_are_masked(settings, memory_store):
    """The briefing may be delivered to a work channel."""
    event = Event("Therapy", datetime.now().replace(hour=11), is_personal=True)
    text = _briefing(settings, memory_store, FakeCalendar([event])).compose()

    assert "Therapy" not in text
    assert "private" in text.lower()


def test_compose_lists_tasks_and_follow_ups(settings, memory_store):
    memory_store.add_task("ship it", due_date=date.today())
    memory_store.add_follow_up(thread_id="t", subject="contract review",
                               sender="a@b.c", triage_score=12)

    text = _briefing(settings, memory_store).compose()

    assert "ship it" in text
    assert "contract review" in text


def test_quiet_day_still_renders_something_useful(settings, memory_store):
    text = _briefing(settings, memory_store).compose()

    assert "Good morning" in text
    assert len(text.strip()) > 0


def test_overdue_is_called_out_separately(settings, memory_store):
    memory_store.add_task("late", due_date=date.today() - timedelta(days=1))

    text = _briefing(settings, memory_store).compose()

    assert "overdue" in text.lower()


def test_compose_accepts_precollected_agenda(settings, memory_store):
    briefing = _briefing(settings, memory_store)
    agenda = briefing.collect()
    assert briefing.compose(agenda)
