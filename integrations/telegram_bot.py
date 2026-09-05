"""Telegram integration — inbound bot listener via python-telegram-bot.

Receives messages/commands from the user's Telegram chat and forwards them to
the orchestrator. Outbound sending lives in ``delivery/telegram.py``.

Phase 1 scope:
    - Build the Application from settings.telegram_bot_token.
    - Register handlers that forward inbound messages to the orchestrator.
    - run_polling(): start the long-poll loop (webhook mode later).
"""

from __future__ import annotations

from collections.abc import Callable

from config.settings import Settings, get_settings


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
