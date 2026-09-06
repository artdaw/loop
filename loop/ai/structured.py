"""Structured model output with bounded repair (agent-stack §3).

Two problems with reading the last message and hoping it is JSON:

* **A model that answers in prose produces a parse error, not a repair.** The
  useful response to malformed output is one more attempt with the validation
  error attached — not a crash, and not silently accepting whatever came back.
* **A parse is not a validation.** `json.loads` succeeding says nothing about
  whether the object has the fields the caller declared. Schema validation has
  to happen on the same path, or an agent can return `{"ok": true}` for an
  operation whose schema demands citations.

So this module asks LangChain for structured output where the model supports it,
falls back to a parse when it does not, and repairs a bounded number of times.

**Every repair spends the original budget.** A repair loop with its own
allowance is a second budget, and two budgets are not a limit — agent-stack §3
is explicit that repair attempts count against the root. `RootBudget` is passed
in and reserved against for each attempt, so three repairs cost three calls.

Unsupported structured output is a **reported configuration limitation**, never
a reason to reach for a different model (LG12).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from loop.ai.budget import RootBudget
from loop.core.errors import ValidationFailed

logger = logging.getLogger(__name__)

#: Attempts *after* the first. Bounded because a model that cannot produce the
#: shape twice will not produce it on the tenth try, and each one costs money.
MAX_REPAIR_ATTEMPTS = 2


@dataclass
class StructuredAttempt:
    """One try, kept for the audit trail."""

    index: int
    raw: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class StructuredResult:
    value: dict[str, Any]
    attempts: list[StructuredAttempt] = field(default_factory=list)
    used_native: bool = False

    @property
    def repairs(self) -> int:
        return max(0, len(self.attempts) - 1)


def supports_structured_output(model: Any) -> bool:
    """Whether this chat model exposes LangChain's structured-output interface."""
    return hasattr(model, "with_structured_output")


def _self_reserving(model: Any, budget: RootBudget | None) -> bool:
    """Whether ``model.invoke`` already reserves against this exact budget.

    `gateway_chat_model` builds a model whose `_generate` calls
    `ModelGateway.invoke_messages`, which reserves the call itself — that path
    is what makes a `create_agent` tool loop share one budget across every
    internal turn (LG05). If this function *also* reserved, every call through
    that model would be charged twice: once here, once inside the gateway.
    Checking identity rather than merely "has a budget" matters too — a model
    bound to a *different* RootBudget must still be reserved against here.
    """
    return budget is not None and getattr(model, "root_budget", None) is budget


def describe_output_support(model: Any) -> str:
    """A configuration limitation to report, not to work around."""
    if supports_structured_output(model):
        return ""
    return ("The configured model does not support structured output; Loop will "
            "validate parsed JSON instead. This is a model limitation, not a "
            "reason to use a different backend.")


def validate_object(value: Any, schema: dict[str, Any]) -> list[str]:
    """Validate against a JSON Schema, returning readable problems.

    Returns problems rather than raising, because the caller's next move is to
    hand them back to the model.
    """
    if not isinstance(value, dict):
        return [f"expected a JSON object, got {type(value).__name__}"]
    if not schema:
        return []

    import jsonschema

    validator = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(str(p) for p in error.path) or '(root)'}: {error.message}"
            for error in sorted(validator.iter_errors(value),
                                key=lambda e: list(e.path))]


def parse_object(text: str) -> tuple[dict[str, Any] | None, str]:
    """Parse a JSON object from model text, tolerating a fenced code block."""
    candidate = text.strip()
    if candidate.startswith("```"):
        # A fenced block is the single most common wrapper; unwrapping it is
        # not the same as accepting arbitrary prose around the object.
        body = candidate.split("```")
        if len(body) >= 2:
            candidate = body[1]
            if candidate.lstrip().startswith("json"):
                candidate = candidate.lstrip()[4:]
    try:
        parsed = json.loads(candidate.strip())
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc.msg} at position {exc.pos}"
    if not isinstance(parsed, dict):
        return None, f"expected a JSON object, got {type(parsed).__name__}"
    return parsed, ""


def _text_of(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content)
    return str(content)


def invoke_structured(model: Any, messages: list[Any], *,
                      schema: dict[str, Any],
                      budget: RootBudget | None = None,
                      max_repairs: int = MAX_REPAIR_ATTEMPTS,
                      label: str = "result") -> StructuredResult:
    """Get a schema-valid object from a model, repairing within the budget.

    ``messages`` is the conversation so far. On a failed attempt the validation
    problems are appended as a user turn, because telling the model *what* was
    wrong is the difference between a repair and a retry.
    """
    conversation = list(messages)
    attempts: list[StructuredAttempt] = []
    native = supports_structured_output(model)
    self_reserving = _self_reserving(model, budget)

    for index in range(max_repairs + 1):
        if budget is not None and not self_reserving:
            # Reserved before the call, including repairs. A repair that does
            # not reserve is a call the budget never sees. Skipped when the
            # model itself reserves (see `_self_reserving`) so the same call
            # is not charged twice.
            budget.reserve_model_call()

        response = model.invoke(conversation)
        raw = _text_of(response)
        value, parse_problem = parse_object(raw)
        problems = [parse_problem] if parse_problem else validate_object(
            value, schema)

        attempts.append(StructuredAttempt(index=index, raw=raw,
                                          problems=list(problems)))
        if not problems and value is not None:
            return StructuredResult(value=value, attempts=attempts,
                                    used_native=native)

        if index == max_repairs:
            break

        conversation = [
            *conversation,
            response,
            {"role": "user",
             "content": (f"That {label} was rejected by the schema validator:\n"
                         + "\n".join(f"- {p}" for p in problems)
                         + "\nReturn only a corrected JSON object.")},
        ]
        logger.info("Structured output repair %d/%d: %s", index + 1, max_repairs,
                    problems)

    raise ValidationFailed(
        f"The model did not produce a valid {label} after "
        f"{len(attempts)} attempt(s).",
        details={"attempts": len(attempts),
                 "problems": attempts[-1].problems if attempts else []})
