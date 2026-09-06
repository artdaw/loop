"""Routines and notification policy (runtime §7, §8).

Covers P01, P02, P06, P07, P09, P20, P21.
"""

from __future__ import annotations

import datetime as dt

import pytest

from loop.core.clock import UTC, FrozenClock
from loop.core.errors import ApprovalRequired, InvalidInput
from loop.runtime.notify_policy import (
    Candidate,
    Category,
    Decision,
    NotificationManager,
    NotificationPolicy,
    assign_category,
)
from loop.runtime.routines import (
    RoutineService,
    RoutineStatus,
    parse_routine,
)

CAPABILITIES = {"weather.forecast", "notification.propose", "calendar.list"}

COMPLETE = """---
schema_version: 1
id: weekday-departure-weather
title: Weather before leaving
trigger:
  kind: local_schedule
  days: [mon, tue, wed, thu, fri]
  at: "07:30"
  timezone: Europe/Berlin
steps:
  - id: forecast
    capability: weather.forecast
    arguments:
      location_ref: home
  - id: advise
    capability: notification.propose
    depends_on: [forecast]
notification:
  destination: owner:telegram
  category: requested_routine
  mode: each_occurrence
limits:
  model_calls: 1
privacy:
  model_scope: local_only
---

Check before the configured departure time.
"""

NO_LOCATION = COMPLETE.replace("      location_ref: home",
                               "      location_ref: $required")

UNKNOWN_CAPABILITY = COMPLETE.replace("    capability: weather.forecast",
                                      "    capability: airquality.sensor")


@pytest.fixture
def service() -> RoutineService:
    return RoutineService()


def _parse(document: str, **kw):
    routine, problems = parse_routine(document, known_capabilities=CAPABILITIES,
                                      **kw)
    assert problems == [], problems
    return routine


# --------------------------------------------------------------------------- #
# P01 — a complete request activates
# --------------------------------------------------------------------------- #
def test_p01_a_complete_routine_parses(service):
    routine = _parse(COMPLETE)

    assert routine.slug == "weekday-departure-weather"
    assert routine.trigger["at"] == "07:30"
    assert routine.missing == []


def test_p01_saving_alone_does_not_activate(service):
    routine = service.save(_parse(COMPLETE))
    assert routine.is_active is False


def test_p01_activation_records_its_authority(service):
    service.save(_parse(COMPLETE))
    routine = service.activate("weekday-departure-weather",
                               activation_event_id="event-42")

    assert routine.status is RoutineStatus.ACTIVE
    assert routine.activation_event_id == "event-42"


def test_p01_activation_without_an_event_is_refused(service):
    service.save(_parse(COMPLETE))
    with pytest.raises(ApprovalRequired):
        service.activate("weekday-departure-weather", activation_event_id="")


def test_p01_the_rationale_stays_readable(service):
    routine = _parse(COMPLETE)
    assert "before the configured departure time" in routine.rationale


def test_p01_the_destination_is_recorded(service):
    assert _parse(COMPLETE).destination == "owner:telegram"


# --------------------------------------------------------------------------- #
# P02 — a missing location is a question, not a guess
# --------------------------------------------------------------------------- #
def test_p02_a_missing_input_is_detected(service):
    routine = _parse(NO_LOCATION)
    assert [m.name for m in routine.missing] == ["location_ref"]


def test_p02_the_routine_is_still_saved(service):
    routine = service.save(_parse(NO_LOCATION))

    assert service.get("weekday-departure-weather") is not None
    assert routine.status is RoutineStatus.PROPOSED


def test_p02_exactly_one_question_is_asked(service):
    routine = _parse(NO_LOCATION)
    assert len(routine.blocking_questions) == 1


def test_p02_the_question_asks_for_a_location(service):
    routine = _parse(NO_LOCATION)
    assert "location" in routine.blocking_questions[0].lower()


def test_p02_the_timezone_is_not_used_as_a_location(service):
    """Europe/Berlin covers hundreds of places with different weather."""
    routine = _parse(NO_LOCATION)
    question = routine.blocking_questions[0]

    assert "Berlin" not in question
    assert routine.trigger["timezone"] == "Europe/Berlin"   # still known
    assert routine.missing[0].name == "location_ref"        # still missing


