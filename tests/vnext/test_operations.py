"""Operation ledger and approvals (runtime §10).

Covers A04, A14–A20, A22 and the replay half of LG07.
"""

from __future__ import annotations

import pytest

from loop.core.errors import ApprovalRequired, Conflict, InvalidInput
from loop.core.ids import input_hash
from loop.runtime.operations import (
    SELF_AUTHORISING_ACTIONS,
    OperationLedger,
    operation_key,
)


@pytest.fixture
def ledger(sessions, clock) -> OperationLedger:
    return OperationLedger(sessions=sessions, clock=clock)


def _propose(ledger, *, action="email.send", payload=None, key="k1"):
    return ledger.propose(action=action, target="anna@acme.com",
                          payload=payload or {"body": "hello"},
                          idempotency_key=key)


# --------------------------------------------------------------------------- #
# Stable keys
# --------------------------------------------------------------------------- #
def test_the_key_is_derived_from_persisted_identifiers():
    first = operation_key(root_id="r1", work_item_id="w1", step_id="s1",
                          action="email.send")
    second = operation_key(root_id="r1", work_item_id="w1", step_id="s1",
                           action="email.send")
    assert first == second


def test_the_key_changes_with_the_action():
    assert operation_key(root_id="r1", work_item_id="w1", step_id="s1",
                         action="email.send") != \
        operation_key(root_id="r1", work_item_id="w1", step_id="s1",
                      action="task.create")


def test_the_key_does_not_depend_on_the_clock(ledger, clock):
    """A replay after a restart must reproduce the same key."""
    before = operation_key(root_id="r", work_item_id="w", step_id="s",
                           action="a")
    clock.advance(hours=10)
    assert operation_key(root_id="r", work_item_id="w", step_id="s",
                         action="a") == before


# --------------------------------------------------------------------------- #
# A14 — direct authenticated requests need no second approval
# --------------------------------------------------------------------------- #
def test_a14_a_reminder_request_is_self_authorising(ledger):
    operation = ledger.propose(action="reminder.schedule", target="task:t1",
                               payload={"at": "09:00"}, idempotency_key="k")
    assert operation.status == "authorized"


def test_a14_a_capture_request_is_self_authorising(ledger):
    operation = ledger.propose(action="vault.capture", target="inbox",
                               payload={"body": "a fact"}, idempotency_key="k")
    assert operation.status == "authorized"


def test_a14_no_approval_is_requested_for_a_precise_reversible_action(ledger):
    operation = ledger.propose(action="task.create", target="t1",
                               payload={"title": "x"}, idempotency_key="k")
    ledger.require_authorized(operation)   # does not raise


def test_a15_an_email_send_is_not_self_authorising(ledger):
    """A suggestion to contact someone is not authority to send it."""
    assert "email.send" not in SELF_AUTHORISING_ACTIONS
    assert _propose(ledger).status == "proposed"


def test_a15_an_unapproved_operation_cannot_run(ledger):
    operation = _propose(ledger)
    with pytest.raises(ApprovalRequired):
        ledger.require_authorized(operation)


# --------------------------------------------------------------------------- #
# A04 / LG07 — idempotency and replay
# --------------------------------------------------------------------------- #
def test_a04_the_same_key_returns_the_same_operation(ledger):
    first = _propose(ledger, key="same")
    second = _propose(ledger, key="same")
    assert first.id == second.id


def test_a04_the_same_key_with_a_different_payload_conflicts(ledger):
    _propose(ledger, key="same", payload={"body": "hello"})
    with pytest.raises(Conflict):
        _propose(ledger, key="same", payload={"body": "different"})


def test_lg07_replay_returns_a_committed_result(ledger):
    operation = ledger.propose(action="task.create", target="t1",
                               payload={"title": "x"}, idempotency_key="k")
    ledger.mark_committed(operation, result={"task_id": "t1"})

    replayed = ledger.replay("k")
    assert replayed is not None
    assert replayed.result == {"task_id": "t1"}


def test_lg07_replay_of_an_uncommitted_operation_returns_nothing(ledger):
    _propose(ledger, key="k")
    assert ledger.replay("k") is None


def test_lg07_a_committed_effect_is_not_repeated(ledger):
    """The ledger is consulted before acting; a hit means do nothing."""
    performed: list[str] = []

    def perform(key):
        if ledger.replay(key) is not None:
            return "reused"
        operation = ledger.propose(action="task.create", target="t",
                                   payload={"x": 1}, idempotency_key=key)
        performed.append(key)
        ledger.mark_committed(operation, result={"ok": True})
        return "performed"

    assert perform("k") == "performed"
    assert perform("k") == "reused"
    assert performed == ["k"]


