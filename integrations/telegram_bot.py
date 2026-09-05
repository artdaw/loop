"""Telegram integration — inbound bot listener via python-telegram-bot.

Receives messages/commands from the user's Telegram chat and forwards them to
the orchestrator. Outbound sending lives in ``delivery/telegram.py``.

Phase 1 scope:
    - Build the Application from settings.telegram_bot_token.
    - Register handlers that forward inbound messages to the orchestrator.
    - run_polling(): start the long-poll loop (webhook mode later).

Phase 4 adds :func:`handle_voice_message`, which turns a Telegram voice memo
into a vault note. The polling loop itself is still a Phase 1 stub, so the
handler is written as a standalone coroutine that takes the bot and the voice
payload — testable against a fake bot, and ready to wire in when polling lands.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class TelegramBot:
    """Inbound Telegram listener."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._app = None
        self._on_message: Callable[[str], str] | None = None
        # TODO(phase1): build Application.builder().token(...).build().

    def set_message_handler(self, handler: Callable[[str], str]) -> None:
        """Register the callback that handles inbound message text."""
        self._on_message = handler

    def run_polling(self) -> None:
        """Start receiving updates via long polling."""
        # TODO(phase1): add CommandHandler/MessageHandler; app.run_polling().
        raise NotImplementedError("TelegramBot.run_polling is a Phase 1 stub.")


async def handle_voice_message(bot: Any, file_id: str, knowledge: Any, *,
                               suffix: str = ".ogg") -> Any:
    """Download a Telegram voice memo and file it as a note.

    Args:
        bot: anything exposing ``async get_file(file_id)`` returning an object
            with ``async download_to_drive(path)`` (the python-telegram-bot API).
        file_id: the Telegram file id of the voice message.
        knowledge: a KnowledgeSpecialist exposing ``note_from_audio(path)``.

    The audio is written to a temporary file, transcribed locally, and deleted —
    Loop keeps the note, not the recording. Voice data never reaches a cloud
    model; see :meth:`specialists.knowledge.KnowledgeSpecialist.note_from_audio`.
    """
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
