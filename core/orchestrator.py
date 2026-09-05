"""Orchestrator — the agent core of Loop.

The orchestrator is the brain of the assistant. It receives events from the
integration layer (new emails, upcoming meetings, inbound chat messages) and
user commands (from the CLI or a chat bot), routes each request to the correct
specialist sub-agent, and composes the final response.

Phase 1 scope:
    - Wire up a minimal LangGraph state graph with the four specialists.
    - Accept an inbound event/message and dispatch it to a specialist.
    - Return a composed response object that the delivery layer can send.

Later phases add conversation memory, multi-step planning, and autonomy levels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config.settings import Settings, get_settings


@dataclass
class AgentEvent:
    """A single unit of work flowing through the orchestrator.

    Attributes:
        source: Where the event came from (e.g. "gmail", "google_calendar", "cli").
        kind: Event type (e.g. "new_email", "meeting_soon", "user_command").
        payload: Arbitrary structured data describing the event.
        is_private: True when the event touches local-only data (privacy gate).
    """

    source: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    is_private: bool = False


@dataclass
class AgentResponse:
    """The orchestrator's answer, ready for the delivery layer."""

    text: str
    channel: str = "auto"  # "telegram" | "teams" | "auto"
    actions: list[dict[str, Any]] = field(default_factory=list)


class Orchestrator:
    """Central coordinator that routes events to specialists and composes replies."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # TODO(phase1): instantiate the LLM router (core.llm_router.LLMRouter).
        # TODO(phase1): instantiate memory store (core.memory.MemoryStore).
        # TODO(phase1): register specialists (Email, Calendar, Tasks, Knowledge)
        #               in a dispatch table keyed by AgentEvent.kind.
        # TODO(phase1): build the LangGraph StateGraph connecting router -> specialist.
        self._specialists: dict[str, Any] = {}

    def register_specialist(self, name: str, specialist: Any) -> None:
        """Register a specialist sub-agent under a routing name."""
        # TODO(phase1): validate that the specialist implements the expected interface.
        self._specialists[name] = specialist

    def handle(self, event: AgentEvent) -> AgentResponse:
        """Route an incoming event to the right specialist and return a response.

        Phase 1 responsibilities:
            1. Pass the event through the privacy gate / LLM router.
            2. Select the specialist based on ``event.kind``.
            3. Delegate and collect the specialist's result.
            4. Compose an ``AgentResponse`` for the delivery layer.
        """
        # TODO(phase1): run privacy gate + choose model via LLMRouter.
        # TODO(phase1): dispatch to the appropriate specialist and compose reply.
        raise NotImplementedError("Orchestrator.handle is a Phase 1 stub.")

    def run_forever(self) -> None:
        """Run the orchestrator as a long-lived daemon.

        Phase 1 responsibilities:
            - Start the scheduler (core.scheduler.Scheduler) for periodic jobs
              (calendar polling, email polling, morning briefing).
            - Start inbound listeners (Telegram/Teams webhooks).
            - Block until interrupted.
        """
        # TODO(phase1): start scheduler + inbound bot listeners, then block.
        raise NotImplementedError("Orchestrator.run_forever is a Phase 1 stub.")
