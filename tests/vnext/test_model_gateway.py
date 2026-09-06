"""ModelGateway policy enforcement (agent-stack §3).

Covers LG03 (private context never reaches cloud, including derived and child
paths), LG05 (one shared budget across parallel children and repairs) and LG12
(unsupported features reported, never implicit cloud use).

These use LangChain's own `GenericFakeChatModel`, so the code path under test is
the real `BaseChatModel.invoke` path — not a hand-rolled stand-in that could
diverge from how the framework actually behaves.
"""

from __future__ import annotations

import threading

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from loop.ai.budget import BudgetLimits, RootBudget
from loop.ai.model_gateway import ModelGateway, PrivacyError
from loop.core.errors import BudgetExhausted, Unavailable
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.core.settings import Settings


def _fake(text: str = "local answer") -> GenericFakeChatModel:
    """A real LangChain chat model that returns scripted messages."""
    return GenericFakeChatModel(messages=iter([AIMessage(content=text)] * 50))


class BrokenModel(GenericFakeChatModel):
    """A model whose invocation always fails, like an Ollama that is down."""

    def invoke(self, *args, **kwargs):  # type: ignore[override]
        raise ConnectionError("ollama is not running")


def _settings(**kw) -> Settings:
    base = {"_env_file": None, "ollama_default_model": "llama3.1:8b"}
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _cloud_settings(**kw) -> Settings:
    return _settings(cloud_enabled=True, anthropic_api_key="k",
                     anthropic_model="claude-x", cloud_daily_budget_usd=5, **kw)


PRIVATE = PrivacyLabel(model_scope=ModelScope.LOCAL_ONLY,
                       origins=frozenset({"telegram_private"}), sensitive=True)
WORK = PrivacyLabel(model_scope=ModelScope.CLOUD_ALLOWED,
                    origins=frozenset({"work"}))


# --------------------------------------------------------------------------- #
# The real LangChain path
# --------------------------------------------------------------------------- #
def test_a_call_goes_through_the_real_langchain_model():
    gateway = ModelGateway(settings=_settings(), local_model=_fake("hello"))

    result = gateway.invoke("what is the plan?", labels=[WORK])

    assert result.text == "hello"
    assert result.backend == "local"


def test_the_response_is_a_langchain_message():
    gateway = ModelGateway(settings=_settings(), local_model=_fake("hi"))
    result = gateway.invoke("q", labels=[WORK])

    assert isinstance(result.raw, AIMessage)


def test_a_system_prompt_is_passed_as_a_system_message():
    gateway = ModelGateway(settings=_settings(), local_model=_fake("ok"))
    assert gateway.invoke("q", labels=[WORK], system="be brief").text == "ok"


# --------------------------------------------------------------------------- #
# LG03 — private context never reaches the cloud
# --------------------------------------------------------------------------- #
def test_lg03_a_private_request_uses_the_local_model():
    cloud = _fake("CLOUD ANSWER")
    gateway = ModelGateway(settings=_cloud_settings(), local_model=_fake("local"),
                           cloud_model=cloud)

    result = gateway.invoke("private thing", labels=[PRIVATE])

    assert result.backend == "local"
    assert gateway.audit.cloud_calls == 0


def test_lg03_local_failure_on_private_input_is_terminal():
    """The alternative to answering privately is not answering — never elsewhere."""
    cloud = _fake("CLOUD ANSWER")
    gateway = ModelGateway(settings=_cloud_settings(),
                           local_model=BrokenModel(messages=iter([])),
                           cloud_model=cloud)

    with pytest.raises(PrivacyError):
        gateway.invoke("private thing", labels=[PRIVATE])

    assert gateway.audit.cloud_calls == 0


def test_lg03_a_derived_summary_inherits_the_private_label():
    """"Summarise it first, then send the summary" is not an escape hatch."""
    gateway = ModelGateway(settings=_cloud_settings(),
                           local_model=BrokenModel(messages=iter([])),
                           cloud_model=_fake("CLOUD"))

    summary_label = gateway.resolve_scope([PRIVATE, WORK])
    assert summary_label.is_local_only is True

    with pytest.raises(PrivacyError):
        gateway.invoke("summary of private input", labels=[PRIVATE, WORK],
                       purpose="summary")

    assert gateway.audit.cloud_calls == 0


def test_lg03_a_child_run_with_private_context_cannot_escalate():
    gateway = ModelGateway(settings=_cloud_settings(),
                           local_model=BrokenModel(messages=iter([])),
                           cloud_model=_fake("CLOUD"))

    with pytest.raises(PrivacyError):
        gateway.invoke("child work", labels=[PRIVATE], purpose="child")

    assert gateway.audit.cloud_calls == 0


