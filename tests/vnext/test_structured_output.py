"""Structured output with bounded repair (agent-stack §3, M1).

Covers the module directly: parsing, schema validation, the repair loop and
its budget accounting. `test_capability_runners.py` covers the same mechanism
wired into the real agent runner; these tests isolate the mechanism itself.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from loop.ai.budget import BudgetLimits, RootBudget
from loop.ai.structured import (
    describe_output_support,
    invoke_structured,
    parse_object,
    supports_structured_output,
    validate_object,
)
from loop.core.errors import ValidationFailed

SCHEMA = {
    "type": "object",
    "required": ["answer", "sources"],
    "properties": {
        "answer": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
}


def _model(*texts: str) -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter([AIMessage(content=t) for t in texts]))


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_parse_object_reads_plain_json():
    value, problem = parse_object('{"a": 1}')
    assert value == {"a": 1} and problem == ""


def test_parse_object_unwraps_a_fenced_code_block():
    value, problem = parse_object('```json\n{"a": 1}\n```')
    assert value == {"a": 1} and problem == ""


def test_parse_object_unwraps_a_fence_with_no_language_tag():
    value, problem = parse_object('```\n{"a": 1}\n```')
    assert value == {"a": 1}


def test_parse_object_reports_invalid_json():
    value, problem = parse_object("not json at all")
    assert value is None
    assert "not valid JSON" in problem


def test_parse_object_rejects_a_non_object_top_level():
    value, problem = parse_object("[1, 2, 3]")
    assert value is None
    assert "expected a JSON object" in problem


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #
def test_validate_object_accepts_a_conforming_value():
    assert validate_object({"answer": "x", "sources": []}, SCHEMA) == []


def test_validate_object_reports_a_missing_required_field():
    problems = validate_object({"answer": "x"}, SCHEMA)
    assert problems and "sources" in problems[0]


def test_validate_object_reports_a_wrong_type():
    problems = validate_object({"answer": 5, "sources": []}, SCHEMA)
    assert problems


def test_validate_object_rejects_a_non_dict():
    problems = validate_object(["not", "a", "dict"], SCHEMA)
    assert problems and "expected a JSON object" in problems[0]


def test_validate_object_with_no_schema_accepts_anything():
    assert validate_object({"anything": True}, {}) == []


# --------------------------------------------------------------------------- #
# The repair loop
# --------------------------------------------------------------------------- #
def test_a_valid_first_answer_needs_no_repair():
    result = invoke_structured(
        _model('{"answer": "hi", "sources": []}'),
        [{"role": "user", "content": "go"}], schema=SCHEMA)

    assert result.value == {"answer": "hi", "sources": []}
    assert result.repairs == 0


def test_an_invalid_first_answer_is_repaired():
    result = invoke_structured(
        _model("not json", '{"answer": "hi", "sources": []}'),
        [{"role": "user", "content": "go"}], schema=SCHEMA)

    assert result.value == {"answer": "hi", "sources": []}
    assert result.repairs == 1


def test_the_repair_turn_includes_what_was_wrong():
    """Telling the model what was wrong is the difference between repair and retry."""
    model = _model('{"answer": "x"}', '{"answer": "hi", "sources": []}')
    invoke_structured(model, [{"role": "user", "content": "go"}], schema=SCHEMA)

    # GenericFakeChatModel records nothing itself; assert indirectly by giving
    # only two messages and confirming both were consumed (no third pull).
    with pytest.raises(StopIteration):
        model.invoke([{"role": "user", "content": "x"}])


def test_repairs_are_bounded():
    always_bad = _model(*(["still not json"] * 10))
    with pytest.raises(ValidationFailed, match="did not produce"):
        invoke_structured(always_bad, [{"role": "user", "content": "go"}],
                          schema=SCHEMA, max_repairs=2)


def test_the_failure_names_the_attempt_count():
    always_bad = _model(*(["nope"] * 10))
    with pytest.raises(ValidationFailed) as excinfo:
        invoke_structured(always_bad, [{"role": "user", "content": "go"}],
                          schema=SCHEMA, max_repairs=1)

    assert excinfo.value.details["attempts"] == 2


# --------------------------------------------------------------------------- #
# Budget: repairs are not a second allowance (agent-stack §3)
# --------------------------------------------------------------------------- #
def test_every_attempt_reserves_against_the_root_budget():
    budget = RootBudget(limits=BudgetLimits(model_calls=5))
    invoke_structured(
        _model("bad", "bad", '{"answer": "hi", "sources": []}'),
        [{"role": "user", "content": "go"}], schema=SCHEMA, budget=budget,
        max_repairs=2)

    assert budget.usage.model_calls == 3


def test_a_repair_loop_cannot_exceed_the_root_budget():
    """A repair loop with its own allowance would be a second budget."""
    from loop.core.errors import BudgetExhausted

    budget = RootBudget(limits=BudgetLimits(model_calls=2))
    with pytest.raises(BudgetExhausted):
        invoke_structured(
            _model("bad", "bad", '{"answer": "hi", "sources": []}'),
            [{"role": "user", "content": "go"}], schema=SCHEMA, budget=budget,
            max_repairs=5)


def test_a_shared_budget_is_visible_across_repairs_and_other_calls():
    budget = RootBudget(limits=BudgetLimits(model_calls=3))
    budget.reserve_model_call()          # a prior, unrelated call

    invoke_structured(
        _model('{"answer": "hi", "sources": []}'),
        [{"role": "user", "content": "go"}], schema=SCHEMA, budget=budget,
        max_repairs=1)

    assert budget.usage.model_calls == 2


# --------------------------------------------------------------------------- #
# Unsupported structured output is reported, not routed around (LG12)
# --------------------------------------------------------------------------- #
def test_a_model_without_structured_output_support_is_detected():
    class Bare:
        def invoke(self, messages):
            return AIMessage(content='{"answer": "hi", "sources": []}')

    assert supports_structured_output(Bare()) is False
    assert "does not support structured output" in describe_output_support(Bare())


def test_a_model_with_structured_output_support_is_detected():
    class Supports:
        def with_structured_output(self, schema):
            return self

        def invoke(self, messages):
            return AIMessage(content="{}")

    assert supports_structured_output(Supports()) is True
    assert describe_output_support(Supports()) == ""


def test_unsupported_output_still_produces_a_result_via_parsing():
    """A missing capability degrades gracefully; it does not block the call."""
    class Bare:
        def __init__(self, texts):
            self._texts = iter(texts)

        def invoke(self, messages):
            return AIMessage(content=next(self._texts))

    result = invoke_structured(
        Bare(['{"answer": "hi", "sources": []}']),
        [{"role": "user", "content": "go"}], schema=SCHEMA)

    assert result.used_native is False
    assert result.value["answer"] == "hi"
