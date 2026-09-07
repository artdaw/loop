"""Notifications and delivery truth (runtime §8).

Covers acceptance D04, D05, D06, T08, T10 and the cancellation half of D11.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from loop.core.clock import to_micros
from loop.core.errors import ErrorCode
from loop.runtime.outbox import FakeTransport, NotificationOutbox, SendResult


def _outbox(sessions, clock, *, transport=None, preflight=None):
    return NotificationOutbox(
        sessions=sessions, clock=clock,
        transports={"telegram": transport or FakeTransport()},
        preflight=preflight,
    )


def _queue(outbox, *, subject="task:t1", occurrence="occ-1", **kw) -> str:
    return outbox.enqueue(category="reminder", subject_ref=subject,
                          occurrence_key=occurrence,
                          destination_id="owner:telegram",
                          payload={"text": "Call the repair shop"}, **kw)


# --------------------------------------------------------------------------- #
# Queueing
# --------------------------------------------------------------------------- #
def test_queueing_creates_a_notification_and_an_outbox_row(sessions, clock):
    outbox = _outbox(sessions, clock)
    notification_id = _queue(outbox)

    assert outbox.get_notification(notification_id).state == "pending"
    assert len(outbox.due_items()) == 1


def test_queued_means_committed_locally_not_sent(sessions, clock):
    """runtime §8: 'queued' is a local fact, never a delivery claim."""
    transport = FakeTransport()
    outbox = _outbox(sessions, clock, transport=transport)
    _queue(outbox)

    assert transport.sent == []


def test_the_same_occurrence_is_queued_once(sessions, clock):
    outbox = _outbox(sessions, clock)

    first = _queue(outbox)
    second = _queue(outbox)

    assert first is not None
    assert second is None
    assert len(outbox.due_items()) == 1


def test_a_different_occurrence_queues_separately(sessions, clock):
    outbox = _outbox(sessions, clock)
    _queue(outbox, occurrence="occ-1")
    _queue(outbox, occurrence="occ-2")

    assert len(outbox.due_items()) == 2


def test_an_unknown_category_is_rejected(sessions, clock):
    outbox = _outbox(sessions, clock)
    with pytest.raises(ValueError):
        outbox.enqueue(category="urgent-alert", subject_ref="s",
                       occurrence_key="o", destination_id="owner:telegram",
                       payload={})


def test_a_future_notification_is_not_due_yet(sessions, clock):
    outbox = _outbox(sessions, clock)
    _queue(outbox, not_before=to_micros(clock.now()) + 3_600_000_000)

    assert outbox.due_items() == []


# --------------------------------------------------------------------------- #
# D04 — crash between the state change and dispatch
# --------------------------------------------------------------------------- #
def test_d04_notification_commits_with_the_causing_state_change(sessions, clock):
    """The notification and the task change share one transaction."""
    outbox = _outbox(sessions, clock)

    with sessions() as session:
        session.execute(text(
            "INSERT INTO tasks (id, version, created_at, updated_at, title, "
            "description, owner, status, priority, timezone, context_json, "
            "privacy) VALUES ('t1', 1, 0, 0, 'Call', '', 'owner', 'ready', "
            "'normal', 'Europe/Berlin', '{}', '{}')"))
        _queue(outbox, session=session)
        session.commit()

    assert len(outbox.due_items()) == 1


def test_d04_a_crash_before_dispatch_leaves_the_notification_pending(sessions, clock):
    """A fresh process finds the work still queued and sends it once."""
    _queue(_outbox(sessions, clock))

    # Simulate a restart: brand-new outbox instance, same database.
    transport = FakeTransport()
    restarted = _outbox(sessions, clock, transport=transport)
    items = restarted.due_items()

    assert len(items) == 1
    assert restarted.dispatch(items[0]) == "delivered"
    assert len(transport.sent) == 1


def test_d04_the_resent_message_keeps_the_same_dedupe_identity(sessions, clock):
    transport = FakeTransport()
    outbox = _outbox(sessions, clock, transport=transport)
    _queue(outbox, subject="task:t1", occurrence="occ-1")

    outbox.dispatch(outbox.due_items()[0])

    assert transport.sent[0]["dedupe_key"] == (
        "reminder:task:t1:occ-1:owner:telegram")


def test_d04_a_delivered_item_is_not_dispatched_again(sessions, clock):
    transport = FakeTransport()
    outbox = _outbox(sessions, clock, transport=transport)
    _queue(outbox)
    outbox.dispatch(outbox.due_items()[0])

    assert outbox.due_items() == []
    assert len(transport.sent) == 1


# --------------------------------------------------------------------------- #
# D05 — definite rejection, then recovery
# --------------------------------------------------------------------------- #
def test_d05_a_retryable_rejection_schedules_a_retry(sessions, clock):
    transport = FakeTransport(results=[
        SendResult(status="rejected", error_code=ErrorCode.RATE_LIMITED,
                   retryable=True)])
    outbox = _outbox(sessions, clock, transport=transport)
    _queue(outbox)

    assert outbox.dispatch(outbox.due_items()[0]) == "retry_wait"


def test_d05_the_retry_eventually_delivers_exactly_once(sessions, clock):
    transport = FakeTransport(results=[
        SendResult(status="rejected", error_code=ErrorCode.RATE_LIMITED,
                   retryable=True),
        SendResult(status="delivered", receipt="msg-9"),
    ])
    outbox = _outbox(sessions, clock, transport=transport)
    _queue(outbox)

    outbox.dispatch(outbox.due_items()[0])
    clock.advance(seconds=60)
    item = outbox.due_items()[0]

    assert outbox.dispatch(item) == "delivered"
    assert outbox.get_item(item.id).provider_receipt == "msg-9"
    assert len(transport.sent) == 2


def test_d05_a_non_retryable_rejection_fails_immediately(sessions, clock):
    transport = FakeTransport(results=[
        SendResult(status="rejected", error_code=ErrorCode.AUTH_REQUIRED,
                   retryable=False)])
    outbox = _outbox(sessions, clock, transport=transport)
    notification_id = _queue(outbox)

    assert outbox.dispatch(outbox.due_items()[0]) == "failed"
    assert outbox.get_notification(notification_id).state == "failed"


def test_d05_an_unconfigured_channel_fails_without_retrying(sessions, clock):
    outbox = NotificationOutbox(sessions=sessions, clock=clock, transports={})
    _queue(outbox)

    assert outbox.dispatch(outbox.due_items()[0]) == "failed"


# --------------------------------------------------------------------------- #
# D06 — uncertain send
# --------------------------------------------------------------------------- #
def test_d06_a_timeout_after_sending_is_recorded_as_unknown(sessions, clock):
    transport = FakeTransport(results=[SendResult(status="unknown")])
    outbox = _outbox(sessions, clock, transport=transport)
    notification_id = _queue(outbox)

    assert outbox.dispatch(outbox.due_items()[0]) == "unknown"
    assert outbox.get_notification(notification_id).state == "unknown"


def test_d06_an_unknown_send_is_never_automatically_resent(sessions, clock):
    """The core of D06: no blind resend, because it may already have arrived."""
    transport = FakeTransport(results=[SendResult(status="unknown")])
    outbox = _outbox(sessions, clock, transport=transport)
    _queue(outbox)
    outbox.dispatch(outbox.due_items()[0])

    clock.advance(hours=6)

    assert outbox.due_items() == [], "an unknown send must not be re-queued"
    assert len(transport.sent) == 1


def test_d06_unknown_is_not_reported_as_sent(sessions, clock):
    transport = FakeTransport(results=[SendResult(status="unknown")])
    outbox = _outbox(sessions, clock, transport=transport)
    notification_id = _queue(outbox)
    outbox.dispatch(outbox.due_items()[0])

    assert outbox.get_notification(notification_id).state != "sent"


def test_d06_a_transport_crash_is_unknown_not_failed(sessions, clock):
    """We cannot know whether the provider received it, so we must not guess."""
    transport = FakeTransport(raises=TimeoutError("connection dropped"))
    outbox = _outbox(sessions, clock, transport=transport)
    _queue(outbox)

    assert outbox.dispatch(outbox.due_items()[0]) == "unknown"


def test_d06_an_unknown_item_carries_the_unknown_effect_code(sessions, clock):
    transport = FakeTransport(results=[SendResult(status="unknown")])
    outbox = _outbox(sessions, clock, transport=transport)
    _queue(outbox)
    item = outbox.due_items()[0]
    outbox.dispatch(item)

    assert outbox.get_item(item.id).last_error_code == "unknown_effect"


# --------------------------------------------------------------------------- #
# T08 / D11 — preflight and cancellation
# --------------------------------------------------------------------------- #
def test_t08_completing_the_task_cancels_the_pending_notification(sessions, clock):
    transport = FakeTransport()
    outbox = _outbox(sessions, clock, transport=transport)
    _queue(outbox, subject="task:t1")

    assert outbox.cancel_for_subject("task:t1") == 1
    assert outbox.due_items() == []
    assert transport.sent == []


def test_t08_the_preflight_suppresses_a_stale_send(sessions, clock):
    """Even if the item was already queued, current state wins at send time."""
    transport = FakeTransport()
    completed = {"task:t1"}
    outbox = _outbox(sessions, clock, transport=transport,
                     preflight=lambda n: n.subject_ref not in completed)
    _queue(outbox, subject="task:t1")

    assert outbox.dispatch(outbox.due_items()[0]) == "cancelled"
    assert transport.sent == []


def test_t08_the_preflight_allows_a_still_valid_send(sessions, clock):
    transport = FakeTransport()
    outbox = _outbox(sessions, clock, transport=transport,
                     preflight=lambda n: True)
    _queue(outbox)

    assert outbox.dispatch(outbox.due_items()[0]) == "delivered"


def test_d11_an_already_delivered_message_is_not_recalled(sessions, clock):
    """A transmitted message is history; cancellation cannot un-send it."""
    outbox = _outbox(sessions, clock)
    _queue(outbox, subject="task:t1")
    item = outbox.due_items()[0]
    outbox.dispatch(item)

    assert outbox.cancel_for_subject("task:t1") == 0
    assert outbox.get_item(item.id).state == "delivered"


def test_an_expired_notification_is_not_sent(sessions, clock):
    transport = FakeTransport()
    outbox = _outbox(sessions, clock, transport=transport)
    _queue(outbox, expires_at=to_micros(clock.now()) + 1_000_000)
    clock.advance(seconds=10)

    assert outbox.dispatch(outbox.due_items()[0]) == "cancelled"
    assert transport.sent == []


# --------------------------------------------------------------------------- #
# Acknowledgement
# --------------------------------------------------------------------------- #
def test_acknowledgement_is_recorded_only_when_it_actually_happens(sessions, clock):
    outbox = _outbox(sessions, clock)
    notification_id = _queue(outbox)
    outbox.dispatch(outbox.due_items()[0])

    assert outbox.get_notification(notification_id).acknowledgement_at is None

    outbox.acknowledge(notification_id)
    assert outbox.get_notification(notification_id).acknowledgement_at is not None


def test_delivery_records_the_provider_receipt(sessions, clock):
    transport = FakeTransport(default=SendResult(status="delivered",
                                                 receipt="tg-4242"))
    outbox = _outbox(sessions, clock, transport=transport)
    _queue(outbox)
    item = outbox.due_items()[0]
    outbox.dispatch(item)

    assert outbox.get_item(item.id).provider_receipt == "tg-4242"


# --------------------------------------------------------------------------- #
# Deferral — what `/snooze` does (M6)
# --------------------------------------------------------------------------- #
def test_deferring_moves_a_pending_message_out_of_the_due_window(sessions, clock):
    outbox = _outbox(sessions, clock)
    _queue(outbox)
    assert len(outbox.due_items()) == 1

    later = to_micros(clock.now()) + 3600 * 1_000_000
    moved = outbox.defer_for_subject("task:t1", until=later)

    assert moved == 1
    assert outbox.due_items() == []


def test_a_delivered_message_is_never_rescheduled(sessions, clock):
    """A sent message is history; moving it would promise a second one."""
    transport = FakeTransport()
    outbox = _outbox(sessions, clock, transport=transport)
    _queue(outbox)
    assert outbox.dispatch(outbox.due_items()[0]) == "delivered"

    moved = outbox.defer_for_subject(
        "task:t1", until=to_micros(clock.now()) + 3600 * 1_000_000)

    assert moved == 0
    assert len(transport.sent) == 1
    assert outbox.due_items() == []


def test_deferring_leaves_another_subjects_message_alone(sessions, clock):
    outbox = _outbox(sessions, clock)
    _queue(outbox, subject="task:t1")
    _queue(outbox, subject="task:t2", occurrence="occ-2")

    outbox.defer_for_subject("task:t1",
                             until=to_micros(clock.now()) + 3600 * 1_000_000)

    remaining = outbox.due_items()
    assert len(remaining) == 1
    notification = outbox.get_notification(remaining[0].notification_id)
    assert notification.subject_ref == "task:t2"


def test_the_notification_itself_moves_with_its_outbox_row(sessions, clock):
    """Otherwise a later policy check still sees the original time."""
    outbox = _outbox(sessions, clock)
    notification_id = _queue(outbox)
    later = to_micros(clock.now()) + 3600 * 1_000_000

    outbox.defer_for_subject("task:t1", until=later)

    with sessions() as session:
        stored = session.execute(
            text("SELECT not_before FROM notifications WHERE id = :id"),
            {"id": notification_id}).scalar()
    assert stored == later
