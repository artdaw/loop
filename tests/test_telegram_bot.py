"""Telegram polling commands without contacting Telegram."""

from __future__ import annotations

from types import SimpleNamespace

from config.settings import Settings
from integrations.telegram_bot import TelegramBot
from specialists.tasks import ParsedTask


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.voice = None
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, chat_id: int, text: str = "", chat_type: str = "private") -> None:
        self.effective_chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.effective_message = FakeMessage(text)


class FakeTaskSpecialist:
    def __init__(self, memory_store) -> None:
        self.memory = memory_store

    def parse_task_from_message(self, text: str) -> ParsedTask:
        return ParsedTask("Submit the report", priority="high")

    def save_task(self, parsed: ParsedTask, *, source: str):
        return self.memory.add_task(parsed.description, due_date=parsed.due_date,
                                    priority=parsed.priority, source=source)


class FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def ask(self, text: str, **kwargs):
        self.calls.append({"text": text, **kwargs})
        return SimpleNamespace(text="A private answer")


def telegram_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={
        "telegram_bot_token": "123:abc",
        "telegram_chat_id": "42",
    })


async def test_rejects_updates_from_another_chat(settings, memory_store):
    bot = TelegramBot(telegram_settings(settings), memory=memory_store)
    update = FakeUpdate(99)

    await bot._status(update, SimpleNamespace(args=[]))

    assert update.effective_message.replies == ["This Loop bot is private."]


async def test_task_command_persists_a_task(settings, memory_store):
    configured = telegram_settings(settings)
    bot = TelegramBot(
        configured,
        memory=memory_store,
        task_specialist=FakeTaskSpecialist(memory_store),
    )
    update = FakeUpdate(42)

    await bot._task(update, SimpleNamespace(args=["submit", "report", "urgent"]))

    tasks = memory_store.list_open_tasks()
    assert len(tasks) == 1
    assert tasks[0].description == "Submit the report"
    assert tasks[0].source == "telegram"
    assert "Added task #" in update.effective_message.replies[0]


async def test_private_text_forces_local_inference(settings, memory_store):
    configured = telegram_settings(settings)
    orchestrator = FakeOrchestrator()
    bot = TelegramBot(configured, memory=memory_store, orchestrator=orchestrator)
    update = FakeUpdate(42, "What is due today?")

    await bot._text(update, SimpleNamespace(args=[]))

    assert orchestrator.calls == [{
        "text": "What is due today?",
        "session_id": "telegram:42",
        "local_only": True,
    }]
    assert update.effective_message.replies == ["A private answer"]


async def test_done_command_completes_a_task(settings, memory_store):
    task = memory_store.add_task("Finish it")
    bot = TelegramBot(telegram_settings(settings), memory=memory_store)
    update = FakeUpdate(42)

    await bot._done(update, SimpleNamespace(args=[str(task.id)]))

    assert memory_store.list_open_tasks() == []
    assert update.effective_message.replies == [f"Completed task #{task.id}."]
