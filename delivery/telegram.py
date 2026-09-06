"""Telegram delivery — outbound message formatting and sending.

Formats reminders, follow-up approval prompts, and briefings, then sends them to
the user's Telegram chat. Inbound handling lives in
``integrations/telegram_bot.py``.

Phase 1 scope:
    - send(): send a plain/markdown message to settings.telegram_chat_id.
    - send_approval(): send an approval prompt with inline buttons
      ([Send] [Edit] [Snooze] [Ignore]).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from config.settings import Settings, get_settings


@dataclass
class ApprovalPrompt:
    """An actionable message asking the user to approve/modify an action."""

    text: str
    action_id: str
    buttons: list[str] = field(default_factory=lambda: ["Send", "Edit", "Snooze", "Ignore"])


class TelegramDelivery:
    """Outbound Telegram sender."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        from telegram import Bot

        token = self.settings.telegram_bot_token.strip()
        if not token or token.startswith("your-"):
            raise RuntimeError("TELEGRAM_BOT_TOKEN is missing; create a bot with @BotFather")
        self._bot = Bot(token=token)

    async def send_async(self, text: str, *, chat_id: str | None = None,
                         reply_markup=None) -> None:
        """Send a message without blocking an existing async application."""
        target = str(chat_id or self.settings.telegram_chat_id).strip()
        if not target or target.startswith("your-"):
            raise RuntimeError("TELEGRAM_CHAT_ID is missing")
        for start in range(0, len(text), 4096):
            await self._bot.send_message(
                chat_id=target,
                text=text[start:start + 4096],
                reply_markup=reply_markup,
            )

    def send(self, text: str, *, chat_id: str | None = None) -> None:
        """Send a message to the user's Telegram chat."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.send_async(text, chat_id=chat_id))
        else:
            loop.create_task(self.send_async(text, chat_id=chat_id))

    def send_approval(self, prompt: ApprovalPrompt, *, chat_id: str | None = None) -> None:
        """Send an approval prompt with inline keyboard buttons."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = [[
            InlineKeyboardButton(label, callback_data=f"{prompt.action_id}:{label.lower()}")
            for label in prompt.buttons
        ]]
        markup = InlineKeyboardMarkup(keyboard)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.send_async(prompt.text, chat_id=chat_id, reply_markup=markup))
        else:
            loop.create_task(self.send_async(prompt.text, chat_id=chat_id,
                                             reply_markup=markup))
