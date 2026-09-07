"""The vNext Telegram bot (interfaces §2), against real `Application` services.

`Bot.get_updates`/`send_message`/`initialize`/`shutdown` are faked (no network
in tests), but every update is real `telegram.Update`/`Message`/`Chat`/`User`
model construction, and every command runs through a real `build_application()`
against a temporary database — the same as the CLI and HTTP tests.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from telegram import Chat, Message, Update, User

from loop.app import build_application
from loop.core.settings import Settings
from loop.interfaces.telegram import LoopTelegramBot

OWNER_CHAT = 42
OWNER_USER = 42
STRANGER_USER = 999


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None, "data_dir": str(tmp_path),
        "database_url": f"sqlite:///{tmp_path / 'loop.db'}",
        "ollama_default_model": "llama3.1:8b",
        "capability_paths": json.dumps([str(tmp_path / "packs")]),
        "telegram_bot_token": "test-token",
        "telegram_chat_id": str(OWNER_CHAT),
        "telegram_user_id": str(OWNER_USER),
    }
    base.update(overrides)
    return Settings(**base)


def _update(update_id: int, text: str, *, chat_id: int = OWNER_CHAT,
           user_id: int = OWNER_USER) -> Update:
    user = User(id=user_id, first_name="Someone", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    message = Message(message_id=update_id, date=dt.datetime.now(dt.UTC),
                      chat=chat, from_user=user, text=text)
    return Update(update_id=update_id, message=message)


class FakeBot:
    """Replaces `telegram.Bot` network calls with an in-memory queue."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.initialized = False
        self.shutdown_called = False

    async def initialize(self) -> None:
        self.initialized = True

    async def shutdown(self) -> None:
        self.shutdown_called = True

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    async def get_updates(self, *, offset: int | None, timeout: int,
                          allowed_updates: object) -> list[Update]:
        del offset, timeout, allowed_updates
        return []  # tests drive `_handle_update` directly, not the poll loop


@pytest.fixture
def bot(tmp_path: Path) -> LoopTelegramBot:
    application = build_application(settings=_settings(tmp_path))
    instance = LoopTelegramBot(application)
    instance.bot = FakeBot()
    return instance


# --------------------------------------------------------------------------- #
# Pairing (interfaces §2: refuse everyone until bound)
# --------------------------------------------------------------------------- #
async def test_an_unbound_instance_refuses_the_first_start(tmp_path):
    application = build_application(
        settings=_settings(tmp_path, telegram_chat_id="", telegram_user_id=""))
    instance = LoopTelegramBot(application)
    instance.bot = FakeBot()

    await instance._handle_update(_update(1, "/start"))

    assert instance.bot.sent == []


async def test_a_message_from_a_different_chat_is_rejected(bot):
    await bot._handle_update(_update(1, "/status", chat_id=999))
    assert bot.bot.sent == []


async def test_a_message_from_a_different_sender_is_rejected(bot):
    await bot._handle_update(_update(1, "/status", user_id=STRANGER_USER))
    assert bot.bot.sent == []


# --------------------------------------------------------------------------- #
# Dedup (interfaces §2: deduplicate provider redelivery)
# --------------------------------------------------------------------------- #
async def test_a_redelivered_update_is_not_processed_twice(bot):
    await bot._handle_update(_update(5, "/task water the fern"))
    assert len(bot.bot.sent) == 1

    await bot._handle_update(_update(5, "/task water the fern"))
    assert len(bot.bot.sent) == 1  # no second task, no second reply

    tasks = bot.application.tasks.list()
    assert len(tasks) == 1


# --------------------------------------------------------------------------- #
# Commands, against real services
# --------------------------------------------------------------------------- #
async def test_status_reports_real_pending_counts(bot):
    await bot._handle_update(_update(1, "/status"))
    chat_id, text = bot.bot.sent[0]
    assert chat_id == OWNER_CHAT
    assert "triggers enabled" in text


async def test_task_creates_a_real_task(bot):
    await bot._handle_update(_update(1, "/task water the fern"))
    _, text = bot.bot.sent[0]
    assert "water the fern" in text

    tasks = bot.application.tasks.list()
    assert len(tasks) == 1
    assert tasks[0].title == "water the fern"


async def test_tasks_lists_open_tasks(bot):
    bot.application.tasks.create("water the fern")
    await bot._handle_update(_update(1, "/tasks"))
    _, text = bot.bot.sent[0]
    assert "water the fern" in text


async def test_done_completes_the_named_task(bot):
    task = bot.application.tasks.create("water the fern")
    await bot._handle_update(_update(1, f"/done {task.id}"))
    _, text = bot.bot.sent[0]
    assert "done" in text

    refreshed = {t.id: t for t in bot.application.tasks.list()}[task.id]
    assert refreshed.status == "done"


async def test_done_with_an_unknown_id_is_a_real_error_not_a_crash(bot):
    await bot._handle_update(_update(1, "/done nonexistent"))
    _, text = bot.bot.sent[0]
    assert "no such task" in text.lower()


async def test_an_unrecognised_command_says_so(bot):
    await bot._handle_update(_update(1, "/fly-me-to-the-moon"))
    _, text = bot.bot.sent[0]
    assert "unrecognised" in text.lower()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
async def test_initialize_and_shutdown_call_the_pinned_lifecycle_methods(bot):
    await bot.initialize()
    assert bot.bot.initialized is True
    await bot.shutdown()
    assert bot.bot.shutdown_called is True


# --------------------------------------------------------------------------- #
# The wider command table (M6)
# --------------------------------------------------------------------------- #
@pytest.fixture
def vault_bot(tmp_path: Path) -> LoopTelegramBot:
    from tests.vnext.vault_fixtures import build_minimal_vault

    vault = build_minimal_vault(tmp_path / "vault")
    application = build_application(
        settings=_settings(tmp_path, obsidian_vault_path=str(vault.root)))
    instance = LoopTelegramBot(application)
    instance.bot = FakeBot()
    return instance


