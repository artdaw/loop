"""The typed plan producer (runtime §2 step 4, M1).

`ModelPlanProducer` turns free text into a plan payload. The safety property is
that a hallucinated capability produces a *rejected plan*, not an unexpected
effect — so these tests are mostly about what gets refused and why.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from loop.agents.planner_agent import PLAN_SCHEMA, ModelPlanProducer
from loop.ai.budget import BudgetLimits, RootBudget
from loop.ai.model_gateway import ModelGateway
from loop.core.errors import ValidationFailed
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.core.settings import Settings
from loop.runtime.authority import AuthorityContext

WORK = PrivacyLabel(model_scope=ModelScope.CLOUD_ALLOWED, origins=frozenset({"cli"}))


def _settings(**kw) -> Settings:
    base = {"_env_file": None, "ollama_default_model": "llama3.1:8b"}
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _gateway(*texts: str) -> ModelGateway:
    model = GenericFakeChatModel(messages=iter([AIMessage(content=t) for t in texts]))
    return ModelGateway(settings=_settings(), local_model=model)


def _context(**kw) -> AuthorityContext:
    defaults: dict = {"owner": "owner", "root_id": "root-1", "privacy": WORK,
                      "budget": RootBudget(limits=BudgetLimits()),
                      "granted_scopes": frozenset({"vault.read"})}
    defaults.update(kw)
    return AuthorityContext(**defaults)


VALID_PLAN = '''{
    "schema_version": 1,
    "intents": ["check the weather"],
    "steps": [{"id": "s1", "role": "daily_life", "objective": "check weather",
               "capability": "weather.forecast", "arguments": {},
               "depends_on": []}],
    "response_intent": "reply with the forecast"
}'''

CATALOGUE = {"weather.forecast": "produce a forecast"}


# --------------------------------------------------------------------------- #
# Producing a plan
# --------------------------------------------------------------------------- #
def test_a_valid_plan_is_returned_as_is():
    producer = ModelPlanProducer(gateway=_gateway(VALID_PLAN))
    payload = producer.produce("check the weather", CATALOGUE, context=_context())

    assert payload["steps"][0]["capability"] == "weather.forecast"


def test_an_empty_catalogue_yields_an_empty_plan_without_calling_the_model():
    """Nothing is available, so there is nothing to plan — not an error."""
    producer = ModelPlanProducer(gateway=_gateway())          # no messages queued
    payload = producer.produce("do anything", {}, context=_context())

    assert payload["steps"] == []
    assert payload["schema_version"] == 1


def test_the_plan_factory_call_signature_matches_what_the_coordinator_uses():
    """`ModelPlanProducer` must work as the Coordinator's `PlanFactory` callback."""
    producer = ModelPlanProducer(gateway=_gateway(VALID_PLAN))
    payload = producer("check the weather", ["weather.forecast"])

    assert payload["steps"][0]["capability"] == "weather.forecast"


# --------------------------------------------------------------------------- #
# The catalogue is an allow-list
# --------------------------------------------------------------------------- #
INVENTED_CAPABILITY_PLAN = '''{
    "schema_version": 1,
    "intents": ["do something"],
    "steps": [{"id": "s1", "role": "daily_life", "objective": "x",
               "capability": "shell.run", "arguments": {}, "depends_on": []}],
    "response_intent": "reply"
}'''


def test_a_plan_naming_an_undisclosed_capability_is_rejected():
    """A hallucinated capability must be a rejected plan, not an effect."""
    producer = ModelPlanProducer(gateway=_gateway(INVENTED_CAPABILITY_PLAN))

    with pytest.raises(ValidationFailed, match="not offered"):
        producer.produce("do something", CATALOGUE, context=_context())


def test_the_rejection_names_what_was_invented():
    producer = ModelPlanProducer(gateway=_gateway(INVENTED_CAPABILITY_PLAN))

    with pytest.raises(ValidationFailed) as excinfo:
        producer.produce("do something", CATALOGUE, context=_context())

    assert excinfo.value.details["unknown"] == ["shell.run"]
    assert excinfo.value.details["offered"] == ["weather.forecast"]


def test_the_system_prompt_only_lists_the_offered_capabilities():
    """The model is never shown a capability it is not allowed to use."""
    producer = ModelPlanProducer(gateway=_gateway(VALID_PLAN))
    prompt = producer._system_prompt(CATALOGUE)

    assert "weather.forecast" in prompt
    assert "shell.run" not in prompt


# --------------------------------------------------------------------------- #
# Shape rejection via the schema (unknown fields, too many steps)
# --------------------------------------------------------------------------- #
EXTRA_FIELD_PLAN = '''{
    "schema_version": 1,
    "intents": ["x"],
    "steps": [{"id": "s1", "role": "daily_life", "objective": "x",
               "capability": "weather.forecast", "shell": "rm -rf /"}],
    "response_intent": "reply"
}'''


def test_a_step_with_an_undeclared_field_is_rejected():
    """Two identical bad replies: schema rejection consumes the repair too."""
    producer = ModelPlanProducer(
        gateway=_gateway(EXTRA_FIELD_PLAN, EXTRA_FIELD_PLAN))

    with pytest.raises(ValidationFailed):
        producer.produce("do something", CATALOGUE, context=_context())


TOO_MANY_STEPS_PLAN = '''{
    "schema_version": 1,
    "intents": ["x"],
    "steps": [
        {"id": "s1", "role": "daily_life", "objective": "a", "capability": "weather.forecast"},
        {"id": "s2", "role": "daily_life", "objective": "b", "capability": "weather.forecast"},
        {"id": "s3", "role": "daily_life", "objective": "c", "capability": "weather.forecast"},
        {"id": "s4", "role": "daily_life", "objective": "d", "capability": "weather.forecast"},
        {"id": "s5", "role": "daily_life", "objective": "e", "capability": "weather.forecast"}
    ],
    "response_intent": "reply"
}'''


def test_more_than_four_steps_is_rejected():
    producer = ModelPlanProducer(
        gateway=_gateway(TOO_MANY_STEPS_PLAN, TOO_MANY_STEPS_PLAN))

    with pytest.raises(ValidationFailed):
        producer.produce("do something", CATALOGUE, context=_context())


# --------------------------------------------------------------------------- #
# Repair
# --------------------------------------------------------------------------- #
def test_an_invalid_first_reply_is_repaired_within_the_budget():
    producer = ModelPlanProducer(gateway=_gateway("not json at all", VALID_PLAN))
    budget = RootBudget(limits=BudgetLimits(model_calls=5))

    payload = producer.produce("check the weather", CATALOGUE,
                               context=_context(budget=budget))

    assert payload["steps"][0]["capability"] == "weather.forecast"
    assert budget.usage.model_calls == 2


def test_repair_is_bounded_by_the_producers_own_limit():
    producer = ModelPlanProducer(gateway=_gateway("bad", "still bad", "still bad"),
                                 max_repairs=1)

    with pytest.raises(ValidationFailed, match="did not produce"):
        producer.produce("check the weather", CATALOGUE, context=_context())


# --------------------------------------------------------------------------- #
# No context supplied (a bare PlanFactory call)
# --------------------------------------------------------------------------- #
def test_producing_without_a_context_still_works():
    producer = ModelPlanProducer(gateway=_gateway(VALID_PLAN))
    payload = producer.produce("check the weather", CATALOGUE)

    assert payload["steps"][0]["capability"] == "weather.forecast"


def test_the_plan_schema_forbids_extra_top_level_fields():
    assert PLAN_SCHEMA["additionalProperties"] is False
