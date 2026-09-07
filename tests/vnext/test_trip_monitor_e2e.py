"""Scoped trip monitoring (travel §7, M6).

`plan_monitoring` produced checkpoints and nothing scheduled them. These tests
run the binding through `build_application()`: a plan becomes durable triggers,
a fired trigger becomes a durable job carrying the *scope the owner approved*,
and cancelling stops both the remaining checks and the notices they would have
produced.

"Scoped" is the property under test throughout — scoped to the requested
checks, to a selected option, to the future, and to a trip that is still on.
"""

from __future__ import annotations

import datetime as dt

import pytest

from loop.app import build_application
from loop.capabilities.travel.monitor import (
    ChangeKind,
    ChangeReport,
    Check,
    plan_monitoring,
)
from loop.core.clock import UTC
from loop.core.errors import InvalidInput, Unavailable
from loop.runtime.trip_monitor import TRIP_JOB_KIND, TRIP_SUBJECT

TRIP = "trip-lisbon"
#: conftest's frozen NOW is 2026-09-05 07:00Z; departure is comfortably ahead.
DEPARTURE = dt.datetime(2026, 9, 20, 9, 0)
TRAVEL_DAYS = [dt.date(2026, 9, 20), dt.date(2026, 9, 21), dt.date(2026, 9, 22)]


@pytest.fixture
def app(settings, clock):
    return build_application(settings, clock=clock)


def a_plan(*, checks: list[str] | None = None, now: dt.datetime | None = None,
           option_id: str = "opt-1"):
    return plan_monitoring(
        trip_id=TRIP, revision_id="rev-1", option_id=option_id,
        requested_checks=checks, departure_local=DEPARTURE,
        travel_days=TRAVEL_DAYS, timezone="Europe/Lisbon",
        now_utc=now or dt.datetime(2026, 9, 5, 7, 0, tzinfo=UTC),
        owner_timezone="Europe/Berlin")


# --------------------------------------------------------------------------- #
# Activation
# --------------------------------------------------------------------------- #
def test_activating_turns_checkpoints_into_durable_triggers(app):
    report = app.trip_monitor.activate(a_plan(), activation_event_id="evt-1")

    assert report.monitoring
    assert report.scheduled == len(a_plan().checkpoints)
    triggers = app.trip_monitor.pending_checks(TRIP)
    assert len(triggers) == report.scheduled
    assert all(t.subject_type == TRIP_SUBJECT for t in triggers)
    assert all(t.kind == "at" for t in triggers)


def test_monitoring_survives_a_restart(settings, clock, app):
    app.trip_monitor.activate(a_plan(checks=["closure"]),
                              activation_event_id="evt-1")

    restarted = build_application(settings, clock=clock)

    state = restarted.trip_monitor.state_for(TRIP)
    assert state is not None
    assert state.active
    assert state.checks == (Check.CLOSURE,)
    assert restarted.trip_monitor.pending_checks(TRIP)


def test_activation_must_name_the_request_that_authorised_it(app):
    """Monitoring is authority; an anonymous activation records no one."""
    with pytest.raises(InvalidInput):
        app.trip_monitor.activate(a_plan(), activation_event_id="")


def test_a_check_that_cannot_be_performed_stops_activation(app):
    """Silently monitoring the subset looks identical to finding nothing."""
    plan = a_plan(checks=["closure", "visa_requirements"])

    with pytest.raises(Unavailable) as raised:
        app.trip_monitor.activate(plan, activation_event_id="evt-1")

    assert "visa_requirements" in str(raised.value)
    assert app.trip_monitor.pending_checks(TRIP) == []
    assert app.trip_monitor.state_for(TRIP) is None


def test_monitoring_requires_a_selected_option(app):
    with pytest.raises(InvalidInput):
        a_plan(option_id="")


