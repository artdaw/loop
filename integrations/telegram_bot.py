"""Telegram long-polling bot for Loop."""

from __future__ import annotations

import asyncio
import inspect
import logging
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

MessageCallback = Callable[[str], str | Awaitable[str]]
MAX_MESSAGE_LENGTH = 4096


class TelegramBot:
    """Receive Telegram commands and messages through long polling.

    Only ``TELEGRAM_CHAT_ID`` may use the bot. This matters because the bot can
    search private notes and mutate the local task database.
    """

    def __init__(self, settings: Settings | None = None, *,
                 orchestrator: Any | None = None, memory: Any | None = None,
                 calendar: Any | None = None, knowledge: Any | None = None,
                 task_specialist: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._orchestrator = orchestrator
        self._memory = memory
        self._calendar = calendar
        self._knowledge = knowledge
        self._task_specialist = task_specialist
        self._app: Any | None = None
        self._on_message: MessageCallback | None = None

    def set_message_handler(self, handler: MessageCallback) -> None:
        """Override natural-language message handling."""
        self._on_message = handler

    def build_application(self) -> Any:
        """Build the python-telegram-bot application and register handlers."""
        from telegram.ext import Application, CommandHandler, MessageHandler, filters

        token = self.settings.telegram_bot_token.strip()
        chat_id = self.settings.telegram_chat_id.strip()
        if not token or token.startswith("your-"):
            raise RuntimeError("TELEGRAM_BOT_TOKEN is missing; create a bot with @BotFather")
        if not chat_id or chat_id.startswith("your-"):
            raise RuntimeError(
                "TELEGRAM_CHAT_ID is missing; set it to your numeric Telegram chat id"
            )

        app = Application.builder().token(token).build()
        app.add_handler(CommandHandler("start", self._start))
        app.add_handler(CommandHandler("help", self._start))
        app.add_handler(CommandHandler("status", self._status))
        app.add_handler(CommandHandler("briefing", self._briefing))
        app.add_handler(CommandHandler("task", self._task))
        app.add_handler(CommandHandler("tasks", self._tasks))
        app.add_handler(CommandHandler("done", self._done))
        app.add_handler(MessageHandler(filters.VOICE, self._voice))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._text))
        app.add_error_handler(self._error)
        self._app = app
        return app

    def run_polling(self) -> None:
        """Block while receiving Telegram updates through long polling."""
        from telegram import Update

        app = self._app or self.build_application()
        logger.info("Starting Telegram polling for chat %s", self.settings.telegram_chat_id)
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)

    def _authorised(self, update: Any) -> bool:
        chat = getattr(update, "effective_chat", None)
        return chat is not None and str(chat.id) == self.settings.telegram_chat_id.strip()

    async def _require_authorised(self, update: Any) -> bool:
        if self._authorised(update):
            return True
        logger.warning("Rejected Telegram update from chat %s",
                       getattr(getattr(update, "effective_chat", None), "id", "unknown"))
        message = getattr(update, "effective_message", None)
        if message is not None:
            await message.reply_text("This Loop bot is private.")
        return False

    async def _reply(self, update: Any, text: str) -> None:
        message = getattr(update, "effective_message", None)
        if message is None:
            return
        value = str(text or "No response.")
        for start in range(0, len(value), MAX_MESSAGE_LENGTH):
            await message.reply_text(value[start:start + MAX_MESSAGE_LENGTH])

    async def _start(self, update: Any, context: Any) -> None:
        if not await self._require_authorised(update):
            return
        await self._reply(update, (
            "Loop is connected.\n\n"
            "/status — health and configuration\n"
            "/briefing — today's agenda\n"
            "/task <description> — add a task\n"
            "/tasks — list open tasks\n"
            "/done <id> — complete a task\n\n"
            "Or send a question in plain text."
        ))

    async def _status(self, update: Any, context: Any) -> None:
        if not await self._require_authorised(update):
            return
        from core.health import HealthChecker

        checker = HealthChecker(self.settings, self._get_memory())
        report = await asyncio.to_thread(lambda: checker.check().render())
        await self._reply(update, report)

    async def _briefing(self, update: Any, context: Any) -> None:
        if not await self._require_authorised(update):
            return
        from specialists.briefing import DailyBriefing

        briefing = DailyBriefing(self.settings, self._get_memory(), self._calendar)
        text = await asyncio.to_thread(briefing.compose)
        await self._reply(update, text)

    async def _task(self, update: Any, context: Any) -> None:
        if not await self._require_authorised(update):
            return
        text = " ".join(getattr(context, "args", [])).strip()
        if not text:
            await self._reply(update, "Usage: /task Submit the report tomorrow")
            return

        from core.autonomy import ActionType, AutonomyGate
        from core.exceptions import ApprovalRequiredError
        from specialists.tasks import ParsedTask

        specialist = self._get_tasks()
        try:
            parsed = await asyncio.to_thread(specialist.parse_task_from_message, text)
        except Exception:  # noqa: BLE001 - capture still works if Ollama is down
            parsed = ParsedTask(
                description=text,
                due_date=specialist._heuristic_due_date(text),
                priority=specialist._heuristic_priority(text),
            )
        try:
            AutonomyGate(self.settings, self._get_memory()).guard(
                ActionType.TASK_CREATE, approved=True, detail="telegram /task"
            )
        except ApprovalRequiredError as exc:
            await self._reply(update, f"Task creation is disabled: {exc}")
            return
        task = specialist.save_task(parsed, source="telegram")
        due = f", due {task.due_date.isoformat()}" if task.due_date else ""
        await self._reply(
            update,
            f"Added task #{task.id}: {task.description} ({task.priority}{due})",
        )

    async def _tasks(self, update: Any, context: Any) -> None:
        if not await self._require_authorised(update):
            return
        tasks = self._get_memory().list_open_tasks()
        if not tasks:
            await self._reply(update, "No open tasks.")
            return
        lines = ["Open tasks:"]
        for task in tasks[:30]:
            due = f" — due {task.due_date.isoformat()}" if task.due_date else ""
            lines.append(f"#{task.id} {task.description}{due}")
        if len(tasks) > 30:
            lines.append(f"…and {len(tasks) - 30} more")
        await self._reply(update, "\n".join(lines))

    async def _done(self, update: Any, context: Any) -> None:
        if not await self._require_authorised(update):
            return
        raw = " ".join(getattr(context, "args", [])).strip().lstrip("#")
        if not raw.isdigit():
            await self._reply(update, "Usage: /done <task id>")
            return
        task_id = int(raw)
        if self._get_memory().complete_task(task_id):
            await self._reply(update, f"Completed task #{task_id}.")
        else:
            await self._reply(update, f"No open task #{task_id}.")

    async def _text(self, update: Any, context: Any) -> None:
        if not await self._require_authorised(update):
            return
        text = (getattr(getattr(update, "effective_message", None), "text", "") or "").strip()
        if not text:
            return
        try:
            if self._on_message is not None:
                result = self._on_message(text)
                answer = await result if inspect.isawaitable(result) else result
            else:
                chat = update.effective_chat
                response = await self._get_orchestrator().ask(
                    text,
                    session_id=f"telegram:{chat.id}",
                    local_only=(self.settings.private_telegram_personal
                                and getattr(chat, "type", "") == "private"),
                )
                answer = response.text
            await self._reply(update, str(answer))
        except Exception as exc:  # noqa: BLE001 - keep polling alive
            logger.exception("Telegram message handling failed")
            await self._reply(update, f"I couldn't answer that: {exc}")

    async def _voice(self, update: Any, context: Any) -> None:
        if not await self._require_authorised(update):
            return
        voice = getattr(getattr(update, "effective_message", None), "voice", None)
        if voice is None:
            return
        try:
            draft = await handle_voice_message(context.bot, voice.file_id,
                                               self._get_knowledge())
            await self._reply(update, f"Filed voice note: {draft.title}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Telegram voice handling failed")
            await self._reply(update, f"I couldn't process that voice note: {exc}")

    async def _error(self, update: object, context: Any) -> None:
        logger.error("Unhandled Telegram update error", exc_info=context.error)

    def _get_memory(self) -> Any:
        if self._memory is None:
            from core.memory import MemoryStore

            self._memory = MemoryStore(self.settings)
            self._memory.bootstrap()
        return self._memory

    def _get_orchestrator(self) -> Any:
        if self._orchestrator is None:
            from core.orchestrator import Orchestrator

            self._orchestrator = Orchestrator(self.settings)
        return self._orchestrator

    def _get_knowledge(self) -> Any:
        if self._knowledge is None:
            from specialists.knowledge import KnowledgeSpecialist

            self._knowledge = KnowledgeSpecialist(self.settings)
        return self._knowledge

    def _get_tasks(self) -> Any:
        if self._task_specialist is None:
            from specialists.tasks import TaskSpecialist

            self._task_specialist = TaskSpecialist(self.settings, memory=self._get_memory())
        return self._task_specialist


async def handle_voice_message(bot: Any, file_id: str, knowledge: Any, *,
                               suffix: str = ".ogg") -> Any:
    """Download a voice memo, file it locally as a note, then delete audio."""
    telegram_file = await bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        await telegram_file.download_to_drive(str(temp_path))
        draft = knowledge.note_from_audio(temp_path, source="telegram_voice")
        logger.info("Filed a voice note: %s", draft.title)
        return draft
    finally:
        temp_path.unlink(missing_ok=True)