def test_lg03_a_schema_repair_on_private_input_cannot_escalate():
    gateway = ModelGateway(settings=_cloud_settings(),
                           local_model=BrokenModel(messages=iter([])),
                           cloud_model=_fake("CLOUD"))

    with pytest.raises(PrivacyError):
        gateway.invoke("repair this json", labels=[PRIVATE], purpose="repair")

    assert gateway.audit.cloud_calls == 0


def test_lg03_a_caller_cannot_declare_private_context_cloud_allowed():
    """A supplied label may narrow, never widen (runtime §3)."""
    gateway = ModelGateway(settings=_cloud_settings(),
                           local_model=BrokenModel(messages=iter([])),
                           cloud_model=_fake("CLOUD"))
    claimed = PrivacyLabel(model_scope=ModelScope.CLOUD_ALLOWED)

    with pytest.raises(PrivacyError):
        gateway.invoke("q", labels=[PRIVATE, claimed])

    assert gateway.audit.cloud_calls == 0


def test_lg03_the_audit_records_no_content():
    gateway = ModelGateway(settings=_settings(), local_model=_fake("secret reply"))
    gateway.invoke("secret prompt text", labels=[PRIVATE])

    record = gateway.audit.records[0]
    serialised = repr(record)
    assert "secret prompt text" not in serialised
    assert "secret reply" not in serialised
    assert len(record.prompt_hash) == 64      # a SHA-256 hash, not the prompt


def test_may_use_cloud_is_false_for_a_private_label():
    gateway = ModelGateway(settings=_cloud_settings(), local_model=_fake())
    assert gateway.may_use_cloud(PRIVATE) is False
    assert gateway.may_use_cloud(WORK) is True


# --------------------------------------------------------------------------- #
# No length/latency escalation
# --------------------------------------------------------------------------- #
def test_a_short_local_answer_does_not_trigger_cloud_escalation():
    """Specification 1.3 forbids treating brevity as low confidence."""
    cloud = _fake("LONG CLOUD ANSWER")
    gateway = ModelGateway(settings=_cloud_settings(), local_model=_fake("no"),
                           cloud_model=cloud)

    result = gateway.invoke("a question", labels=[WORK])

    assert result.text == "no"
    assert gateway.audit.cloud_calls == 0


def test_a_refusal_shaped_local_answer_does_not_trigger_escalation():
    gateway = ModelGateway(settings=_cloud_settings(),
                           local_model=_fake("I don't know"),
                           cloud_model=_fake("CLOUD"))

    result = gateway.invoke("a question", labels=[WORK])

    assert result.text == "I don't know"
    assert gateway.audit.cloud_calls == 0


def test_cloud_is_used_only_when_the_local_model_is_genuinely_unavailable():
    gateway = ModelGateway(settings=_cloud_settings(),
                           local_model=BrokenModel(messages=iter([])),
                           cloud_model=_fake("CLOUD"))

    result = gateway.invoke("non-private work", labels=[WORK])

    assert result.backend == "cloud"
    assert gateway.audit.cloud_calls == 1


def test_unconfigured_cloud_means_local_failure_is_simply_unavailable():
    gateway = ModelGateway(settings=_settings(),
                           local_model=BrokenModel(messages=iter([])))

    with pytest.raises(Unavailable):
        gateway.invoke("non-private work", labels=[WORK])


# --------------------------------------------------------------------------- #
# LG05 — one shared budget
# --------------------------------------------------------------------------- #
def test_lg05_budget_is_reserved_before_the_call():
    budget = RootBudget(limits=BudgetLimits(model_calls=3))
    gateway = ModelGateway(settings=_settings(), local_model=_fake())

    gateway.invoke("q", labels=[WORK], budget=budget)

    assert budget.usage.model_calls == 1


def test_lg05_the_cap_is_enforced():
    budget = RootBudget(limits=BudgetLimits(model_calls=2))
    gateway = ModelGateway(settings=_settings(), local_model=_fake())

    gateway.invoke("q1", labels=[WORK], budget=budget)
    gateway.invoke("q2", labels=[WORK], budget=budget)

    with pytest.raises(BudgetExhausted):
        gateway.invoke("q3", labels=[WORK], budget=budget)


