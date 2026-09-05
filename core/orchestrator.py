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

    def __init__(self, settings: Settings | None = None, *,
                 router: Any | None = None, vectors: Any | None = None,
                 conversation: Any | None = None) -> None:
        self.settings = settings or get_settings()
        # Collaborators are injectable for testing; created lazily otherwise.
        self._router = router
        self._vectors = vectors
        self._conversation = conversation
        self._specialists: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # Lazy collaborators
    # ------------------------------------------------------------------ #
    def _get_router(self) -> Any:
        if self._router is None:
            from core.llm_router import LLMRouter
            self._router = LLMRouter(self.settings)
        return self._router

    def _get_vectors(self) -> Any:
        if self._vectors is None:
            from core.vector_store import VectorStore
            self._vectors = VectorStore(self.settings)
        return self._vectors

    def _get_conversation(self) -> Any:
        if self._conversation is None:
            from core.memory import ConversationMemory
            self._conversation = ConversationMemory(self.settings)
        return self._conversation

    def register_specialist(self, name: str, specialist: Any) -> None:
        """Register a specialist sub-agent under a routing name."""
        self._specialists[name] = specialist

    # ------------------------------------------------------------------ #
    # Free-form Q&A (Phase 3)
    # ------------------------------------------------------------------ #
    async def ask(self, question: str, *, session_id: str = "cli",
                  local_only: bool | None = None,
                  n_context: int = 5) -> AgentResponse:
        """Answer a free-form question using memory + retrieved knowledge.

        Pipeline:
            1. Semantic search over the ChromaDB knowledge/email collections.
            2. Load recent conversation turns for continuity.
            3. Compose a grounded prompt (retrieved context + history + Q).
            4. Route through the privacy-gated LLM router (local-first, with
               Anthropic fallback for non-private work). If any retrieved
               source is private, force local-only so private content never
               reaches the cloud.
            5. Persist both turns to conversation memory.

        Args:
            question: the user's natural-language question.
            session_id: conversation session key (defaults to "cli").
            local_only: force local-only inference (overrides auto-detection).
            n_context: number of knowledge snippets to retrieve.
        """
        vectors = self._get_vectors()
        conversation = self._get_conversation()
        router = self._get_router()

        # --- 1. Retrieve relevant context --------------------------------
        try:
            hits = vectors.semantic_search(question, n_results=n_context)
        except Exception:  # noqa: BLE001 - retrieval is best-effort
            hits = []

        # A private source forces local-only unless the caller overrode it.
        has_private = any(
            str((h.metadata or {}).get("is_personal", "")).lower() in ("true", "1")
            or (h.metadata or {}).get("collection") == "personal"
            for h in hits
        )
        force_local = local_only if local_only is not None else has_private

        context_block = self._format_context(hits)

        # --- 2. Conversation history -------------------------------------
        history = await conversation.get_recent(session_id, n=10)
        history_block = "\n".join(
            f"{turn['role']}: {turn['content']}" for turn in history
        )

        # --- 3. Compose the prompt ---------------------------------------
        system = (
            "You are Loop, a concise personal assistant. Answer the user's "
            "question using the provided context and conversation history. If "
            "the context is insufficient, say so honestly rather than inventing "
            "facts."
        )
        prompt_parts: list[str] = []
        if context_block:
            prompt_parts.append(f"Relevant context:\n{context_block}")
        if history_block:
            prompt_parts.append(f"Conversation so far:\n{history_block}")
        prompt_parts.append(f"User question: {question}")
        prompt = "\n\n".join(prompt_parts)

        # --- 4. Route through the privacy-gated LLM ----------------------
        result = await router.route_async(
            prompt,
            {"local_only": force_local, "source": "personal" if force_local else "work"},
            system=system,
        )

        # --- 5. Persist the exchange -------------------------------------
        await conversation.save_turn("user", question, session_id)
        await conversation.save_turn("assistant", result.text, session_id)

        return AgentResponse(
            text=result.text,
            actions=[{"backend": getattr(result.backend, "value", str(result.backend)),
                      "local_only": force_local,
                      "sources": len(hits)}],
        )

    @staticmethod
    def _format_context(hits: list) -> str:
        """Render retrieved search hits into a compact context block."""
        lines: list[str] = []
        for i, hit in enumerate(hits, start=1):
            meta = hit.metadata or {}
            title = meta.get("title") or meta.get("subject") or meta.get("path") or "source"
            snippet = (hit.document or "").strip().replace("\n", " ")
            if len(snippet) > 500:
                snippet = snippet[:500] + "\u2026"
            lines.append(f"[{i}] {title}: {snippet}")
        return "\n".join(lines)

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