def test_checkpoints_already_past_are_not_fired_on_activation(app):
    """Booking three days out must not fire the seven-day check immediately."""
    late = dt.datetime(2026, 9, 19, 7, 0, tzinfo=UTC)
    plan = a_plan(now=late)

    report = app.trip_monitor.activate(plan, activation_event_id="evt-1")

    assert report.scheduled == len(plan.checkpoints)
    assert all(t.next_fire_at > int(late.timestamp() * 1_000_000)
               for t in app.trip_monitor.pending_checks(TRIP))


def test_a_checkpoint_that_passed_between_planning_and_approval_is_skipped(app,
                                                                          clock):
    plan = a_plan()
    clock.set(dt.datetime(2026, 9, 14, 8, 0, tzinfo=UTC))   # past the 7-day mark

    report = app.trip_monitor.activate(plan, activation_event_id="evt-1")

    assert report.skipped_past >= 1
    assert report.scheduled == len(plan.checkpoints) - report.skipped_past


def test_the_owners_timezone_assumption_is_reported_not_hidden(app):
    report = app.trip_monitor.activate(a_plan(), activation_event_id="evt-1")

    assert any("timezone" in note for note in report.assumptions)


# --------------------------------------------------------------------------- #
# Firing: the scope travels with the work
# --------------------------------------------------------------------------- #
def test_a_due_check_queues_a_job_carrying_the_approved_scope(app, clock):
    app.trip_monitor.activate(a_plan(checks=["closure", "weather"]),
                              activation_event_id="evt-1")
    first = min(t.next_fire_at for t in app.trip_monitor.pending_checks(TRIP))
    clock.set(dt.datetime.fromtimestamp(first / 1_000_000, UTC))

    report = app.service.tick()

    assert report.triggers_fired == 1
    job = app.jobs.claim("worker", kinds=[TRIP_JOB_KIND])
    assert job is not None
    assert job.payload["trip_id"] == TRIP
    assert job.payload["revision_id"] == "rev-1"
    assert job.payload["option_id"] == "opt-1"
    # A worker that re-derived the checks could widen them; this cannot.
    assert sorted(job.payload["checks"]) == ["closure", "weather"]


def test_firing_a_trip_check_calls_no_model(app, clock):
    app.trip_monitor.activate(a_plan(), activation_event_id="evt-1")
    first = min(t.next_fire_at for t in app.trip_monitor.pending_checks(TRIP))
    clock.set(dt.datetime.fromtimestamp(first / 1_000_000, UTC))

    app.service.tick()

    assert app.model_gateway.audit.cloud_calls == 0
    assert app.model_gateway.audit.local_calls == 0


def test_the_same_checkpoint_cannot_queue_two_jobs(app, clock):
    app.trip_monitor.activate(a_plan(), activation_event_id="evt-1")
    first = min(t.next_fire_at for t in app.trip_monitor.pending_checks(TRIP))
    clock.set(dt.datetime.fromtimestamp(first / 1_000_000, UTC))

    app.service.tick()
    app.service.tick()

    assert app.service.pending_summary()["jobs"] == 1


def test_routine_and_trip_triggers_do_not_see_each_others_work(app, clock):
    """One sweep serves both; neither dispatcher may claim the other's subject."""
    from loop.runtime.routine_dispatch import ROUTINE_KIND

    app.trip_monitor.activate(a_plan(), activation_event_id="evt-1")
    first = min(t.next_fire_at for t in app.trip_monitor.pending_checks(TRIP))
    clock.set(dt.datetime.fromtimestamp(first / 1_000_000, UTC))
    app.service.tick()

    assert app.jobs.claim("w", kinds=[ROUTINE_KIND]) is None
    assert app.jobs.claim("w", kinds=[TRIP_JOB_KIND]) is not None


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #
def test_cancelling_stops_the_remaining_checks(app):
    app.trip_monitor.activate(a_plan(), activation_event_id="evt-1")
    assert app.trip_monitor.pending_checks(TRIP)

    disabled = app.trip_monitor.deactivate(TRIP)

    assert disabled > 0
    assert app.trip_monitor.pending_checks(TRIP) == []
    assert app.trip_monitor.state_for(TRIP).active is False


