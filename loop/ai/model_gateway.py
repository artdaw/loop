"""ModelGateway — the single policy adapter around LangChain chat models.

Every model call in Loop goes through here: agent turns, summaries, schema
repairs and child runs alike. Applying the policy in one place is what makes the
privacy guarantee checkable — a second call path would be a second place to get
it wrong.

The gateway is deliberately **small**. It does not wrap or re-implement the
LangChain model API; it decides *which* model a labelled context may use,
reserves budget, and records metadata. The actual invocation is a standard
`BaseChatModel.invoke`.

Three rules it exists to enforce:

* **A private context never reaches the cloud.** If the label is local-only and
  the local model fails, that is terminal — `PrivacyError` — not a fallback.
  This holds for derived content too: a summary of private input inherits the
  label, so "summarise it first, then send the summary" is not an escape hatch.
* **A caller cannot downgrade a label.** Merging is via
  `PrivacyLabel.merge`, where restrictions spread and permissions shrink, so a
  supplied `local_only: false` cannot lower a true one.
* **Short answers and slow answers are not confidence signals.** The legacy
  router escalated to the cloud when a local reply looked "weak" by length or
  latency. Specification 1.3 forbids that: those are not measurements of
  quality, and using them silently sends private-adjacent work to a third party.

Audit records the backend, a prompt *hash*, latency and token counts. Never the
prompt, the response, or any content.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from loop.ai.budget import RootBudget
from loop.core.clock import Clock, SystemClock
from loop.core.errors import PrivacyBlocked, Unavailable
from loop.core.ids import content_hash
from loop.core.privacy import PrivacyLabel
from loop.core.settings import Settings

logger = logging.getLogger(__name__)


class PrivacyError(PrivacyBlocked):
    """A local-only request could not be served locally. Never falls back."""


@dataclass
class ModelCallRecord:
    """Audit metadata for one model call. Content never appears here."""

    backend: str
    prompt_hash: str
    latency_ms: int
    local_only: bool
    input_tokens: int = 0
    output_tokens: int = 0
    error_code: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ModelResult:
    """A completion plus how it was produced."""

    text: str
    backend: str
    latency_ms: int
    local_only: bool
    tokens: int = 0
    raw: Any = None


@dataclass
class GatewayAudit:
    """In-memory audit sink. The durable sink is the `audit` table."""

    records: list[ModelCallRecord] = field(default_factory=list)

    def record(self, entry: ModelCallRecord) -> None:
        self.records.append(entry)

    @property
    def cloud_calls(self) -> int:
        return sum(1 for r in self.records if r.backend == "cloud")

    @property
    def local_calls(self) -> int:
        return sum(1 for r in self.records if r.backend == "local")


class ModelGateway:
    """Chooses a permitted model, enforces budget, and records metadata."""

    def __init__(self, *, settings: Settings,
                 local_model: Any | None = None,
                 cloud_model: Any | None = None,
                 clock: Clock | None = None,
                 audit: GatewayAudit | None = None) -> None:
        self._settings = settings
        # Models are injected so tests use a real LangChain fake chat model
        # rather than a hand-rolled stand-in: the call path under test is the
        # genuine one.
        self._local_model = local_model
        self._cloud_model = cloud_model
        self._clock = clock or SystemClock()
        self.audit = audit or GatewayAudit()

    # ------------------------------------------------------------------ #
    # Model construction
    # ------------------------------------------------------------------ #
    def local_model(self) -> Any:
        """The local chat model (`ChatOllama`), built lazily."""
        if self._local_model is None:
            if not self._settings.ollama_default_model.strip():
                raise Unavailable(
                    "No local model selected. Set OLLAMA_DEFAULT_MODEL, or run "
                    "`loop init` to pin an installed model.")
            from langchain_ollama import ChatOllama

            self._local_model = ChatOllama(
                model=self._settings.ollama_default_model,
                base_url=self._settings.ollama_base_url,
                # Provider-level retries are disabled: the graph and job layers
                # own retry policy, and stacking them multiplies allowances
                # (agent-stack §3).
                num_predict=None,
            )
        return self._local_model

    def cloud_model(self) -> Any:
        """The cloud chat model (`ChatAnthropic`), built lazily."""
        if self._cloud_model is None:
            if not self._settings.cloud_available:
                raise Unavailable(
                    "Cloud model is not available: it requires CLOUD_ENABLED, an "
                    "API key, an explicit model id and a positive daily budget.")
            from langchain_anthropic import ChatAnthropic
            from pydantic import SecretStr

            # langchain-anthropic 1.7 declares the field as `model` with alias
            # `model_name`, and the key as SecretStr. Both spellings work at
            # runtime; these are the declared types, so the checker agrees too.
            self._cloud_model = ChatAnthropic(
                model_name=self._settings.anthropic_model,
                api_key=SecretStr(self._settings.anthropic_api_key),
                max_retries=0,   # explicit retry policy lives above, not here
                # `timeout` and `stop` are optional at runtime, but their
                # aliased declarations make the type checker treat them as
                # required. Passing the explicit no-op values satisfies both
                # rather than silencing the checker.
                timeout=None,
                stop=None,
            )
        return self._cloud_model

    # ------------------------------------------------------------------ #
    # Policy
    # ------------------------------------------------------------------ #
    def resolve_scope(self, labels: Sequence[PrivacyLabel]) -> PrivacyLabel:
        """Merge every contributing label before deciding anything.

        Includes the labels of retrieved documents, conversation history, tool
        results and child output — not just the original message.
        """
        return PrivacyLabel.merge(list(labels))

    def may_use_cloud(self, label: PrivacyLabel) -> bool:
        """Cloud needs a non-private label *and* full configured authorisation."""
        if label.is_local_only:
            return False
        return self._settings.cloud_available

    # ------------------------------------------------------------------ #
    # Invocation
    # ------------------------------------------------------------------ #
    def invoke(self, prompt: str, *, labels: Sequence[PrivacyLabel] | None = None,
               budget: RootBudget | None = None,
               system: str | None = None,
               estimated_tokens: int = 0,
               purpose: str = "agent") -> ModelResult:
        """Run one policy-checked model call.

        ``purpose`` distinguishes agent turns from summaries, repairs and child
        runs in the audit trail. All of them take this path — that is what makes
        "no cloud call for private input" a property of the system rather than
        of one call site.
        """
        label = self.resolve_scope(labels or [PrivacyLabel.for_unlabelled_import()])
        prompt_hash = content_hash(prompt)

        if budget is not None:
            budget.reserve_model_call(estimated_tokens=estimated_tokens)

        if label.is_local_only:
            return self._invoke_local_only(prompt, system, label, prompt_hash,
                                           budget, purpose)
        return self._invoke_preferring_local(prompt, system, label, prompt_hash,
                                             budget, purpose)

    def _invoke_local_only(self, prompt: str, system: str | None,
                           label: PrivacyLabel, prompt_hash: str,
                           budget: RootBudget | None, purpose: str) -> ModelResult:
        """Fail closed. A private request never reaches a cloud provider."""
        try:
            return self._call(self.local_model(), prompt, system, "local",
                              label, prompt_hash, budget, purpose)
        except Exception as exc:
            if budget is not None:
                budget.record_failure()
            self.audit.record(ModelCallRecord(
                backend="local", prompt_hash=prompt_hash, latency_ms=0,
                local_only=True, error_code="privacy_blocked"))
            # Deliberately not a fallback: the alternative to answering privately
            # is not answering, never answering elsewhere.
            raise PrivacyError(
                "This request is local-only and the local model is unavailable. "
                "It will not be sent to a cloud provider.",
                details={"purpose": purpose},
            ) from exc

    def _invoke_preferring_local(self, prompt: str, system: str | None,
                                 label: PrivacyLabel, prompt_hash: str,
                                 budget: RootBudget | None,
                                 purpose: str) -> ModelResult:
        """Non-private work: local first, cloud only on local *unavailability*.

        Note what is absent: no length check, no latency check. A short or slow
        local answer is still an answer (agent-stack §3).
        """
        try:
            return self._call(self.local_model(), prompt, system, "local",
                              label, prompt_hash, budget, purpose)
        except Exception as exc:
            logger.info("Local model unavailable (%s); considering cloud", exc)
            if budget is not None:
                budget.record_failure()
            if not self.may_use_cloud(label):
                raise Unavailable(
                    "The local model is unavailable and cloud use is not "
                    "authorised for this request.") from exc
            return self._call(self.cloud_model(), prompt, system, "cloud",
                              label, prompt_hash, budget, purpose)

    def _call(self, model: Any, prompt: str, system: str | None, backend: str,
              label: PrivacyLabel, prompt_hash: str, budget: RootBudget | None,
              purpose: str) -> ModelResult:
        from langchain_core.messages import HumanMessage, SystemMessage

        messages: list[Any] = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=prompt))

        started = time.monotonic()
        response = model.invoke(messages)
        latency_ms = int((time.monotonic() - started) * 1000)

        text = _response_text(response)
        tokens = _response_tokens(response)
        if budget is not None and tokens:
            budget.record_tokens(tokens)

        self.audit.record(ModelCallRecord(
            backend=backend, prompt_hash=prompt_hash, latency_ms=latency_ms,
            local_only=label.is_local_only, output_tokens=tokens))
        logger.debug("Model call: backend=%s purpose=%s latency=%dms",
                     backend, purpose, latency_ms)

        return ModelResult(text=text, backend=backend, latency_ms=latency_ms,
                           local_only=label.is_local_only, tokens=tokens,
                           raw=response)

    # ------------------------------------------------------------------ #
    # Feature support
    # ------------------------------------------------------------------ #
    def supports_tool_calling(self, *, backend: str = "local") -> bool:
        """Whether the configured model can call tools.

        A missing capability is a configuration limitation to report, never a
        reason to quietly use the cloud instead (LG12).
        """
        model = self.local_model() if backend == "local" else self.cloud_model()
        return hasattr(model, "bind_tools")

    def describe_limitations(self) -> list[str]:
        """Human-readable configuration limitations, for `loop doctor`."""
        problems: list[str] = []
        if not self._settings.ollama_default_model.strip():
            problems.append("No local model selected (OLLAMA_DEFAULT_MODEL).")
        if not self._settings.cloud_available:
            problems.append(
                "Cloud model unconfigured: local-only operation. Non-private "
                "work still runs locally; nothing silently escalates.")
        return problems


def _response_text(response: Any) -> str:
    """Extract text from a LangChain message, tolerating block content."""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [block.get("text", "") if isinstance(block, dict) else str(block)
                 for block in content]
        return "".join(parts)
    return str(content)


def _response_tokens(response: Any) -> int:
    """Read token usage when the provider reported it; 0 when it did not."""
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        return int(usage.get("total_tokens") or 0)
    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, dict):
        token_usage = metadata.get("token_usage") or {}
        if isinstance(token_usage, dict):
            return int(token_usage.get("total_tokens") or 0)
    return 0
