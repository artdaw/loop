"""LLM Router — local-first model routing with a hard privacy gate.

Every request that needs an LLM passes through this router. It enforces the
privacy rules from the architecture plan:

    1. If the data item is flagged local-only -> Ollama only. Full stop.
    2. If the item carries a private-calendar marker -> summarise locally,
       send only the sanitised summary to a cloud model.
    3. Otherwise -> try local Ollama first; escalate to Anthropic only when the
       local model's confidence is below threshold or the caller forces it.

Phase 1 scope:
    - Implement the privacy gate decision function.
    - Implement a local Ollama client call.
    - Stub the Anthropic fallback (wired for real in Phase 3).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

import httpx

from config.settings import Settings, get_settings


class Backend(str, enum.Enum):
    """Which LLM backend a request should be routed to."""

    LOCAL = "local"      # Ollama
    CLOUD = "cloud"      # Anthropic


@dataclass
class RoutingDecision:
    """The outcome of the privacy gate + routing logic."""

    backend: Backend
    reason: str
    sanitised: bool = False  # True when PII was stripped before cloud use


class LLMRouter:
    """Decides which model handles a request and executes the call."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # TODO(phase1): build an httpx.AsyncClient pointed at settings.ollama_base_url.
        # TODO(phase3): lazily construct the Anthropic client from settings.anthropic_api_key.
        self._http = httpx.Client(timeout=60.0)

    def decide(self, *, is_private: bool, has_private_calendar_marker: bool,
               force_backend: Backend | None = None) -> RoutingDecision:
        """Apply the privacy gate and return a routing decision.

        This mirrors the privacy-gate flowchart in the architecture plan.
        """
        if force_backend is not None:
            return RoutingDecision(force_backend, reason="forced by caller")
        if is_private:
            return RoutingDecision(Backend.LOCAL, reason="local-only flagged data")
        if has_private_calendar_marker:
            # TODO(phase1): strip PII + summarise locally before any cloud call.
            return RoutingDecision(Backend.CLOUD, reason="private marker", sanitised=True)
        # TODO(phase1): use local confidence to decide escalation; default to local.
        return RoutingDecision(Backend.LOCAL, reason="default local-first")

    def complete(self, prompt: str, *, decision: RoutingDecision,
                 model: str | None = None) -> str:
        """Run a completion against the backend chosen by ``decision``."""
        if decision.backend is Backend.LOCAL:
            return self._complete_local(prompt, model=model)
        return self._complete_cloud(prompt, model=model)

    def _complete_local(self, prompt: str, *, model: str | None = None) -> str:
        """Call the local Ollama server."""
        # TODO(phase1): POST to {ollama_base_url}/api/generate with the chosen model
        #               (default settings.ollama_default_model) and return the text.
        raise NotImplementedError("LLMRouter._complete_local is a Phase 1 stub.")

    def _complete_cloud(self, prompt: str, *, model: str | None = None) -> str:
        """Call the Anthropic API (fallback)."""
        # TODO(phase3): call Anthropic messages API using settings.anthropic_api_key.
        raise NotImplementedError("LLMRouter._complete_cloud is a Phase 3 stub.")
