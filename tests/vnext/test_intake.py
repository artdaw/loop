"""Authenticated event intake (runtime §2/§5, interfaces §2).

Covers acceptance T02 (replay) and T15 (idempotency key reused with a different
body), plus the owner-binding rules that stop a stranger becoming the owner.
"""

from __future__ import annotations

import pytest

from loop.core.errors import AuthRequired, Conflict, ErrorCode
from loop.core.privacy import ModelScope
from loop.runtime.intake import EventIntake, InboundMessage


@pytest.fixture
def intake(sessions, settings, clock) -> EventIntake:
    return EventIntake(sessions=sessions, settings=settings, clock=clock)


def _telegram(text: str = "Remind me tomorrow at 9 to call the repair shop",
              *, update_id: str = "update:12345",
              chat_id: str = "4242", user_id: str = "99") -> InboundMessage:
    return InboundMessage(
        origin="telegram",
        origin_id=update_id,
        idempotency_key=update_id,
        kind="message.received",
        payload={"text": text},
        actor_chat_id=chat_id,
        actor_user_id=user_id,
    )


# --------------------------------------------------------------------------- #
# Acceptance and commit ordering
# --------------------------------------------------------------------------- #
def test_an_authenticated_message_is_accepted(intake):
    result = intake.accept(_telegram())

    assert result.accepted is True
    assert result.created is True
    assert result.event_id


def test_the_event_is_committed_before_acknowledgement(intake, sessions):
    """Runtime §2 step 1: commit the event, *then* acknowledge it."""
    result = intake.accept(_telegram())

    from sqlalchemy import text
    with sessions() as session:
        stored = session.execute(
            text("SELECT id, state FROM events WHERE id = :id"),
            {"id": result.event_id}).one()
    assert stored[1] == "accepted"


def test_the_envelope_records_origin_identity(intake, sessions):
    from sqlalchemy import text

    result = intake.accept(_telegram(update_id="update:777"))

    with sessions() as session:
        origin, origin_id, key = session.execute(
            text("SELECT origin, origin_id, idempotency_key FROM events "
                 "WHERE id = :id"), {"id": result.event_id}).one()
    assert (origin, origin_id, key) == ("telegram", "update:777", "update:777")


def test_the_event_is_its_own_root_with_no_causation(intake, sessions):
    from sqlalchemy import text

    result = intake.accept(_telegram())

    with sessions() as session:
        root, causation, hops = session.execute(
            text("SELECT root_id, causation_id, hop_count FROM events "
                 "WHERE id = :id"), {"id": result.event_id}).one()
    assert root == result.event_id
    assert causation is None
    assert hops == 0


def test_occurred_and_received_times_come_from_the_injected_clock(intake, clock):
    from loop.core.clock import to_micros

    result = intake.accept(_telegram())
    assert result.received_at == to_micros(clock.now())


# --------------------------------------------------------------------------- #
# T02 — replay
# --------------------------------------------------------------------------- #
def test_t02_replaying_the_same_update_three_times_stores_one_event(intake, sessions):
    from sqlalchemy import text

    results = [intake.accept(_telegram()) for _ in range(3)]

    with sessions() as session:
        count = session.execute(text("SELECT COUNT(*) FROM events")).scalar()
    assert count == 1
    assert len({r.event_id for r in results}) == 1


def test_t02_replays_return_the_same_accepted_result(intake):
    first = intake.accept(_telegram())
    second = intake.accept(_telegram())

    assert second.accepted is True
    assert second.event_id == first.event_id


def test_t02_only_the_first_delivery_is_marked_created(intake):
    """`created` is what downstream uses to avoid a second acknowledgement."""
    first = intake.accept(_telegram())
    replays = [intake.accept(_telegram()) for _ in range(2)]

    assert first.created is True
    assert [r.created for r in replays] == [False, False]


def test_t02_replay_is_not_reported_as_an_error(intake):
    """A replay is a normal, expected delivery — not a failure."""
    intake.accept(_telegram())
    assert intake.accept(_telegram()).duplicate is True


def test_a_different_update_creates_a_second_event(intake, sessions):
    from sqlalchemy import text

    intake.accept(_telegram(update_id="update:1"))
    intake.accept(_telegram(update_id="update:2"))

    with sessions() as session:
        assert session.execute(text("SELECT COUNT(*) FROM events")).scalar() == 2


def test_the_same_key_from_another_origin_is_a_separate_event(intake, sessions):
    from sqlalchemy import text

    intake.accept(_telegram(update_id="shared-key"))
    intake.accept(InboundMessage(
        origin="cli", origin_id="shared-key", idempotency_key="shared-key",
        kind="message.received", payload={"text": "hello"},
    ))

    with sessions() as session:
        assert session.execute(text("SELECT COUNT(*) FROM events")).scalar() == 2


