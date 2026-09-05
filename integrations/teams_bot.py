"""Microsoft Teams integration — inbound bot listener via the Bot Framework.

Receives Teams activities (messages) and forwards them to the orchestrator.
Outbound sending lives in ``delivery/teams.py``. Requires an Azure app
registration (app id / password).

Phase 2 scope (Teams lands in Phase 2 per the roadmap):
    - Build the BotFrameworkAdapter from settings.teams_app_id/password.
    - Handle inbound activities and forward text to the orchestrator.
    - Expose an aiohttp/FastAPI webhook endpoint for the Bot Framework.
"""

from __future__ import annotations

from collections.abc import Callable

from config.settings import Settings, get_settings


class TeamsBot:
    """Inbound Teams listener."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._adapter = None
        self._on_message: Callable[[str], str] | None = None
        # TODO(phase2): construct BotFrameworkAdapter with app id/password.

    def set_message_handler(self, handler: Callable[[str], str]) -> None:
        """Register the callback that handles inbound message text."""
        self._on_message = handler

    async def process_activity(self, body: dict, auth_header: str) -> None:
        """Process one inbound Bot Framework activity."""
        # TODO(phase2): adapter.process_activity(...) -> forward to handler.
        raise NotImplementedError("TeamsBot.process_activity is a Phase 2 stub.")