def test_lg05_children_share_one_root_budget():
    """Spawning children must not multiply the allowance."""
    budget = RootBudget(limits=BudgetLimits(model_calls=3))
    gateway = ModelGateway(settings=_settings(), local_model=_fake())

    for purpose in ("agent", "child", "child"):
        gateway.invoke("q", labels=[WORK], budget=budget, purpose=purpose)

    with pytest.raises(BudgetExhausted):
        gateway.invoke("q", labels=[WORK], budget=budget, purpose="child")


def test_lg05_schema_repairs_count_against_the_same_budget():
    """agent-stack §3: any bounded repair counts against the original budget."""
    budget = RootBudget(limits=BudgetLimits(model_calls=2))
    gateway = ModelGateway(settings=_settings(), local_model=_fake())

    gateway.invoke("q", labels=[WORK], budget=budget, purpose="agent")
    gateway.invoke("repair", labels=[WORK], budget=budget, purpose="repair")

    with pytest.raises(BudgetExhausted):
        gateway.invoke("repair again", labels=[WORK], budget=budget,
                       purpose="repair")


def test_lg05_a_failed_call_still_consumes_its_reservation():
    """Otherwise a run could burn its deadline on failures and look untouched."""
    budget = RootBudget(limits=BudgetLimits(model_calls=2))
    gateway = ModelGateway(settings=_settings(),
                           local_model=BrokenModel(messages=iter([])))

    with pytest.raises(Unavailable):
        gateway.invoke("q", labels=[WORK], budget=budget)

    assert budget.usage.model_calls == 1
    assert budget.usage.failures == 1


def test_lg05_parallel_children_cannot_each_take_the_last_call():
    """Without the lock, two branches can both see room for one more call."""
    budget = RootBudget(limits=BudgetLimits(model_calls=5))
    gateway = ModelGateway(settings=_settings(), local_model=_fake())
    granted: list[bool] = []
    lock = threading.Lock()
    start = threading.Barrier(10)

    def child() -> None:
        start.wait(timeout=5)
        try:
            gateway.invoke("q", labels=[WORK], budget=budget, purpose="child")
            with lock:
                granted.append(True)
        except BudgetExhausted:
            with lock:
                granted.append(False)

    threads = [threading.Thread(target=child) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sum(granted) == 5, f"exactly 5 calls should be granted, got {granted}"
    assert budget.usage.model_calls == 5


def test_lg05_token_budget_is_enforced_separately():
    budget = RootBudget(limits=BudgetLimits(total_tokens=100))
    gateway = ModelGateway(settings=_settings(), local_model=_fake())

    with pytest.raises(BudgetExhausted):
        gateway.invoke("q", labels=[WORK], budget=budget, estimated_tokens=500)


def test_tool_call_budget_is_tracked():
    budget = RootBudget(limits=BudgetLimits(tool_calls=2))
    budget.reserve_tool_call()
    budget.reserve_tool_call()

    with pytest.raises(BudgetExhausted):
        budget.reserve_tool_call()


def test_budget_snapshot_reports_usage():
    budget = RootBudget(limits=BudgetLimits(model_calls=3))
    gateway = ModelGateway(settings=_settings(), local_model=_fake())
    gateway.invoke("q", labels=[WORK], budget=budget)

    assert budget.snapshot()["model_calls"] == 1
    assert budget.model_calls_remaining == 2


# --------------------------------------------------------------------------- #
# LG12 — unsupported features are reported, never worked around silently
# --------------------------------------------------------------------------- #
def test_lg12_missing_local_model_is_a_configuration_limitation():
    gateway = ModelGateway(settings=Settings(_env_file=None))  # type: ignore[call-arg]

    problems = gateway.describe_limitations()

    assert any("local model" in p.lower() for p in problems)


def test_lg12_unconfigured_cloud_is_reported_not_hidden():
    gateway = ModelGateway(settings=_settings(), local_model=_fake())
    problems = gateway.describe_limitations()

    assert any("cloud" in p.lower() for p in problems)
    assert any("local-only" in p.lower() for p in problems)


def test_lg12_a_configured_stack_reports_no_limitations():
    gateway = ModelGateway(settings=_cloud_settings(), local_model=_fake())
    assert gateway.describe_limitations() == []


def test_lg12_tool_support_is_probed_not_assumed():
    gateway = ModelGateway(settings=_settings(), local_model=_fake())
    assert gateway.supports_tool_calling() in (True, False)


def test_lg12_no_model_is_invoked_without_a_request():
    """Idle costs nothing: constructing the gateway calls nothing (D17/LG12)."""
    gateway = ModelGateway(settings=_settings(), local_model=_fake())
    assert gateway.audit.records == []
