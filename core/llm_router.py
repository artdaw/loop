"""LLM Router — local-first model routing with a hard privacy gate.

Every request that needs an LLM passes through this router. It enforces the
privacy rules from the architecture plan:

    1. If the data item is flagged ``local_only`` (or its ``source`` is one of
       the private sources) -> Ollama only. Full stop. If Ollama is unavailable
       we raise :class:`PrivacyError` and never fall back to a cloud model.
    2. Otherwise -> try local Ollama first; escalate to Anthropic only when the
       local model's confidence is low (response too short, a refusal phrase,
       or latency over budget) or the caller forces a backend.

Public API (Phase 2):
    router = LLMRouter()
    text = router.route("summarise this", {"source": "work"})
    text = router.route(prompt, {"local_only": True, "source": "obsidian_private"})

The legacy ``decide``/``complete`` helpers are kept for the specialists that
were scaffolded in Phase 1.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from config.settings import Settings, get_settings
from core.exceptions import BackendUnavailableError, PrivacyError

# Sources whose data must never leave the machine.
PRIVATE_SOURCES: frozenset[str] = frozenset(
    {"obsidian_private", "personal_calendar", "telegram_private"}
)

# Phrases that suggest the local model could not answer confidently.
_REFUSAL_MARKERS: tuple[str, ...] = (
    "i don't know",
    "i do not know",
    "i'm not sure",
    "i am not sure",
    "cannot answer",
    "can't answer",
    "no information",
)


class Backend(str, enum.Enum):
    """Which LLM backend a request should be routed to."""

    LOCAL = "local"      # Ollama
    CLOUD = "cloud"      # Anthropic


@dataclass
class RoutingDecision:
    """The outcome of the privacy gate + routing logic."""

    backend: Backend
    reason: str
    sanitised: bool = False       # True when PII was stripped before cloud use
    local_only: bool = False      # True when cloud fallback is forbidden


@dataclass
class LLMResult:
    """A completion plus the metadata about how it was produced."""

    text: str
    backend: Backend
    reason: str
    latency_seconds: float = 0.0
    fell_back: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class LLMRouter:
    """Decides which model handles a request and executes the call."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._http = httpx.Client(timeout=self.settings.local_latency_budget_seconds + 55.0)
        self._anthropic: Any | None = None  # lazily constructed

    # ------------------------------------------------------------------ #
    # Public high-level API
    # ------------------------------------------------------------------ #
    def route(self, prompt: str, context_metadata: dict[str, Any] | None = None,
              *, model: str | None = None, system: str | None = None) -> str:
        """Route a prompt through the privacy gate and return the completion text.

        Args:
            prompt: The user/prompt text to complete.
            context_metadata: Optional dict with keys:
                ``local_only`` (bool) and ``source`` (str). When ``local_only``
                is True, or ``source`` is one of ``PRIVATE_SOURCES``, the request
                is served by Ollama only.
            model: Optional Ollama model override.
            system: Optional system prompt.

        Returns:
            The completion text.

        Raises:
            PrivacyError: when a local-only request cannot be served locally.
        """
        return self.route_detailed(prompt, context_metadata, model=model, system=system).text

    def route_detailed(self, prompt: str, context_metadata: dict[str, Any] | None = None,
                       *, model: str | None = None, system: str | None = None) -> LLMResult:
        """Like :meth:`route` but returns a rich :class:`LLMResult`."""
        meta = context_metadata or {}
        local_only = bool(meta.get("local_only", False))
        source = str(meta.get("source", "work"))

        decision = self.decide_route(local_only=local_only, source=source)

        # --- Local-only path: Ollama or bust. ---------------------------
        if decision.local_only:
            try:
                start = time.monotonic()
                text = self._complete_local(prompt, model=model, system=system)
                return LLMResult(
                    text=text,
                    backend=Backend.LOCAL,
                    reason=decision.reason,
                    latency_seconds=time.monotonic() - start,
                    metadata={"source": source, "local_only": True},
                )
            except Exception as exc:  # noqa: BLE001 - fail closed on any local error
                raise PrivacyError(
                    f"Local-only request (source={source!r}) could not be served by "
                    f"Ollama and must NOT fall back to a cloud model: {exc}"
                ) from exc

        # --- Non-private path: try local first, fall back if weak. -------
        start = time.monotonic()
        try:
            text = self._complete_local(prompt, model=model, system=system)
            latency = time.monotonic() - start
            if not self._is_low_confidence(text, latency):
                return LLMResult(
                    text=text, backend=Backend.LOCAL, reason="local-first (confident)",
                    latency_seconds=latency, metadata={"source": source},
                )
            fallback_reason = "local response low-confidence -> cloud fallback"
        except Exception as exc:  # noqa: BLE001 - local down; try the cloud
            latency = time.monotonic() - start
            fallback_reason = f"local backend error -> cloud fallback ({exc})"

        # --- Cloud fallback (Anthropic). --------------------------------
        try:
            cloud_start = time.monotonic()
            text = self._complete_cloud(prompt, system=system)
            return LLMResult(
                text=text, backend=Backend.CLOUD, reason=fallback_reason,
                latency_seconds=time.monotonic() - cloud_start, fell_back=True,
                metadata={"source": source},
            )
        except BackendUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - both backends failed
            raise BackendUnavailableError(str(exc)) from exc

    # ------------------------------------------------------------------ #
    # Decision logic
    # ------------------------------------------------------------------ #
    def decide_route(self, *, local_only: bool, source: str) -> RoutingDecision:
        """Apply the privacy gate and return a routing decision."""
        if local_only:
            return RoutingDecision(Backend.LOCAL, reason="local_only flag set",
                                   local_only=True)
        if source in PRIVATE_SOURCES:
            return RoutingDecision(Backend.LOCAL, reason=f"private source: {source}",
                                   local_only=True)
        return RoutingDecision(Backend.LOCAL, reason="default local-first")

    def _is_low_confidence(self, text: str, latency: float) -> bool:
        """Heuristic: should we escalate this local response to the cloud?"""
        stripped = (text or "").strip()
        if len(stripped) < self.settings.local_min_response_chars:
            return True
        lowered = stripped.lower()
        if any(marker in lowered for marker in _REFUSAL_MARKERS):
            return True
        if latency > self.settings.local_latency_budget_seconds:
            return True
        return False

    # ------------------------------------------------------------------ #
    # Backend calls
    # ------------------------------------------------------------------ #
    def _complete_local(self, prompt: str, *, model: str | None = None,
                        system: str | None = None) -> str:
        """Call the local Ollama server's /api/generate endpoint."""
        url = f"{self.settings.ollama_base_url.rstrip('/')}/api/generate"
        payload: dict[str, Any] = {
            "model": model or self.settings.ollama_default_model,
            "prompt": prompt,
            "stream": False,
        }
        if system:
            payload["system"] = system
        resp = self._http.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return (data.get("response") or "").strip()

    def _complete_cloud(self, prompt: str, *, model: str | None = None,
                        system: str | None = None) -> str:
        """Call the Anthropic messages API (fallback only)."""
        if not self.settings.anthropic_api_key:
            raise BackendUnavailableError("ANTHROPIC_API_KEY is not configured")
        client = self._get_anthropic()
        message = client.messages.create(
            model=model or self.settings.anthropic_model,
            max_tokens=1024,
            system=system or "You are Loop, a concise personal assistant.",
            messages=[{"role": "user", "content": prompt}],
        )
        # Anthropic returns a list of content blocks.
        parts = [block.text for block in message.content if getattr(block, "type", "") == "text"]
        return "".join(parts).strip()

    def _get_anthropic(self) -> Any:
        """Lazily construct the Anthropic client."""
        if self._anthropic is None:
            import anthropic  # local import keeps startup light

            self._anthropic = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._anthropic

    # ------------------------------------------------------------------ #
    # Legacy Phase 1 helpers (kept for the scaffolded specialists)
    # ------------------------------------------------------------------ #
    def decide(self, *, is_private: bool, has_private_calendar_marker: bool,
               force_backend: Backend | None = None) -> RoutingDecision:
        """Legacy privacy-gate decision (Phase 1 API)."""
        if force_backend is not None:
            return RoutingDecision(force_backend, reason="forced by caller",
                                   local_only=force_backend is Backend.LOCAL)
        if is_private:
            return RoutingDecision(Backend.LOCAL, reason="local-only flagged data",
                                   local_only=True)
        if has_private_calendar_marker:
            return RoutingDecision(Backend.CLOUD, reason="private marker", sanitised=True)
        return RoutingDecision(Backend.LOCAL, reason="default local-first")

    def complete(self, prompt: str, *, decision: RoutingDecision,
                 model: str | None = None) -> str:
        """Run a completion against the backend chosen by ``decision``."""
        if decision.backend is Backend.LOCAL:
            return self._complete_local(prompt, model=model)
        return self._complete_cloud(prompt, model=model)