def test_cancelling_drops_the_notices_it_was_about_to_send(app):
    """A cancelled trip that still reports a delayed train (TR19)."""
    report = ChangeReport(ChangeKind.TRANSPORT_TIME,
                          "train 40 minutes later",
                          before="0", after="40")
    app.trip_monitor.notices.record(trip_id=TRIP, subject="leg-1", report=report)
    assert not app.trip_monitor.notices.should_notify(
        trip_id=TRIP, subject="leg-1", report=report)

    app.trip_monitor.deactivate(TRIP)

    assert app.trip_monitor.notices.sent == {}


def test_a_cancelled_trip_whose_trigger_still_fires_queues_nothing(app, clock):
    """Belt and braces: the dispatcher rechecks the monitoring state."""
    app.trip_monitor.activate(a_plan(), activation_event_id="evt-1")
    triggers = app.trip_monitor.pending_checks(TRIP)
    first = min(t.next_fire_at for t in triggers)
    # Deactivate the *state* only, leaving the trigger enabled.
    with app.sessions() as session:
        from sqlalchemy import text
        session.execute(text("UPDATE trip_monitoring SET active = 0 "
                             "WHERE trip_id = :t"), {"t": TRIP})
        session.commit()
    clock.set(dt.datetime.fromtimestamp(first / 1_000_000, UTC))

    app.service.tick()

    assert app.service.pending_summary()["jobs"] == 0


def test_an_unmonitored_trip_check_queues_nothing(app, clock):
    app.triggers.create_at(subject_type=TRIP_SUBJECT, subject_id="trip-unknown",
                           instant=clock.now(), timezone="UTC")

    app.service.tick()

    assert app.service.pending_summary()["jobs"] == 0


def test_each_checkpoint_produces_its_own_job(app, clock):
    """Keyed on the trip alone, the 7-day and 1-day checks become one job.

    Trigger-firing dedup does not catch this: each checkpoint is its own
    one-shot trigger, so they never collide there — the collision would happen
    on the *job's* dedupe key, silently, with the second check simply never
    running.
    """
    app.trip_monitor.activate(a_plan(), activation_event_id="evt-1")
    moments = sorted(t.next_fire_at
                     for t in app.trip_monitor.pending_checks(TRIP))[:2]

    queued = []
    for moment in moments:
        clock.set(dt.datetime.fromtimestamp(moment / 1_000_000, UTC))
        app.service.tick()
        job = app.jobs.claim("worker", kinds=[TRIP_JOB_KIND])
        assert job is not None, "a checkpoint produced no job"
        queued.append(job.payload["reason"])
        app.jobs.succeed(job)

    assert len(queued) == 2
    assert queued[0] != queued[1], "two checkpoints collapsed into one job"


def test_a_fired_routine_does_not_queue_a_trip_check(app, clock):
    """Each dispatcher must ignore the other's subject type entirely."""
    from loop.runtime.routine_dispatch import ROUTINE_KIND
    from loop.runtime.routines import parse_routine

    document = """---
schema_version: 1
id: rain-check
title: Rain check
trigger:
  kind: at
  timezone: Europe/Berlin
steps:
  - capability: weather.prepare
    arguments:
      location_ref: home
notification:
  mode: each_occurrence
  destination: "4242"
---

Once.
"""
    routine, problems = parse_routine(
        document, known_capabilities=app.known_capabilities())
    assert problems == []
    app.routines.save(routine)
    app.routine_scheduler.activate("rain-check", activation_event_id="evt-r")
    app.trip_monitor.activate(a_plan(), activation_event_id="evt-1")

    app.service.tick()          # the routine's `at` trigger is due now

    assert app.jobs.claim("w", kinds=[ROUTINE_KIND]) is not None
    assert app.jobs.claim("w", kinds=[TRIP_JOB_KIND]) is None
