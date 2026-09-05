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
        self._bot = None
        # TODO(phase1): construct telegram.Bot(token=settings.telegram_bot_token).

    def send(self, text: str, *, chat_id: str | None = None) -> None:
        """Send a message to the user's Telegram chat."""
        # TODO(phase1): call bot.send_message(chat_id or settings.telegram_chat_id).
        raise NotImplementedError("TelegramDelivery.send is a Phase 1 stub.")

    def send_approval(self, prompt: ApprovalPrompt, *, chat_id: str | None = None) -> None:
        """Send an approval prompt with inline keyboard buttons."""
        # TODO(phase1): build InlineKeyboardMarkup from prompt.buttons.
        raise NotImplementedError("TelegramDelivery.send_approval is a Phase 1 stub.")