# --------------------------------------------------------------------------- #
# T15 — idempotency key reused with a different body
# --------------------------------------------------------------------------- #
def test_t15_same_key_different_body_is_a_conflict(intake):
    intake.accept(_telegram("call the repair shop"))

    with pytest.raises(Conflict) as excinfo:
        intake.accept(_telegram("transfer 500 euro instead"))

    assert excinfo.value.code is ErrorCode.CONFLICT


def test_t15_conflict_maps_to_http_409(intake):
    intake.accept(_telegram("original"))
    with pytest.raises(Conflict) as excinfo:
        intake.accept(_telegram("different"))

    assert excinfo.value.http_status == 409


def test_t15_the_conflicting_body_has_no_second_effect(intake, sessions):
    from sqlalchemy import text

    first = intake.accept(_telegram("original"))
    with pytest.raises(Conflict):
        intake.accept(_telegram("different"))

    with sessions() as session:
        rows = session.execute(text("SELECT id, payload_json FROM events")).all()
    assert len(rows) == 1
    assert rows[0][0] == first.event_id
    assert "original" in rows[0][1]


def test_t15_conflict_names_the_key_without_leaking_the_body(intake):
    intake.accept(_telegram("secret original text"))
    with pytest.raises(Conflict) as excinfo:
        intake.accept(_telegram("secret different text"))

    message = str(excinfo.value)
    assert "update:12345" in message
    assert "secret" not in message


# --------------------------------------------------------------------------- #
# Owner binding (interfaces §2)
# --------------------------------------------------------------------------- #
def test_a_message_from_another_chat_is_rejected(intake):
    with pytest.raises(AuthRequired):
        intake.accept(_telegram(chat_id="9999"))


def test_a_message_from_another_sender_in_the_right_chat_is_rejected(intake):
    """Both chat *and* sender identity are verified, not just the chat."""
    with pytest.raises(AuthRequired):
        intake.accept(_telegram(user_id="1234"))


def test_a_rejected_message_stores_no_event(intake, sessions):
    from sqlalchemy import text

    with pytest.raises(AuthRequired):
        intake.accept(_telegram(chat_id="9999"))

    with sessions() as session:
        assert session.execute(text("SELECT COUNT(*) FROM events")).scalar() == 0


def test_an_unbound_instance_refuses_the_first_arbitrary_start(sessions, clock,
                                                               settings):
    """interfaces §2: an unbound instance MUST NOT adopt a stranger as owner."""
    unbound = settings.model_copy(update={"telegram_chat_id": "",
                                          "telegram_user_id": ""})
    intake = EventIntake(sessions=sessions, settings=unbound, clock=clock)

    with pytest.raises(AuthRequired):
        intake.accept(_telegram("/start", chat_id="12345", user_id="6789"))


def test_a_non_telegram_origin_does_not_require_chat_binding(intake):
    """The CLI runs as the authenticated owner already."""
    result = intake.accept(InboundMessage(
        origin="cli", origin_id="cli:1", idempotency_key="cli:1",
        kind="message.received", payload={"text": "add a task"},
    ))
    assert result.accepted is True


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #
def test_telegram_events_are_local_only_and_sensitive_by_default(intake, sessions):
    import json

    from sqlalchemy import text

    result = intake.accept(_telegram())

    with sessions() as session:
        raw = session.execute(text("SELECT privacy FROM events WHERE id = :id"),
                              {"id": result.event_id}).scalar()
    label = json.loads(raw)
    assert label["model_scope"] == ModelScope.LOCAL_ONLY.value
    assert label["sensitive"] is True
    assert label["origins"] == ["telegram_private"]


def test_the_owner_chat_is_an_allowed_destination(intake, sessions):
    """Local-only governs models, not whether Loop may reply to its owner."""
    import json

    from sqlalchemy import text

    result = intake.accept(_telegram())
    with sessions() as session:
        raw = session.execute(text("SELECT privacy FROM events WHERE id = :id"),
                              {"id": result.event_id}).scalar()

    assert "owner:telegram" in json.loads(raw)["allowed_destinations"]


def test_a_caller_cannot_declare_an_event_cloud_allowed(intake, sessions):
    """A supplied label may narrow, never widen (runtime §3)."""
    import json

    from sqlalchemy import text

    message = _telegram()
    message.claimed_privacy = {"model_scope": "cloud_allowed",
                               "allowed_destinations": ["anywhere"]}
    result = intake.accept(message)

    with sessions() as session:
        raw = session.execute(text("SELECT privacy FROM events WHERE id = :id"),
                              {"id": result.event_id}).scalar()
    label = json.loads(raw)
    assert label["model_scope"] == "local_only"
    assert "anywhere" not in label["allowed_destinations"]
