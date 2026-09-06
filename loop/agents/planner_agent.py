"""Turning free text into a typed plan (runtime §2 step 4).

The Coordinator does not improvise. It asks a model for a *plan* — a typed
object naming roles, capabilities and dependencies — and the plan is then
validated before anything runs. That ordering is the whole safety property: a
model that hallucinates a capability produces a rejected plan, not an
unexpected effect.

Three constraints shape this producer:

* **The catalogue is an allow-list, and it is already role-filtered.** A model
  is shown only operations the assigned role may reach, so a step naming
  anything else fails validation. Disclosure is the boundary, not a later check.
* **Repair is bounded and shares the root budget.** An invalid plan gets the
  validator's own complaint back and one more attempt; both cost the caller.
* **No plan is not an error.** A request needing no capability yields an empty
  plan, and the Coordinator answers directly. Forcing a step would invent work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from loop.agents.roles import ROLE_IDS, RoleRegistry, get_role
from loop.ai.model_gateway import ModelGateway, gateway_chat_model
from loop.ai.structured import invoke_structured
from loop.core.errors import ValidationFailed
from loop.core.privacy import PrivacyLabel
from loop.runtime.authority import AuthorityContext

logger = logging.getLogger(__name__)

#: The shape a plan must have before it is parsed. Deliberately strict: an
#: unknown key is a sign the model invented a field, and inventing fields is how
#: a step acquires a `shell` argument.
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "intents", "steps", "response_intent"],
    "properties": {
        "schema_version": {"const": 1},
        "intents": {"type": "array", "items": {"type": "string"}},
        "response_intent": {"type": "string"},
        "steps": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "role", "objective", "capability"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "role": {"type": "string"},
                    "objective": {"type": "string"},
                    "capability": {"type": "string"},
                    "arguments": {"type": "object"},
                    "depends_on": {"type": "array",
                                   "items": {"type": "string"}},
                },
            },
        },
    },
}

SYSTEM_PROMPT = """\
You produce typed execution plans for a personal assistant. Reply with one JSON \
object and nothing else.

Rules:
- Use only the capabilities listed below. A capability not listed does not exist.
- Assign each step the role that owns that kind of work.
- Use depends_on only when a step genuinely needs another step's result.
- At most four steps. Prefer fewer.
- If the request needs no capability at all, return an empty steps array.
- Never invent arguments the capability did not declare.
"""


@dataclass
class PlanRequest:
    objective: str
    catalogue: dict[str, str]
    roles: dict[str, str]


class ModelPlanProducer:
    """Asks the configured model for a typed plan (runtime §2).

    Callable as a `PlanFactory`, so the Coordinator's existing injection point
    is the seam — the coordinator has no idea a model is involved.
    """

    def __init__(self, *, gateway: ModelGateway,
                 role_registry: RoleRegistry | None = None,
                 max_repairs: int = 1) -> None:
        self.gateway = gateway
        self.roles = role_registry or RoleRegistry()
        self.max_repairs = max_repairs

    def __call__(self, objective: str, discovered: list[str]) -> dict[str, Any]:
        """The `PlanFactory` signature the Coordinator already accepts."""
        return self.produce(objective, dict.fromkeys(discovered, ""))

    def produce(self, objective: str, catalogue: dict[str, str], *,
                context: AuthorityContext | None = None) -> dict[str, Any]:
        """Produce a validated plan payload for the coordinator to parse."""
        if not catalogue:
            # Nothing is available, so there is nothing to plan. An empty plan
            # is an honest answer; a fabricated step is not.
            return _empty_plan(objective)

        labels = [context.privacy] if context is not None else \
            [PrivacyLabel.for_unlabelled_import()]
        model = gateway_chat_model(
            self.gateway, labels=labels,
            budget=context.budget if context is not None else None,
            purpose="planning")

        messages = [
            {"role": "system", "content": self._system_prompt(catalogue)},
            {"role": "user", "content": objective},
        ]
        result = invoke_structured(
            model, messages, schema=PLAN_SCHEMA,
            budget=context.budget if context is not None else None,
            max_repairs=self.max_repairs, label="plan")

        payload = result.value
        self._check_catalogue(payload, catalogue)
        if result.repairs:
            logger.info("Plan required %d repair(s)", result.repairs)
        return payload

    def _system_prompt(self, catalogue: dict[str, str]) -> str:
        lines = [f"- {name}: {description or 'no description'}"
                 for name, description in sorted(catalogue.items())]
        roles = [f"- {role_id}: {get_role(role_id).summary}"
                 for role_id in sorted(ROLE_IDS)]
        return (f"{SYSTEM_PROMPT}\nAvailable capabilities:\n" + "\n".join(lines)
                + "\n\nRoles:\n" + "\n".join(roles))

    def _check_catalogue(self, payload: dict[str, Any],
                         catalogue: dict[str, str]) -> None:
        """Reject a plan naming something outside the disclosed allow-list.

        `parse_plan` checks this too. Checking here as well means the failure
        names the *planner* as the source, which is the difference between a
        useful log line and a mystery.
        """
        unknown = [step.get("capability") for step in payload.get("steps", [])
                   if step.get("capability") not in catalogue]
        if unknown:
            raise ValidationFailed(
                "The plan named capabilities that were not offered.",
                details={"unknown": unknown, "offered": sorted(catalogue)})


def _empty_plan(objective: str) -> dict[str, Any]:
    return {"schema_version": 1, "intents": [objective], "steps": [],
            "response_intent": "answer the owner directly"}