def test_p02_an_incomplete_routine_cannot_activate(service):
    service.save(_parse(NO_LOCATION))
    with pytest.raises(InvalidInput) as excinfo:
        service.activate("weekday-departure-weather",
                         activation_event_id="event-1")

    assert "location_ref" in excinfo.value.details["missing"]


def test_p02_supplying_the_input_clears_the_requirement(service):
    routine = _parse(NO_LOCATION, provided_inputs={"location_ref": "Berlin"})
    assert routine.missing == []


# --------------------------------------------------------------------------- #
# P20 — an unknown capability
# --------------------------------------------------------------------------- #
def test_p20_an_unknown_capability_is_detected(service):
    routine = _parse(UNKNOWN_CAPABILITY)

    assert [m.name for m in routine.missing] == ["airquality.sensor"]
    assert routine.missing[0].kind == "capability"


def test_p20_the_proposal_stays_visible(service):
    service.save(_parse(UNKNOWN_CAPABILITY))
    described = service.describe("weekday-departure-weather")

    assert described["active"] is False
    assert described["missing"][0]["name"] == "airquality.sensor"


def test_p20_a_missing_adapter_is_not_a_question(service):
    """No answer from the user would supply a sensor Loop does not have."""
    routine = _parse(UNKNOWN_CAPABILITY)
    assert routine.blocking_questions == []


def test_p20_it_cannot_activate_and_produce_fake_data(service):
    service.save(_parse(UNKNOWN_CAPABILITY))
    with pytest.raises(InvalidInput):
        service.activate("weekday-departure-weather",
                         activation_event_id="event-1")


# --------------------------------------------------------------------------- #
# P21 — broadening scope needs authority
# --------------------------------------------------------------------------- #
def test_p21_a_timing_change_applies_directly(service):
    service.save(_parse(COMPLETE))
    service.activate("weekday-departure-weather", activation_event_id="e1")

    edited = _parse(COMPLETE.replace('at: "07:30"', 'at: "08:00"'))
    applies, broadened = service.review_edit("weekday-departure-weather", edited)

    assert applies is True
    assert broadened == []


def test_p21_a_new_destination_is_a_permission_change(service):
    service.save(_parse(COMPLETE))
    edited = _parse(COMPLETE.replace("destination: owner:telegram",
                                     "destination: owner:email"))

    applies, broadened = service.review_edit("weekday-departure-weather", edited)

    assert applies is False
    assert any("destination" in b for b in broadened)


def test_p21_a_new_tool_is_a_permission_change(service):
    service.save(_parse(COMPLETE))
    edited = _parse(COMPLETE.replace(
        "  - id: advise\n    capability: notification.propose",
        "  - id: peek\n    capability: calendar.list\n"
        "  - id: advise\n    capability: notification.propose"))

    applies, broadened = service.review_edit("weekday-departure-weather", edited)

    assert applies is False
    assert any("tools" in b for b in broadened)


def test_p21_applying_a_broadened_edit_without_authority_is_refused(service):
    service.save(_parse(COMPLETE))
    edited = _parse(COMPLETE.replace("destination: owner:telegram",
                                     "destination: owner:email"))

    with pytest.raises(ApprovalRequired):
        service.apply_edit("weekday-departure-weather", edited)


def test_p21_the_old_scope_stays_effective_meanwhile(service):
    service.save(_parse(COMPLETE))
    service.activate("weekday-departure-weather", activation_event_id="e1")
    edited = _parse(COMPLETE.replace("destination: owner:telegram",
                                     "destination: owner:email"))

    with pytest.raises(ApprovalRequired):
        service.apply_edit("weekday-departure-weather", edited)

    assert service.get("weekday-departure-weather").destination == "owner:telegram"


def test_p21_a_broadened_edit_applies_with_fresh_authority(service):
    service.save(_parse(COMPLETE))
    service.activate("weekday-departure-weather", activation_event_id="e1")
    edited = _parse(COMPLETE.replace("destination: owner:telegram",
                                     "destination: owner:email"))

    updated = service.apply_edit("weekday-departure-weather", edited,
                                 authority_event_id="e2")

    assert updated.destination == "owner:email"