async def reply(bot: LoopTelegramBot, text: str, *, update_id: int = 1) -> str:
    await bot._handle_update(_update(update_id, text))
    sent: list[tuple[int, str]] = bot.bot.sent    # type: ignore[attr-defined]
    return sent[-1][1] if sent else ""


async def test_help_names_what_is_not_available_rather_than_omitting_it(bot):
    """Otherwise the owner learns by trial which commands are real."""
    text = await reply(bot, "/help")

    assert "/remember" in text and "/weather" in text
    assert "Not available yet" in text
    assert "/travel" in text and "/briefing" in text


async def test_remember_captures_exactly_and_says_what_happened(vault_bot):
    text = await reply(vault_bot, "/remember Supplier lead time is six weeks")

    assert text.startswith("Saved to your vault: 0-raw/inbox/")
    path = text.split("Saved to your vault: ")[1].strip()
    stored = vault_bot.application.knowledge.gateway.read_text(path)
    assert stored.endswith("Supplier lead time is six weeks")
    assert "origin: telegram" in stored


async def test_find_returns_a_cited_answer(vault_bot):
    await reply(vault_bot, "/remember Anodised brackets arrive Thursday",
                update_id=1)

    text = await reply(vault_bot, "/find anodised brackets", update_id=2)

    assert "0-raw/inbox/" in text
    assert "uncompiled" in text.lower()


async def test_find_with_nothing_behind_it_reports_a_gap(vault_bot):
    text = await reply(vault_bot, "/find quarterly revenue in Patagonia")

    assert "nothing in the vault" in text.lower()


async def test_knowledge_commands_say_plainly_when_no_vault_is_configured(bot):
    text = await reply(bot, "/remember something")

    assert "No vault is configured" in text


async def test_weather_without_a_location_asks_instead_of_guessing(bot):
    """A timezone is not a coordinate (WF16), and this reaches no provider."""
    text = await reply(bot, "/weather")

    assert "location" in text.lower()


async def test_capabilities_lists_the_trusted_ports_and_enabled_packs(bot):
    text = await reply(bot, "/capabilities")

    assert "weather.prepare" in text
    assert "vault.search" in text


async def test_do_invokes_any_operation_without_a_command_of_its_own(vault_bot):
    text = await reply(vault_bot, '/do vault.search {"query": "lead time"}')

    assert "knowledge_gap" in text


async def test_do_rejects_arguments_that_are_not_json(bot):
    text = await reply(bot, "/do vault.search not-json")

    assert "must be JSON" in text


async def test_routines_lists_status_and_next_run(bot):
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
  destination: "42"
---

Once.
"""
    routine, problems = parse_routine(
        document, known_capabilities=bot.application.known_capabilities())
    assert problems == []
    bot.application.routines.save(routine)

    assert "unscheduled" in await reply(bot, "/routines", update_id=1)

    bot.application.routine_scheduler.activate("rain-check",
                                               activation_event_id="evt")
    text = await reply(bot, "/routines", update_id=2)
    assert "[active]" in text and "next " in text

    assert "paused" in await reply(bot, "/pause rain-check", update_id=3)
    assert "active" in await reply(bot, "/resume rain-check", update_id=4)


async def test_snooze_moves_the_queued_message_and_records_the_signal(bot):
    """Both halves: this occurrence moves, and the pattern is observed."""
    application = bot.application
    application.outbox.enqueue(
        category="requested_routine", subject_ref="routine:morning-weather",
        occurrence_key="2026-09-06", destination_id="42",
        payload={"text": "briefing"})

    text = await reply(bot, "/snooze morning-weather 45")

    assert "Moved 1 message(s)" in text
    assert application.service.tick().notifications_sent == 0
    recorded = application.feedback.log.since("routine:morning-weather", since=0)
    assert [f.shift_minutes for f in recorded] == [45]


async def test_snooze_defaults_to_an_hour(bot):
    await reply(bot, "/snooze morning-weather")

    recorded = bot.application.feedback.log.since("routine:morning-weather",
                                                 since=0)
    assert [f.shift_minutes for f in recorded] == [60]


async def test_snooze_with_nothing_queued_says_so_rather_than_claiming_success(bot):
    text = await reply(bot, "/snooze morning-weather 30")

    assert "nothing to move" in text
    assert "Recorded" in text


async def test_review_reports_pending_work_and_proposes_nothing_silently(bot):
    text = await reply(bot, "/review")

    assert "Open tasks:" in text
    assert "No adaptations to propose." in text


async def test_why_explains_a_task_from_what_was_recorded(bot):
    await reply(bot, "/task water the fern", update_id=1)
    task_id = bot.application.tasks.list()[0].id

    text = await reply(bot, f"/why {task_id}", update_id=2)

    assert task_id in text
    assert "water the fern" in text


async def test_why_explains_a_routine_including_who_authorised_it(bot):
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
  destination: "42"
---

Once.
"""
    routine, _ = parse_routine(
        document, known_capabilities=bot.application.known_capabilities())
    bot.application.routines.save(routine)
    bot.application.routine_scheduler.activate("rain-check",
                                               activation_event_id="evt-77")

    text = await reply(bot, "/why rain-check")

    assert "evt-77" in text
    assert "active" in text


async def test_why_an_unknown_id_does_not_invent_an_explanation(bot):
    text = await reply(bot, "/why not-a-real-id")

    assert "Nothing recorded" in text


async def test_an_unknown_command_is_still_reported_honestly(bot):
    text = await reply(bot, "/travel two weeks in Portugal")

    assert "Unrecognised command" in text
