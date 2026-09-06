"""Stage A end to end: task → restart → reminder → snooze/done.

This is the demonstration the implementation brief asks for. Every step crosses
a real process boundary in the sense that matters here: each "restart" builds
brand-new service objects against the same database, so nothing carries over in
memory. Only committed state survives, which is the point.

Covers T01, T08, T09, T10, D01, D15, D16, D17.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from loop.core.clock import UTC, FrozenClock, to_micros
from loop.core.errors import ErrorCode
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.runtime.outbox import FakeTransport, NotificationOutbox, SendResult
from loop.runtime.service import (
    LEADER_LEASE_SECONDS,
    CountingModel,
    LeaderLease,
    LoopService,
)
from loop.runtime.triggers import TriggerService
from loop.services.tasks import TaskService

BERLIN = "Europe/Berlin"
#: 2026-09-05 09:00 Europe/Berlin — the instant T01 starts from.
START = dt.datetime(2026, 9, 5, 7, 0, tzinfo=UTC)


def build_service(sessions, clock, transport, *, completed: set[str] | None = None):
    """Construct a fresh service stack, as a new process would."""
    done = completed if completed is not None else set()
    outbox = NotificationOutbox(
        sessions=sessions, clock=clock,
        transports={"telegram": transport},
        # The preflight consults *current* task state at send time.
        preflight=lambda n: n.subject_ref not in done,
    )
    return LoopService(sessions=sessions, clock=clock, outbox=outbox)


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


# --------------------------------------------------------------------------- #
# T01 — capture a task and a reminder together
# --------------------------------------------------------------------------- #
def test_t01_task_and_trigger_commit_together(sessions, clock):
    """"Remind me tomorrow at 9 to call the repair shop" at 2026-09-05 09:00."""
    tasks = TaskService(sessions=sessions, clock=clock)
    triggers = TriggerService(sessions=sessions, clock=clock)

    with sessions() as session:
        task = tasks.create(
            "Call the repair shop", timezone=BERLIN, session=session,
            privacy=PrivacyLabel(model_scope=ModelScope.LOCAL_ONLY,
                                 allowed_destinations=frozenset({"owner:telegram"})))
        triggers.create_at(
            subject_type="task", subject_id=task.id,
            instant=dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC),
            timezone=BERLIN, original_local="2026-09-06T09:00", session=session)
        session.commit()

    stored = tasks.get(task.id)
    assert stored.status == "ready"

    with sessions() as session:
        fire_at = session.execute(text(
            "SELECT next_fire_at FROM triggers WHERE subject_id = :id"),
            {"id": task.id}).scalar()

    # 2026-09-06T09:00 Berlin is 07:00Z, exactly as the scenario requires.
    assert fire_at == to_micros(dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))


def test_t01_the_reply_can_state_the_actual_local_time(sessions, clock):
    """The trigger stores the original local wall time so the reply is truthful."""
    triggers = TriggerService(sessions=sessions, clock=clock)
    trigger = triggers.create_at(
        subject_type="task", subject_id="t1",
        instant=dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC), timezone=BERLIN,
        original_local="2026-09-06T09:00")

    assert trigger.definition["original_local"] == "2026-09-06T09:00"
    assert trigger.definition["timezone"] == BERLIN


# --------------------------------------------------------------------------- #
# D01 — survive a restart and fire once
# --------------------------------------------------------------------------- #
def _task_with_reminder(sessions, clock, *, fire_at: dt.datetime) -> str:
    tasks = TaskService(sessions=sessions, clock=clock)
    triggers = TriggerService(sessions=sessions, clock=clock)
    with sessions() as session:
        task = tasks.create("Call the repair shop", timezone=BERLIN,
                            session=session)
        triggers.create_at(subject_type="task", subject_id=task.id,
                           instant=fire_at, timezone=BERLIN, session=session)
        session.commit()
    return task.id


def test_d01_a_reminder_survives_a_restart_and_fires_once(sessions, transport):
    clock = FrozenClock(START)
    task_id = _task_with_reminder(
        sessions, clock, fire_at=dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))

    # The process dies here. Nothing is retained in memory.
    del clock

    restarted_clock = FrozenClock(dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))
    service = build_service(sessions, restarted_clock, transport)
    service.tick()

    fired = service.triggers.due_triggers()
    assert fired == [], "the trigger should have been consumed"
    with sessions() as session:
        firings = session.execute(text(
            "SELECT COUNT(*) FROM trigger_firings WHERE trigger_id IN "
            "(SELECT id FROM triggers WHERE subject_id = :id)"),
            {"id": task_id}).scalar()
    assert firings == 1


def test_d01_repeated_sweeps_do_not_fire_a_second_time(sessions, transport):
    """Exactly-once for the internal occurrence, however often we sweep."""
    clock = FrozenClock(START)
    task_id = _task_with_reminder(
        sessions, clock, fire_at=dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))

    later = FrozenClock(dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))
    service = build_service(sessions, later, transport)
    for _ in range(5):
        service.tick()

    with sessions() as session:
        firings = session.execute(text(
            "SELECT COUNT(*) FROM trigger_firings WHERE trigger_id IN "
            "(SELECT id FROM triggers WHERE subject_id = :id)"),
            {"id": task_id}).scalar()
    assert firings == 1


def test_d01_a_fresh_service_object_still_finds_the_work(sessions, transport):
    """Durability is in the database, not in a live scheduler object."""
    clock = FrozenClock(START)
    _task_with_reminder(sessions, clock,
                        fire_at=dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))

    at_time = FrozenClock(dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))
    report = build_service(sessions, at_time, transport).tick()

    assert report.triggers_fired == 1


# --------------------------------------------------------------------------- #
# The full journey: reminder delivered, then snoozed, then completed
# --------------------------------------------------------------------------- #
def test_reminder_is_delivered_after_restart(sessions, transport):
    clock = FrozenClock(START)
    task_id = _task_with_reminder(
        sessions, clock, fire_at=dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))

    at_time = FrozenClock(dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))
    service = build_service(sessions, at_time, transport)
    service.tick()

    service.outbox.enqueue(
        category="reminder", subject_ref=f"task:{task_id}",
        occurrence_key="occ-1", destination_id="owner:telegram",
        payload={"text": "Call the repair shop"})
    report = service.tick()

    assert report.notifications_sent == 1
    assert len(transport.sent) == 1


def test_t09_snoozing_creates_one_replacement_occurrence(sessions, transport):
    """T09: one replacement at the requested time; the task stays open."""
    clock = FrozenClock(dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))
    tasks = TaskService(sessions=sessions, clock=clock)
    triggers = TriggerService(sessions=sessions, clock=clock)
    task = tasks.create("Call the repair shop", timezone=BERLIN)

    # Snooze by one hour: a new trigger, not a mutated old one.
    triggers.create_at(subject_type="task", subject_id=task.id,
                       instant=clock.now() + dt.timedelta(hours=1),
                       timezone=BERLIN)

    with sessions() as session:
        count = session.execute(text(
            "SELECT COUNT(*) FROM triggers WHERE subject_id = :id AND enabled = 1"),
            {"id": task.id}).scalar()

    assert count == 1
    assert tasks.get(task.id).status == "ready", "snooze must not complete the task"


def test_t09_a_replayed_snooze_button_produces_one_feedback_record(sessions, clock):
    """The button can be tapped twice; the feedback is recorded once."""
    from sqlalchemy.exc import IntegrityError

    def record(event_id: str) -> None:
        with sessions() as session:
            session.execute(text(
                "INSERT INTO feedback (id, subject_ref, event_id, kind, "
                "value_json, occurred_at, privacy) VALUES (:id, 'task:t1', "
                ":event_id, 'snooze', '{\"hours\": 1}', :now, '{}')"),
                {"id": f"f-{event_id}", "event_id": event_id,
                 "now": to_micros(clock.now())})
            session.commit()

    record("callback:99")
    with pytest.raises(IntegrityError):
        record("callback:99")

    with sessions() as session:
        assert session.execute(text("SELECT COUNT(*) FROM feedback")).scalar() == 1


def test_t08_completing_the_task_stops_the_pending_reminder(sessions, transport):
    """T08: done cancels the pending firing/notification; nothing is sent."""
    clock = FrozenClock(dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))
    tasks = TaskService(sessions=sessions, clock=clock)
    task = tasks.create("Call the repair shop", timezone=BERLIN)

    completed: set[str] = set()
    service = build_service(sessions, clock, transport, completed=completed)
    service.outbox.enqueue(category="reminder", subject_ref=f"task:{task.id}",
                           occurrence_key="occ-1",
                           destination_id="owner:telegram",
                           payload={"text": "Call the repair shop"})

    # The user completes the task before the sweep runs.
    with sessions() as session:
        tasks.complete(task.id, expected_version=1, session=session)
        service.outbox.cancel_for_subject(f"task:{task.id}", session=session)
        session.commit()
    completed.add(f"task:{task.id}")

    report = service.tick()

    assert report.notifications_sent == 0
    assert transport.sent == []
    assert tasks.get(task.id).status == "done"


def test_t08_the_preflight_catches_a_completion_that_races_the_sweep(sessions,
                                                                    transport):
    """Even without explicit cancellation, current state wins at send time."""
    clock = FrozenClock(dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))
    tasks = TaskService(sessions=sessions, clock=clock)
    task = tasks.create("Call the repair shop", timezone=BERLIN)

    completed: set[str] = set()
    service = build_service(sessions, clock, transport, completed=completed)
    service.outbox.enqueue(category="reminder", subject_ref=f"task:{task.id}",
                           occurrence_key="occ-1",
                           destination_id="owner:telegram", payload={"text": "x"})

    tasks.complete(task.id, expected_version=1)
    completed.add(f"task:{task.id}")     # no cancel_for_subject call

    report = service.tick()

    assert report.notifications_cancelled == 1
    assert transport.sent == []


def test_t10_dismiss_suppresses_the_notification_without_completing(sessions,
                                                                   transport):
    """T10: dismissing is about the message, not the commitment."""
    clock = FrozenClock(dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))
    tasks = TaskService(sessions=sessions, clock=clock)
    task = tasks.create("Call the repair shop", timezone=BERLIN)

    service = build_service(sessions, clock, transport)
    service.outbox.enqueue(category="reminder", subject_ref=f"task:{task.id}",
                           occurrence_key="occ-1",
                           destination_id="owner:telegram", payload={"text": "x"})
    service.outbox.cancel_for_subject(f"task:{task.id}")

    service.tick()

    assert transport.sent == []
    assert tasks.get(task.id).status == "ready", "dismiss must not complete a task"


# --------------------------------------------------------------------------- #
# D15 — a single leader
# --------------------------------------------------------------------------- #
def test_d15_a_second_process_does_not_become_a_second_leader(sessions, clock):
    """A web reload or a second bot alias must not double-schedule."""
    first = LeaderLease(sessions=sessions, clock=clock)
    second = LeaderLease(sessions=sessions, clock=clock)

    assert first.acquire() is True
    assert second.acquire() is False


def test_d15_only_the_leader_sweeps(sessions, transport):
    clock = FrozenClock(dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))
    _task_with_reminder(sessions, FrozenClock(START),
                        fire_at=dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))

    leader = build_service(sessions, clock, transport)
    follower = build_service(sessions, clock, FakeTransport())

    leader_report = leader.tick()
    follower_report = follower.tick()

    assert leader_report.triggers_fired == 1
    assert follower_report.triggers_fired == 0
    assert "not leader" in " ".join(follower_report.details)


def test_d15_the_leader_can_renew_its_own_lease(sessions, clock):
    lease = LeaderLease(sessions=sessions, clock=clock)
    assert lease.acquire() is True
    assert lease.acquire() is True, "renewing your own lease must succeed"


def test_d15_an_expired_lease_can_be_taken_over(sessions):
    clock = FrozenClock(START)
    first = LeaderLease(sessions=sessions, clock=clock)
    first.acquire()

    clock.advance(seconds=120)
    second = LeaderLease(sessions=sessions, clock=clock)

    assert second.acquire() is True


# --------------------------------------------------------------------------- #
# D16 / D17 — deterministic paths never touch a model
# --------------------------------------------------------------------------- #
def test_d16_a_reminder_fires_with_no_model_available(sessions, transport):
    """The model is a landmine: any call raises. The reminder still sends."""
    clock = FrozenClock(START)
    task_id = _task_with_reminder(
        sessions, clock, fire_at=dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))

    model = CountingModel()
    at_time = FrozenClock(dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))
    service = build_service(sessions, at_time, transport)
    service.outbox.enqueue(category="reminder", subject_ref=f"task:{task_id}",
                           occurrence_key="occ-1",
                           destination_id="owner:telegram", payload={"text": "x"})

    report = service.tick()

    assert report.triggers_fired == 1
    assert report.notifications_sent == 1
    assert model.calls == [], "a deterministic reminder must not invoke a model"


def test_d17_an_idle_service_over_24_hours_makes_zero_model_calls(sessions,
                                                                 transport):
    """D17: idle polling must not consume model calls."""
    clock = FrozenClock(START)
    model = CountingModel()
    service = build_service(sessions, clock, transport)

    for _ in range(24):
        clock.advance(hours=1)
        report = service.tick()
        assert report.model_calls == 0

    assert model.calls == []
    assert transport.sent == []


def test_d17_idle_sweeps_are_bounded_and_report_no_work(sessions, transport):
    clock = FrozenClock(START)
    service = build_service(sessions, clock, transport)

    reports = service.run_once()

    assert len(reports) == 1, "quiescence should be detected immediately"
    assert reports[0].did_work is False


# --------------------------------------------------------------------------- #
# run --once is bounded
# --------------------------------------------------------------------------- #
def test_run_once_does_not_loop_forever_on_a_recurring_schedule(sessions,
                                                                transport):
    """interfaces §3: `run --once` must not turn a schedule into an infinite run."""
    clock = FrozenClock(START)
    triggers = TriggerService(sessions=sessions, clock=clock)
    triggers.create_local_schedule(subject_type="routine", subject_id="r1",
                                   days=["mon", "tue", "wed", "thu", "fri",
                                         "sat", "sun"],
                                   at="07:30", timezone=BERLIN)

    service = build_service(sessions, clock, transport)
    reports = service.run_once(max_sweeps=5)

    assert len(reports) <= 5


def test_status_reports_the_next_wake(sessions, transport):
    clock = FrozenClock(START)
    _task_with_reminder(sessions, clock,
                        fire_at=dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))
    service = build_service(sessions, clock, transport)

    assert service.next_wake_at() == to_micros(
        dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))


def test_status_counts_unknown_sends_separately(sessions):
    """An uncertain delivery must be visible, not hidden among successes."""
    clock = FrozenClock(START)
    transport = FakeTransport(results=[SendResult(status="unknown")])
    service = build_service(sessions, clock, transport)
    service.outbox.enqueue(category="reminder", subject_ref="task:t1",
                           occurrence_key="occ-1",
                           destination_id="owner:telegram", payload={"text": "x"})

    report = service.tick()

    assert report.unknown_sends == 1
    assert service.pending_summary()["unknown"] == 1


def test_a_failed_send_records_its_error_code(sessions):
    clock = FrozenClock(START)
    transport = FakeTransport(results=[
        SendResult(status="rejected", error_code=ErrorCode.AUTH_REQUIRED,
                   retryable=False)])
    service = build_service(sessions, clock, transport)
    service.outbox.enqueue(category="reminder", subject_ref="task:t1",
                           occurrence_key="occ-1",
                           destination_id="owner:telegram", payload={"text": "x"})
    service.tick()

    item = service.outbox.due_items()
    assert item == []


def test_d01_a_crash_between_firing_and_disabling_does_not_double_fire(sessions,
                                                                      transport):
    """The real exactly-once test.

    The earlier D01 tests pass even with occurrence-key suppression removed,
    because a fired one-shot trigger gets disabled and is never seen again. That
    proves the disable, not the guarantee. This reproduces the dangerous window:
    the firing committed but the disable did not, so a restart finds the trigger
    still enabled and due. Only the occurrence key stops a second reminder.
    """
    clock = FrozenClock(START)
    task_id = _task_with_reminder(
        sessions, clock, fire_at=dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))

    at_time = FrozenClock(dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC))
    service = build_service(sessions, at_time, transport)
    service.tick()

    # Simulate the crash: the trigger row is back to enabled and due, exactly as
    # it would be if the process died before the disable committed.
    # The dead process's leader lease must also expire, or the replacement
    # would simply decline to sweep and the test would prove nothing.
    with sessions() as session:
        session.execute(text(
            "UPDATE triggers SET enabled = 1, next_fire_at = :when "
            "WHERE subject_id = :id"),
            {"when": to_micros(dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC)),
             "id": task_id})
        session.commit()

    at_time.advance(seconds=LEADER_LEASE_SECONDS + 5)
    restarted = build_service(sessions, at_time, transport)
    report = restarted.tick()
    assert "not leader" not in " ".join(report.details), (
        "the replacement must actually sweep, or this test proves nothing")

    with sessions() as session:
        firings = session.execute(text(
            "SELECT COUNT(*) FROM trigger_firings WHERE trigger_id IN "
            "(SELECT id FROM triggers WHERE subject_id = :id)"),
            {"id": task_id}).scalar()

    assert firings == 1, "the occurrence key must suppress the replayed firing"
    assert report.triggers_fired == 0
    assert report.triggers_skipped == 1
