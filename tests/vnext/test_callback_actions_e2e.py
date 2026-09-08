"""Buttons on a delivered reminder (T09, T10).

interfaces §2 treats callback data as untrusted, and every test here is about
one of those clauses: the button carries an opaque id and nothing else, a press
is validated against actor, expiry and prior use, acknowledgement is not
success, and Dismiss suppresses a message without completing the errand.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User

from loop.app import build_application
from loop.core.clock import UTC
from loop.core.settings import Settings
from loop.interfaces.telegram import LoopTelegramBot
from loop.services.actions import ActionKind, ActionRefused

OWNER = 42
STRANGER = 999
FIRES_AT = dt.datetime(2026, 9, 6, 7, 0, tzinfo=UTC)


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None, "data_dir": str(tmp_path),
        "database_url": f"sqlite:///{tmp_path / 'loop.db'}",
        "capability_paths": json.dumps([str(tmp_path / "packs")]),
        "telegram_bot_token": "test-token",
        "telegram_chat_id": str(OWNER), "telegram_user_id": str(OWNER),
        "timezone": "Europe/Berlin",
    }
    base.update(overrides)
    return Settings(**base)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.answered: list[tuple[str, str]] = []

    async def initialize(self) -> None: ...
    async def shutdown(self) -> None: ...

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    async def answer_callback_query(self, *, callback_query_id: str,
                                    text: str) -> None:
        self.answered.append((callback_query_id, text))

    async def get_updates(self, *, offset, timeout, allowed_updates):
        del offset, timeout, allowed_updates
        return []


@pytest.fixture
def sends() -> Any:
    from loop.runtime.outbox import FakeTransport
    return FakeTransport()


@pytest.fixture
def bot(tmp_path: Path, clock, sends) -> LoopTelegramBot:
    application = build_application(_settings(tmp_path), clock=clock,
                                    transports={"telegram": sends})
    instance = LoopTelegramBot(application)
    instance.bot = FakeBot()
    instance.bot_transport = sends            # type: ignore[attr-defined]
    return instance


def press(action_id: str, *, query_id: str = "q1", user_id: int = OWNER) -> Update:
    user = User(id=user_id, first_name="Owner", is_bot=False)
    chat = Chat(id=OWNER, type="private")
    message = Message(message_id=1, date=dt.datetime.now(dt.UTC), chat=chat,
                      from_user=user, text="reminder")
    query = CallbackQuery(id=query_id, from_user=user, chat_instance="c",
                          data=action_id, message=message)
    return Update(update_id=hash(query_id) % 100000, callback_query=query)


def a_reminder(bot: LoopTelegramBot, *, title: str = "call the plumber"):
    """Schedule a reminder, fire it, and return its queued buttons."""
    application = bot.application
    reminder = application.reminders.schedule(
        title, instant=FIRES_AT, timezone="Europe/Berlin")
    application.clock.set(FIRES_AT)                       # type: ignore[attr-defined]
    application.service.tick()

    # The sweep delivered it through the fake transport, so the buttons are
    # read from the message the owner actually received.
    delivered = bot.bot_transport.sent[-1]["payload"]      # type: ignore[attr-defined]
    return reminder.task, delivered["actions"]


# --------------------------------------------------------------------------- #
# What the button carries
# --------------------------------------------------------------------------- #
def test_a_reminder_offers_done_snooze_and_dismiss(bot):
    _task, actions = a_reminder(bot)

    assert set(actions) == {"done", "snooze", "dismiss"}


def test_the_button_carries_an_opaque_id_and_nothing_else(bot):
    """Encoding the task or the action would let an edited callback retarget.

    Asserted as a shape rather than by hunting for substrings: a random id can
    contain "60" by coincidence, and a test that fails on a coincidence gets
    deleted rather than believed. A bare UUID cannot carry a payload at all.
    """
    import re

    uuid = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{4}-[0-9a-f]{12}$")
    task, actions = a_reminder(bot)

    assert len(set(actions.values())) == 3, "the three buttons share an id"
    for kind, action_id in actions.items():
        assert uuid.match(action_id), f"{kind} id is not an opaque uuid"
        assert task.id not in action_id
        assert kind not in action_id


# --------------------------------------------------------------------------- #
# T09 — snooze
# --------------------------------------------------------------------------- #
async def test_snoozing_creates_one_replacement_occurrence(bot, clock):
    """A delivered reminder cannot be un-sent, so snooze reschedules it (T09)."""
    task, actions = a_reminder(bot)
    before = len(bot.application.triggers.for_subject("task", task.id))

    await bot._handle_update(press(actions["snooze"]))

    after = bot.application.triggers.for_subject("task", task.id)
    assert len(after) == before + 1, "expected exactly one replacement"
    assert bot.application.tasks.get(task.id).is_open, "snooze completed the task"

    # And it fires an hour later, not now.
    assert bot.application.service.tick().notifications_sent == 0
    clock.advance(minutes=61)
    assert bot.application.service.tick().notifications_sent == 1


async def test_snoozing_records_exactly_one_feedback_observation(bot):
    task, actions = a_reminder(bot)

    await bot._handle_update(press(actions["snooze"]))

    recorded = bot.application.feedback.log.since(f"task:{task.id}", since=0)
    assert len(recorded) == 1
    assert recorded[0].shift_minutes == 60


async def test_pressing_snooze_twice_produces_one_effect(bot):
    """Telegram redelivers and users double-tap; one press is one occurrence."""
    task, actions = a_reminder(bot)

    await bot._handle_update(press(actions["snooze"], query_id="q1"))
    await bot._handle_update(press(actions["snooze"], query_id="q2"))

    recorded = bot.application.feedback.log.since(f"task:{task.id}", since=0)
    assert len(recorded) == 1
    assert "already been used" in bot.bot.sent[-1][1]
    # One live replacement, not two. The original one-shot trigger was
    # disabled when it fired, so this counts only what the presses created.
    assert len(bot.application.triggers.for_subject("task", task.id)) == 1


# --------------------------------------------------------------------------- #
# T10 — dismiss
# --------------------------------------------------------------------------- #
async def test_dismissing_suppresses_the_message_without_completing_the_task(bot):
    """The errand is still outstanding; only the message was cleared."""
    task, actions = a_reminder(bot)

    await bot._handle_update(press(actions["dismiss"]))

    assert bot.application.tasks.get(task.id).is_open
    assert bot.application.tasks.get(task.id).status != "done"
    assert "still open" in bot.bot.sent[-1][1]


async def test_done_completes_the_task_and_stops_its_reminders(bot):
    task, actions = a_reminder(bot)

    await bot._handle_update(press(actions["done"]))

    assert bot.application.tasks.get(task.id).status == "done"
    assert bot.application.service.tick().notifications_sent == 0


# --------------------------------------------------------------------------- #
# What a press is not allowed to do
# --------------------------------------------------------------------------- #
async def test_a_forwarded_button_cannot_be_pressed_by_someone_else(bot):
    task, actions = a_reminder(bot)

    await bot._handle_update(press(actions["done"], user_id=STRANGER))

    assert bot.application.tasks.get(task.id).is_open
    assert bot.bot.sent == [], "a stranger was answered at all"


async def test_an_unknown_button_id_does_nothing(bot):
    a_reminder(bot)

    await bot._handle_update(press("not-a-real-action"))

    assert "no longer valid" in bot.bot.sent[-1][1]


def test_an_expired_button_is_refused(bot, clock):
    _task, actions = a_reminder(bot)
    clock.set(FIRES_AT + dt.timedelta(days=3))

    with pytest.raises(ActionRefused) as raised:
        bot.application.actions.claim(actions["done"], actor=str(OWNER))

    assert "expired" in str(raised.value)


async def test_the_callback_is_acknowledged_after_the_effect(bot):
    """Acknowledgement stops the spinner; it must not precede the work."""
    task, actions = a_reminder(bot)

    await bot._handle_update(press(actions["done"], query_id="ack-1"))

    assert bot.bot.answered, "the press was never acknowledged"
    query_id, text = bot.bot.answered[-1]
    assert query_id == "ack-1"
    assert "Done" in text
    assert bot.application.tasks.get(task.id).status == "done"


def test_claiming_is_atomic_rather_than_read_then_write(bot):
    """Two taps arriving together must not both pass the check."""
    _task, actions = a_reminder(bot)
    store = bot.application.actions

    store.claim(actions["snooze"], actor=str(OWNER))
    with pytest.raises(ActionRefused):
        store.claim(actions["snooze"], actor=str(OWNER))


def test_actions_survive_a_restart(tmp_path, clock):
    """A button pressed tomorrow must still resolve to its task."""
    settings = _settings(tmp_path)
    first = build_application(settings, clock=clock)
    reminder = first.reminders.schedule("call the plumber", instant=FIRES_AT,
                                        timezone="Europe/Berlin")
    issued = first.actions.issue(
        kinds=[ActionKind.DONE], subject_ref=f"task:{reminder.task.id}",
        task_id=reminder.task.id, occurrence_key="occ-1", actor=str(OWNER))

    restarted = build_application(settings, clock=clock)
    action = restarted.actions.claim(issued["done"], actor=str(OWNER))

    assert action.task_id == reminder.task.id


def test_the_store_refuses_a_press_from_a_different_actor(bot):
    """The bot's pairing check rejects a stranger before this is reached.

    Tested directly because the two guards protect different things: pairing
    decides who may talk to Loop at all, while this decides whether *this*
    button belongs to the person pressing it — which matters the moment a
    message is forwarded, or a second owner exists.
    """
    _task, actions = a_reminder(bot)

    with pytest.raises(ActionRefused) as raised:
        bot.application.actions.claim(actions["done"], actor=str(STRANGER))

    assert "not meant for you" in str(raised.value)
    # And refusing must not consume it: the owner can still press it.
    assert bot.application.actions.claim(actions["done"], actor=str(OWNER))
