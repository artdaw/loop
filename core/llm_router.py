"""LLM Router — local-first model routing with a hard privacy gate.

Every request that needs an LLM passes through this router. It enforces the
privacy rules from the architecture plan:

    1. If the data item is flagged ``local_only`` (or its ``source`` is one of
       the private sources) -> Ollama only. Full stop. If Ollama is unavailable
       we raise :class:`PrivacyError` and never fall back to a cloud model.
    2. Otherwise -> try local Ollama first; escalate to Anthropic only when the
       local model's confidence is low (response too short, a refusal phrase,
       latency over budget) or Ollama is unavailable.

Phase 3 additions:
    - Real ``anthropic`` SDK integration (``claude-3-5-sonnet-20241022``).
    - Real ``ollama`` SDK integration.
    - ``async def ask_local(prompt, system_prompt)`` and
      ``async def ask_anthropic(prompt, system_prompt)``.
    - Every request is logged to SQLite (table ``llm_usage_log``) with the
      backend that served it, a prompt hash, latency, and the local_only flag.
    - Cloud calls are hard-blocked for local-only requests (``PrivacyError``).

Public API:
    router = LLMRouter()
    text = router.route("summarise this", {"source": "work"})            # sync
    result = await router.route_async(prompt, {"local_only": True})       # async
    text = await router.ask_local(prompt, system_prompt)                  # async
    text = await router.ask_anthropic(prompt, system_prompt)             # async

The legacy ``decide``/``complete`` helpers are kept for the specialists that
were scaffolded in earlier phases.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from config.settings import Settings, get_settings
from core.exceptions import BackendUnavailableError, PrivacyError

logger = logging.getLogger(__name__)

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

_DEFAULT_SYSTEM = "You are Loop, a concise, helpful personal assistant."


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


def _hash_prompt(prompt: str) -> str:
    """Return a SHA-256 hex digest of the prompt (content is never persisted)."""
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()


class LLMRouter:
    """Decides which model handles a request and executes the call."""

    def __init__(self, settings: Settings | None = None, memory: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._http = httpx.Client(timeout=self.settings.local_latency_budget_seconds + 55.0)
        self._anthropic_sync: Any | None = None   # lazily constructed (sync client)
        self._anthropic_async: Any | None = None  # lazily constructed (async client)
        self._ollama_async: Any | None = None     # lazily constructed
        self._memory = memory  # optional MemoryStore; lazily created for logging

    # ------------------------------------------------------------------ #
    # Fallback heuristics
    # ------------------------------------------------------------------ #
    @property
    def _min_chars(self) -> int:
        """Minimum acceptable local response length before a cloud fallback."""
        return getattr(self.settings, "llm_fallback_min_chars",
                       self.settings.local_min_response_chars)

    @property
    def _latency_budget(self) -> float:
        """Latency (seconds) above which we escalate to the cloud."""
        return getattr(self.settings, "llm_fallback_latency_seconds",
                       self.settings.local_latency_budget_seconds)

    def _is_low_confidence(self, text: str, latency: float) -> bool:
        """Heuristic: should we escalate this local response to the cloud?

        Triggers: response too short (< ``llm_fallback_min_chars``), contains a
        refusal phrase ("I don't know" / "I'm not sure"), or latency over budget.
        """
        stripped = (text or "").strip()
        if len(stripped) < self._min_chars:
            return True
        lowered = stripped.lower()
        if any(marker in lowered for marker in _REFUSAL_MARKERS):
            return True
        if latency > self._latency_budget:
            return True
        return False

    # ------------------------------------------------------------------ #
    # Usage logging
    # ------------------------------------------------------------------ #
    def _log(self, *, backend: Backend, prompt_hash: str, latency_seconds: float,
             local_only: bool) -> None:
        """Persist a usage row to SQLite (never raises)."""
        try:
            store = self._get_memory()
            store.log_llm_usage(
                backend=backend.value,
                prompt_hash=prompt_hash,
                latency_ms=int(latency_seconds * 1000),
                local_only=local_only,
            )
        except Exception:  # noqa: BLE001 - logging must never break a request
            logger.debug("Failed to record LLM usage", exc_info=True)

    def _get_memory(self) -> Any:
        if self._memory is None:
            from core.memory import MemoryStore  # local import avoids a cycle

            store = MemoryStore(self.settings)
            store.bootstrap()
            self._memory = store
        return self._memory

    # ------------------------------------------------------------------ #
    # Async backend calls (Phase 3)
    # ------------------------------------------------------------------ #
    async def ask_local(self, prompt: str, system_prompt: str | None = None,
                        *, model: str | None = None) -> str:
        """Call the local Ollama model via the official ``ollama`` async SDK.

        Raises:
            BackendUnavailableError: when Ollama cannot be reached.
        """
        client = self._get_ollama_async()
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        try:
            response = await client.chat(
                model=model or self.settings.ollama_default_model,
                messages=messages,
                stream=False,
            )
        except Exception as exc:  # noqa: BLE001 - normalise to our error type
            raise BackendUnavailableError(f"Ollama unavailable: {exc}") from exc
        # ollama returns {"message": {"content": ...}} (dict or object).
        message = response.get("message") if isinstance(response, dict) else getattr(response, "message", None)
        if isinstance(message, dict):
            return (message.get("content") or "").strip()
        return (getattr(message, "content", "") or "").strip()

    async def ask_anthropic(self, prompt: str, system_prompt: str | None = None,
                            *, model: str | None = None) -> str:
        """Call Anthropic's Claude via the official async SDK (cloud fallback only).

        Raises:
            BackendUnavailableError: when no API key is configured or the call fails.
        """
        if not self.settings.anthropic_api_key:
            raise BackendUnavailableError("ANTHROPIC_API_KEY is not configured")
        client = self._get_anthropic_async()
        try:
            message = await client.messages.create(
                model=model or self.settings.anthropic_model,
                max_tokens=1024,
                system=system_prompt or _DEFAULT_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - normalise to our error type
            raise BackendUnavailableError(f"Anthropic call failed: {exc}") from exc
        parts = [block.text for block in message.content
                 if getattr(block, "type", "") == "text"]
        return "".join(parts).strip()

    async def route_async(self, prompt: str, context_metadata: dict[str, Any] | None = None,
                          *, model: str | None = None, system: str | None = None) -> LLMResult:
        """Async privacy-gated routing with local-first + Anthropic fallback.

        This is the Phase 3 primary entry point. It logs every request to the
        ``llm_usage_log`` SQLite table.

        Raises:
            PrivacyError: when a local-only request cannot be served locally
                (we NEVER fall back to the cloud for such requests).
            BackendUnavailableError: when a non-private request cannot be served
                by either backend.
        """
        meta = context_metadata or {}
        local_only = bool(meta.get("local_only", False))
        source = str(meta.get("source", "work"))
        decision = self.decide_route(local_only=local_only, source=source)
        prompt_hash = _hash_prompt(prompt)

        # --- Local-only path: Ollama or bust (fail closed). --------------
        if decision.local_only:
            start = time.monotonic()
            try:
                text = await self.ask_local(prompt, system, model=model)
            except Exception as exc:  # noqa: BLE001 - never leak to the cloud
                raise PrivacyError(
                    f"Local-only request (source={source!r}) could not be served by "
                    f"Ollama and must NOT fall back to a cloud model: {exc}"
                ) from exc
            latency = time.monotonic() - start
            self._log(backend=Backend.LOCAL, prompt_hash=prompt_hash,
                      latency_seconds=latency, local_only=True)
            return LLMResult(text=text, backend=Backend.LOCAL, reason=decision.reason,
                             latency_seconds=latency,
                             metadata={"source": source, "local_only": True})

        # --- Non-private path: try local first, fall back if weak. -------
        start = time.monotonic()
        local_text: str | None = None
        try:
            local_text = await self.ask_local(prompt, system, model=model)
            latency = time.monotonic() - start
            if not self._is_low_confidence(local_text, latency):
                self._log(backend=Backend.LOCAL, prompt_hash=prompt_hash,
                          latency_seconds=latency, local_only=False)
                return LLMResult(text=local_text, backend=Backend.LOCAL,
                                 reason="local-first (confident)",
                                 latency_seconds=latency, metadata={"source": source})
            fallback_reason = "local response low-confidence -> cloud fallback"
        except Exception as exc:  # noqa: BLE001 - local down; try the cloud
            fallback_reason = f"local backend error -> cloud fallback ({exc})"

        # --- Cloud fallback (Anthropic). --------------------------------
        cloud_start = time.monotonic()
        try:
            text = await self.ask_anthropic(prompt, system, model=None)
        except BackendUnavailableError:
            # Cloud not available. If we at least have a weak local answer, use it.
            if local_text is not None:
                latency = time.monotonic() - start
                self._log(backend=Backend.LOCAL, prompt_hash=prompt_hash,
                          latency_seconds=latency, local_only=False)
                return LLMResult(text=local_text, backend=Backend.LOCAL,
                                 reason="cloud unavailable; used weak local answer",
                                 latency_seconds=latency, metadata={"source": source})
            raise
        latency = time.monotonic() - cloud_start
        self._log(backend=Backend.CLOUD, prompt_hash=prompt_hash,
                  latency_seconds=latency, local_only=False)
        return LLMResult(text=text, backend=Backend.CLOUD, reason=fallback_reason,
                         latency_seconds=latency, fell_back=True,
                         metadata={"source": source})

    # ------------------------------------------------------------------ #
    # Public sync API (kept for existing specialists)
    # ------------------------------------------------------------------ #
    def route(self, prompt: str, context_metadata: dict[str, Any] | None = None,
              *, model: str | None = None, system: str | None = None) -> str:
        """Route a prompt through the privacy gate and return the completion text."""
        return self.route_detailed(prompt, context_metadata, model=model, system=system).text

    def route_detailed(self, prompt: str, context_metadata: dict[str, Any] | None = None,
                       *, model: str | None = None, system: str | None = None) -> LLMResult:
        """Synchronous privacy-gated routing (local-first with cloud fallback)."""
        meta = context_metadata or {}
        local_only = bool(meta.get("local_only", False))
        source = str(meta.get("source", "work"))
        decision = self.decide_route(local_only=local_only, source=source)
        prompt_hash = _hash_prompt(prompt)

        # --- Local-only path: Ollama or bust. ---------------------------
        if decision.local_only:
            try:
                start = time.monotonic()
                text = self._complete_local(prompt, model=model, system=system)
                latency = time.monotonic() - start
                self._log(backend=Backend.LOCAL, prompt_hash=prompt_hash,
                          latency_seconds=latency, local_only=True)
                return LLMResult(
                    text=text, backend=Backend.LOCAL, reason=decision.reason,
                    latency_seconds=latency,
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
                self._log(backend=Backend.LOCAL, prompt_hash=prompt_hash,
                          latency_seconds=latency, local_only=False)
                return LLMResult(
                    text=text, backend=Backend.LOCAL, reason="local-first (confident)",
                    latency_seconds=latency, metadata={"source": source},
                )
            fallback_reason = "local response low-confidence -> cloud fallback"
        except Exception as exc:  # noqa: BLE001 - local down; try the cloud
            fallback_reason = f"local backend error -> cloud fallback ({exc})"

        # --- Cloud fallback (Anthropic). --------------------------------
        try:
            cloud_start = time.monotonic()
            text = self._complete_cloud(prompt, system=system)
            latency = time.monotonic() - cloud_start
            self._log(backend=Backend.CLOUD, prompt_hash=prompt_hash,
                      latency_seconds=latency, local_only=False)
            return LLMResult(
                text=text, backend=Backend.CLOUD, reason=fallback_reason,
                latency_seconds=latency, fell_back=True,
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

    # ------------------------------------------------------------------ #
    # Sync backend calls
    # ------------------------------------------------------------------ #
    def _complete_local(self, prompt: str, *, model: str | None = None,
                        system: str | None = None) -> str:
        """Call the local Ollama server's /api/generate endpoint (sync)."""
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
        """Call the Anthropic messages API (sync, fallback only)."""
        if not self.settings.anthropic_api_key:
            raise BackendUnavailableError("ANTHROPIC_API_KEY is not configured")
        client = self._get_anthropic_sync()
        message = client.messages.create(
            model=model or self.settings.anthropic_model,
            max_tokens=1024,
            system=system or _DEFAULT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [block.text for block in message.content
                 if getattr(block, "type", "") == "text"]
        return "".join(parts).strip()

    # ------------------------------------------------------------------ #
    # Lazy SDK clients
    # ------------------------------------------------------------------ #
    def _get_anthropic_sync(self) -> Any:
        if self._anthropic_sync is None:
            import anthropic

            self._anthropic_sync = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._anthropic_sync

    def _get_anthropic_async(self) -> Any:
        if self._anthropic_async is None:
            import anthropic

            self._anthropic_async = anthropic.AsyncAnthropic(
                api_key=self.settings.anthropic_api_key
            )
        return self._anthropic_async

    def _get_ollama_async(self) -> Any:
        if self._ollama_async is None:
            import ollama

            self._ollama_async = ollama.AsyncClient(host=self.settings.ollama_base_url)
        return self._ollama_async

    # ------------------------------------------------------------------ #
    # Legacy helpers (kept for the scaffolded specialists)
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