def test_resume_keeps_the_previously_approved_scope(service):
    service.save(_parse(COMPLETE))
    service.activate("weekday-departure-weather", activation_event_id="e1")
    service.pause("weekday-departure-weather")

    resumed = service.resume("weekday-departure-weather")

    assert resumed.is_active is True
    assert resumed.activation_event_id == "e1"


def test_resuming_a_never_activated_routine_is_refused(service):
    service.save(_parse(COMPLETE))
    with pytest.raises(ApprovalRequired):
        service.resume("weekday-departure-weather")


def test_an_invalid_document_reports_problems():
    routine, problems = parse_routine("---\nid: x\n---\n",
                                      known_capabilities=CAPABILITIES)
    assert routine is None
    assert problems


# --------------------------------------------------------------------------- #
# Notification policy
# --------------------------------------------------------------------------- #
def _clock_at(hour: int) -> FrozenClock:
    """A clock at a given *Berlin* hour on a fixed date."""
    from zoneinfo import ZoneInfo

    local = dt.datetime(2026, 9, 7, hour, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    return FrozenClock(local.astimezone(UTC))


def _manager(hour: int) -> NotificationManager:
    return NotificationManager(policy=NotificationPolicy(), clock=_clock_at(hour))


def _candidate(category: Category = Category.DISCRETIONARY, **kw) -> Candidate:
    defaults = {"category": category, "subject_ref": "task:t1",
                "occurrence_key": "occ-1", "why_now": "because"}
    defaults.update(kw)
    return Candidate(**defaults)


def test_quiet_hours_are_detected():
    policy = NotificationPolicy()
    assert policy.is_quiet(_clock_at(23).now()) is True
    assert policy.is_quiet(_clock_at(3).now()) is True
    assert policy.is_quiet(_clock_at(12).now()) is False


# --------------------------------------------------------------------------- #
# P06 — discretionary during quiet hours or over cap
# --------------------------------------------------------------------------- #
def test_p06_a_discretionary_candidate_defers_during_quiet_hours():
    outcome = _manager(23).decide(_candidate())

    assert outcome.decision is Decision.DEFER_TO_DIGEST
    assert "quiet hours" in outcome.reason


def test_p06_the_deferred_message_gets_a_digest_slot():
    outcome = _manager(23).decide(_candidate())
    assert outcome.deliver_at is not None


def test_p06_the_daily_cap_defers_further_messages():
    manager = _manager(12)
    for index in range(3):
        candidate = _candidate(subject_ref=f"task:{index}")
        assert manager.decide(candidate).sends
        manager.record_sent(candidate)

    assert manager.decide(_candidate(subject_ref="task:9")).decision is \
        Decision.DEFER_TO_DIGEST


def test_p06_a_per_subject_cooldown_applies():
    manager = _manager(12)
    first = _candidate(subject_ref="task:t1")
    manager.record_sent(first)

    outcome = manager.decide(_candidate(subject_ref="task:t1"))
    assert outcome.decision is Decision.DEFER_TO_DIGEST


def test_p06_a_discretionary_candidate_is_never_promoted():
    """"Important" is not a category an agent can claim."""
    manager = _manager(23)
    outcome = manager.decide(_candidate(why_now="this is urgent!"))

    assert outcome.decision is not Decision.SEND


def test_p06_an_expired_candidate_is_not_sent():
    manager = _manager(12)
    expired = _candidate(expires_at=_clock_at(11).now())

    assert manager.decide(expired).decision is Decision.EXPIRE


# --------------------------------------------------------------------------- #
# P07 — explicit reminders honour their time
# --------------------------------------------------------------------------- #
def test_p07_an_explicit_reminder_sends_during_quiet_hours():
    outcome = _manager(23).decide(_candidate(Category.REMINDER))

    assert outcome.decision is Decision.SEND
    assert "explicit authority" in outcome.reason


def test_p07_a_reply_is_never_discretionary():
    assert _manager(23).decide(_candidate(Category.REPLY)).sends is True


def test_p07_an_approval_result_sends_immediately():
    assert _manager(2).decide(_candidate(Category.APPROVAL)).sends is True


def test_p07_an_activated_routine_output_sends_at_its_time():
    assert _manager(23).decide(_candidate(Category.REQUESTED_ROUTINE)).sends


def test_p07_reminders_do_not_count_against_the_discretionary_cap():
    manager = _manager(12)
    for _ in range(5):
        reminder = _candidate(Category.REMINDER)
        assert manager.decide(reminder).sends
        manager.record_sent(reminder)

    assert manager.decide(_candidate(Category.DISCRETIONARY,
                                     subject_ref="other")).sends


# --------------------------------------------------------------------------- #
# Category assignment
# --------------------------------------------------------------------------- #
def test_a_user_request_becomes_a_reminder():
    assert assign_category(requested_by_user=True) is Category.REMINDER


def test_an_agent_idea_becomes_discretionary():
    assert assign_category(requested_by_user=False) is Category.DISCRETIONARY


def test_an_activated_routines_own_output_is_requested():
    assert assign_category(requested_by_user=False,
                           from_activated_routine=True) is \
        Category.REQUESTED_ROUTINE


def test_activation_does_not_exempt_unrelated_suggestions():
    """Only the routine's own scheduled output is non-discretionary."""
    assert assign_category(requested_by_user=False) is Category.DISCRETIONARY


# --------------------------------------------------------------------------- #
# P09 — rescheduled meetings
# --------------------------------------------------------------------------- #
def test_p09_an_old_occurrence_is_cancelled_when_the_meeting_moves():
    pending = [_candidate(Category.REMINDER, subject_ref="event:m1",
                          occurrence_key="09:00"),
               _candidate(Category.REMINDER, subject_ref="event:m2",
                          occurrence_key="11:00")]

    kept, cancelled = NotificationManager.supersede(
        pending, subject_ref="event:m1", new_occurrence_key="14:00")

    assert [c.occurrence_key for c in cancelled] == ["09:00"]
    assert [c.subject_ref for c in kept] == ["event:m2"]


def test_p09_unrelated_subjects_are_untouched():
    pending = [_candidate(Category.REMINDER, subject_ref="event:m2",
                          occurrence_key="11:00")]
    kept, cancelled = NotificationManager.supersede(
        pending, subject_ref="event:m1", new_occurrence_key="14:00")

    assert cancelled == []
    assert len(kept) == 1


def test_p09_the_same_occurrence_is_not_cancelled():
    pending = [_candidate(Category.REMINDER, subject_ref="event:m1",
                          occurrence_key="09:00")]
    kept, cancelled = NotificationManager.supersede(
        pending, subject_ref="event:m1", new_occurrence_key="09:00")

    assert cancelled == []
    assert len(kept) == 1


# --------------------------------------------------------------------------- #
# Ranking and scoped suppression
# --------------------------------------------------------------------------- #
def test_ranking_prefers_the_soonest_expiry():
    soon = _candidate(expires_at=_clock_at(13).now())
    later = _candidate(expires_at=_clock_at(20).now())

    assert _manager(12).rank([later, soon])[0] is soon


def test_ranking_prefers_the_current_project():
    other = _candidate(project_ref="other")
    current = _candidate(project_ref="loop")

    ranked = _manager(12).rank([other, current], current_project="loop")
    assert ranked[0] is current


def test_ranking_is_deterministic():
    candidates = [_candidate(subject_ref=f"t{i}") for i in range(4)]
    manager = _manager(12)

    assert manager.rank(list(candidates)) == manager.rank(list(candidates))


def test_a_suppressed_scope_blocks_delivery():
    manager = _manager(12)
    manager.suppress_scope("commute", until=_clock_at(20).now())

    outcome = manager.decide(_candidate(scope="commute.weather"))
    assert outcome.decision is Decision.SUPPRESS


def test_an_unrelated_scope_is_unaffected():
    manager = _manager(12)
    manager.suppress_scope("commute", until=_clock_at(20).now())

    assert manager.decide(_candidate(scope="medication")).sends is True
