"""Microsoft Teams integration — Bot Framework webhook handler.

Receives Teams activities (messages) via the Bot Framework, routes their text to
the orchestrator, and returns the reply. Also supports *proactive* messages so
Loop can push reminders and follow-up approval prompts into a Teams
conversation.

Built on ``botbuilder-core`` + ``botbuilder-integration-aiohttp``. Requires an
Azure app registration (``TEAMS_APP_ID`` / ``TEAMS_APP_PASSWORD``).

Usage::

    bot = TeamsBot(orchestrator=my_orchestrator)
    app = bot.build_app()            # aiohttp.web.Application with /api/messages
    web.run_app(app, port=3978)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from aiohttp import web
from botbuilder.core import (
    ActivityHandler,
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    TurnContext,
)
from botbuilder.core.integration import aiohttp_error_middleware
from botbuilder.schema import Activity, ConversationReference

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# A handler takes the inbound text and returns the reply text (sync or async).
MessageHandler = Callable[[str], Any]


class _LoopActivityHandler(ActivityHandler):
    """Routes inbound Teams messages to the registered handler."""

    def __init__(self, on_message: MessageHandler | None,
                 store_reference: Callable[[ConversationReference], None]) -> None:
        self._on_message = on_message
        self._store_reference = store_reference

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        # Remember where the user is so we can message them proactively later.
        reference = TurnContext.get_conversation_reference(turn_context.activity)
        self._store_reference(reference)

        text = (turn_context.activity.text or "").strip()
        if self._on_message is None:
            await turn_context.send_activity("Loop is online, but no handler is wired yet.")
            return

        result = self._on_message(text)
        if hasattr(result, "__await__"):
            result = await result  # support async handlers
        await turn_context.send_activity(str(result))


class TeamsBot:
    """Inbound + proactive Teams bot built on the Bot Framework."""

    def __init__(self, settings: Settings | None = None,
                 orchestrator: Any | None = None,
                 on_message: MessageHandler | None = None) -> None:
        self.settings = settings or get_settings()
        self._orchestrator = orchestrator
        self._on_message = on_message or self._default_handler
        self._references: dict[str, ConversationReference] = {}

        adapter_settings = BotFrameworkAdapterSettings(
            app_id=self.settings.teams_app_id,
            app_password=self.settings.teams_app_password,
        )
        self._adapter = BotFrameworkAdapter(adapter_settings)
        self._adapter.on_turn_error = self._on_turn_error
        self._handler = _LoopActivityHandler(self._on_message, self._store_reference)

    # ------------------------------------------------------------------ #
    # Handler wiring
    # ------------------------------------------------------------------ #
    def set_message_handler(self, handler: MessageHandler) -> None:
        """Register the callback that handles inbound message text."""
        self._on_message = handler
        self._handler = _LoopActivityHandler(self._on_message, self._store_reference)

    def _default_handler(self, text: str) -> str:
        """Fallback handler that forwards to the orchestrator if available."""
        if self._orchestrator is not None and hasattr(self._orchestrator, "handle_text"):
            return self._orchestrator.handle_text(text, source="teams")
        return f"Received: {text}"

    def _store_reference(self, reference: ConversationReference) -> None:
        if reference.conversation and reference.conversation.id:
            self._references[reference.conversation.id] = reference

    # ------------------------------------------------------------------ #
    # aiohttp endpoint
    # ------------------------------------------------------------------ #
    async def messages(self, request: web.Request) -> web.Response:
        """aiohttp handler for the ``/api/messages`` Bot Framework endpoint."""
        if "application/json" not in request.headers.get("Content-Type", ""):
            return web.Response(status=415, text="Expected application/json")

        body = await request.json()
        activity = Activity().deserialize(body)
        auth_header = request.headers.get("Authorization", "")

        response = await self._adapter.process_activity(
            activity, auth_header, self._handler.on_turn
        )
        if response:
            return web.json_response(data=response.body, status=response.status)
        return web.Response(status=201)

    def build_app(self) -> web.Application:
        """Build an aiohttp application exposing ``POST /api/messages``."""
        app = web.Application(middlewares=[aiohttp_error_middleware])
        app.router.add_post("/api/messages", self.messages)
        return app

    async def process_activity(self, body: dict, auth_header: str) -> Any:
        """Process one inbound Bot Framework activity (programmatic entry)."""
        activity = Activity().deserialize(body)
        return await self._adapter.process_activity(
            activity, auth_header, self._handler.on_turn
        )

    # ------------------------------------------------------------------ #
    # Proactive messaging
    # ------------------------------------------------------------------ #
    async def send_proactive(self, conversation_ref: ConversationReference | str,
                             message: str) -> None:
        """Send a proactive message (reminder / approval prompt) into Teams."""
        reference = conversation_ref
        if isinstance(conversation_ref, str):
            reference = self._references.get(conversation_ref)
            if reference is None:
                raise KeyError(f"No stored conversation reference for {conversation_ref!r}")

        async def _send(turn_context: TurnContext) -> None:
            await turn_context.send_activity(message)

        await self._adapter.continue_conversation(
            reference, _send, self.settings.teams_app_id
        )

    async def _on_turn_error(self, turn_context: TurnContext, error: Exception) -> None:
        logger.exception("Teams bot turn error: %s", error)
        await turn_context.send_activity("Sorry, Loop hit an error handling that message.")