# --------------------------------------------------------------------------- #
# A16 — approval binding
# --------------------------------------------------------------------------- #
def test_a16_an_approval_authorises_its_operation(ledger):
    operation = _propose(ledger)
    approval = ledger.request_approval(operation, destination_id="owner:telegram",
                                       actor_id="owner")

    ledger.resolve_approval(approval.id, resolution="approved", actor_id="owner",
                            current_input_hash=operation.input_hash)

    assert ledger.get(operation.id).status == "authorized"


def test_a16_a_replayed_approval_returns_the_existing_result(ledger):
    operation = _propose(ledger)
    approval = ledger.request_approval(operation, destination_id="owner:telegram",
                                       actor_id="owner")
    ledger.resolve_approval(approval.id, resolution="approved", actor_id="owner",
                            current_input_hash=operation.input_hash)

    again = ledger.resolve_approval(approval.id, resolution="approved",
                                    actor_id="owner",
                                    current_input_hash=operation.input_hash)

    assert again.resolution == "approved"
    assert ledger.get(operation.id).status == "authorized"


def test_a16_a_different_actor_cannot_approve(ledger):
    operation = _propose(ledger)
    approval = ledger.request_approval(operation, destination_id="owner:telegram",
                                       actor_id="owner")

    with pytest.raises(ApprovalRequired):
        ledger.resolve_approval(approval.id, resolution="approved",
                                actor_id="someone-else",
                                current_input_hash=operation.input_hash)


def test_a16_a_changed_payload_invalidates_the_approval(ledger):
    operation = _propose(ledger)
    approval = ledger.request_approval(operation, destination_id="owner:telegram",
                                       actor_id="owner")

    with pytest.raises(Conflict):
        ledger.resolve_approval(approval.id, resolution="approved",
                                actor_id="owner",
                                current_input_hash=input_hash({"body": "edited"}))


def test_a16_an_expired_approval_cannot_execute(ledger, clock):
    operation = _propose(ledger)
    approval = ledger.request_approval(operation, destination_id="owner:telegram",
                                       actor_id="owner", ttl_seconds=60)
    clock.advance(hours=2)

    with pytest.raises(ApprovalRequired):
        ledger.resolve_approval(approval.id, resolution="approved",
                                actor_id="owner",
                                current_input_hash=operation.input_hash)


def test_a16_a_rejection_cancels_the_operation(ledger):
    operation = _propose(ledger)
    approval = ledger.request_approval(operation, destination_id="owner:telegram",
                                       actor_id="owner")
    ledger.resolve_approval(approval.id, resolution="rejected", actor_id="owner",
                            current_input_hash=operation.input_hash)

    assert ledger.get(operation.id).status == "cancelled"


def test_a16_a_rejected_operation_cannot_run(ledger):
    operation = _propose(ledger)
    approval = ledger.request_approval(operation, destination_id="owner:telegram",
                                       actor_id="owner")
    ledger.resolve_approval(approval.id, resolution="rejected", actor_id="owner",
                            current_input_hash=operation.input_hash)

    with pytest.raises(ApprovalRequired):
        ledger.require_authorized(operation)


def test_resolving_an_unknown_approval_is_an_error(ledger):
    with pytest.raises(InvalidInput):
        ledger.resolve_approval("nope", resolution="approved", actor_id="owner",
                                current_input_hash="x")


# --------------------------------------------------------------------------- #
# A17 — authorized is not committed
# --------------------------------------------------------------------------- #
def test_a17_authorisation_alone_is_not_a_commit(ledger):
    operation = ledger.propose(action="task.create", target="t",
                               payload={"x": 1}, idempotency_key="k")
    assert operation.status == "authorized"
    assert ledger.get(operation.id).committed is False


def test_a17_a_provider_failure_after_authorisation_is_recorded_as_failed(ledger):
    operation = ledger.propose(action="task.create", target="t",
                               payload={"x": 1}, idempotency_key="k")
    ledger.mark_running(operation)
    ledger.mark_failed(operation, error_code="unavailable")

    stored = ledger.get(operation.id)
    assert stored.status == "failed"
    assert stored.committed is False


def test_a17_an_uncertain_effect_is_unknown_not_committed(ledger):
    operation = ledger.propose(action="task.create", target="t",
                               payload={"x": 1}, idempotency_key="k")
    ledger.mark_unknown(operation)

    stored = ledger.get(operation.id)
    assert stored.status == "unknown"
    assert stored.committed is False


def test_a17_committed_is_only_recorded_after_a_verified_result(ledger):
    operation = ledger.propose(action="task.create", target="t",
                               payload={"x": 1}, idempotency_key="k")
    ledger.mark_committed(operation, result={"verified": True})

    assert ledger.get(operation.id).result == {"verified": True}
