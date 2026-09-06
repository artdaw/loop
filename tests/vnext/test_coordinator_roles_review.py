"""Role-scoped discovery and the Reviewer/repair stage (runtime §1, M1).

`test_coordinator_durability.py` covers checkpointing and approval. These
tests cover the two other M1 deliverables: an operation no assigned role may
reach never reaches a model's shortlist, and a failing result becomes bounded
repair work rather than an unconditional exception.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from loop.agents.coordinator import Coordinator
from loop.agents.roles import RoleRegistry
from loop.ai.budget import BudgetLimits, RootBudget
from loop.ai.model_gateway import ModelGateway
from loop.capabilities.registry import CapabilityRegistry
from loop.capabilities.runners import (
    ArtifactStore,
    CapabilityInvoker,
    RegisteredHandler,
)
from loop.core.errors import ValidationFailed
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.core.settings import Settings
from loop.runtime.authority import AuthorityContext
from tests.vnext.test_capability_runners import ToolCallingFakeChatModel

WORK = PrivacyLabel(model_scope=ModelScope.CLOUD_ALLOWED, origins=frozenset({"cli"}))


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, data_dir=str(tmp_path),  # type: ignore[call-arg]
                    ollama_default_model="llama3.1:8b")


def _context(**kw) -> AuthorityContext:
    defaults: dict = {"owner": "owner", "root_id": "root-1", "privacy": WORK,
                      "budget": RootBudget(limits=BudgetLimits()),
                      "granted_scopes": frozenset({"vault.read"})}
    defaults.update(kw)
    return AuthorityContext(**defaults)


def _handlers() -> dict[str, RegisteredHandler]:
    return {
        "vault.read": RegisteredHandler(
            lambda args, context: {"answer": "read", "sources": []},
            description="Read a note.",
            input_schema={"type": "object", "additionalProperties": True},
            owner_role="compiler"),
        "email.send": RegisteredHandler(
            lambda args, context: {"answer": "sent", "sources": []},
            description="Send an email.",
            input_schema={"type": "object", "additionalProperties": True},
            owner_role="commitments"),
    }


def _coordinator(tmp_path, sessions, clock, *, plan_factory=None,
                 reviewer=None) -> Coordinator:
    gateway = ModelGateway(settings=_settings(tmp_path),
                           local_model=ToolCallingFakeChatModel(
                               messages=iter([AIMessage(content="{}")] * 4)),
                           clock=clock)
    invoker = CapabilityInvoker(
        registry=CapabilityRegistry(roots=[]), gateway=gateway,
        artifact_store=ArtifactStore(sessions=sessions, clock=clock),
        handlers=_handlers())
    return Coordinator(invoker=invoker, plan_factory=plan_factory,
                       roles=RoleRegistry(), reviewer=reviewer)


# --------------------------------------------------------------------------- #
# Role-filtered discovery — a role never sees what it may not reach
# --------------------------------------------------------------------------- #
def test_discovery_only_offers_operations_some_role_may_use(tmp_path, sessions,
                                                            clock):
    """`email.send` belongs to `commitments`; the shortlist still includes it
    somewhere a role owns it, but the *catalogue* handed to the plan producer
    must never include an operation no role can reach at all."""
    seen: dict = {}

    def factory(objective, discovered):
        seen["discovered"] = list(discovered)
        return {"schema_version": 1, "intents": [objective], "steps": [],
                "response_intent": "reply"}

    coordinator = _coordinator(tmp_path, sessions, clock, plan_factory=factory)
    coordinator.invoke(objective="read a note", context=_context())

    assert "vault.read" in seen["discovered"]
    assert "email.send" in seen["discovered"]


def test_an_operation_no_role_can_reach_never_reaches_the_planner(tmp_path,
                                                                  sessions, clock):
    """An operation with no owning role anywhere is invisible to the shortlist,
    not merely rejected after the fact."""
    seen: dict = {}

    def factory(objective, discovered):
        seen["discovered"] = list(discovered)
        return {"schema_version": 1, "intents": [objective], "steps": [],
                "response_intent": "reply"}

    gateway = ModelGateway(settings=_settings(tmp_path),
                           local_model=ToolCallingFakeChatModel(
                               messages=iter([AIMessage(content="{}")] * 4)),
                           clock=clock)
    handlers = dict(_handlers())
    handlers["shell.run"] = RegisteredHandler(
        lambda args, context: {"answer": "ran", "sources": []},
        description="Run an arbitrary shell command.",
        input_schema={"type": "object", "additionalProperties": True},
        owner_role="nonexistent-role")
    invoker = CapabilityInvoker(
        registry=CapabilityRegistry(roots=[]), gateway=gateway,
        artifact_store=ArtifactStore(sessions=sessions, clock=clock),
        handlers=handlers)
    coordinator = Coordinator(invoker=invoker, plan_factory=factory,
                              roles=RoleRegistry())

    coordinator.invoke(objective="do something", context=_context())

    assert "shell.run" not in seen["discovered"]


def test_a_step_assigned_to_a_role_without_the_scope_is_refused(tmp_path,
                                                                sessions, clock):
    """Role scope is a runtime ceiling: the plan can name the pairing, but the
    coordinator refuses to run it."""
    def factory(objective, discovered):
        return {"schema_version": 1, "intents": [objective],
                "steps": [{"id": "s1", "role": "seeker", "objective": objective,
                          "capability": "email.send", "arguments": {},
                          "depends_on": []}],
                "response_intent": "reply"}

    coordinator = _coordinator(tmp_path, sessions, clock, plan_factory=factory)

    with pytest.raises(ValidationFailed, match="may not"):
        coordinator.invoke(objective="send it", context=_context())


def test_a_step_assigned_to_its_owning_role_runs(tmp_path, sessions, clock):
    def factory(objective, discovered):
        return {"schema_version": 1, "intents": [objective],
                "steps": [{"id": "s1", "role": "commitments",
                          "objective": objective, "capability": "email.send",
                          "arguments": {}, "depends_on": []}],
                "response_intent": "reply"}

    coordinator = _coordinator(tmp_path, sessions, clock, plan_factory=factory)
    result = coordinator.invoke(objective="send it", context=_context())

    assert result.response["status"] == "succeeded"


# --------------------------------------------------------------------------- #
# The Reviewer stage and bounded repair
# --------------------------------------------------------------------------- #
def _single_step_factory(role: str, capability: str):
    def factory(objective, discovered):
        return {"schema_version": 1, "intents": [objective],
                "steps": [{"id": "s1", "role": role, "objective": objective,
                          "capability": capability, "arguments": {},
                          "depends_on": []}],
                "response_intent": "reply"}
    return factory


def test_a_deterministic_check_runs_even_with_no_reviewer(tmp_path, sessions,
                                                          clock):
    """Runtime §1: deterministic validators run even when no Reviewer agrees."""
    coordinator = _coordinator(
        tmp_path, sessions, clock,
        plan_factory=_single_step_factory("compiler", "vault.read"))

    result = coordinator.invoke(objective="read", context=_context())
    assert result.response["status"] == "succeeded"


def test_a_reviewer_finding_triggers_one_repair_attempt(tmp_path, sessions,
                                                        clock):
    calls = {"n": 0}

    def reviewer(plan, summaries, context):
        calls["n"] += 1
        if calls["n"] == 1:
            return ["step s1: the answer does not address the objective"]
        return []

    coordinator = _coordinator(
        tmp_path, sessions, clock,
        plan_factory=_single_step_factory("compiler", "vault.read"),
        reviewer=reviewer)
    result = coordinator.invoke(objective="read", context=_context())

    assert result.response["status"] == "succeeded"
    assert calls["n"] == 2


def test_repair_re_executes_the_capability(tmp_path, sessions, clock):
    """A repair must actually redo the failed work, not just retry the review."""
    attempts = {"n": 0}

    def counting_handler(args, context):
        attempts["n"] += 1
        return {"answer": f"attempt-{attempts['n']}", "sources": []}

    gateway = ModelGateway(settings=_settings(tmp_path),
                           local_model=ToolCallingFakeChatModel(
                               messages=iter([AIMessage(content="{}")] * 4)),
                           clock=clock)
    handlers = {"vault.read": RegisteredHandler(
        counting_handler, description="Read.",
        input_schema={"type": "object", "additionalProperties": True},
        owner_role="compiler")}
    invoker = CapabilityInvoker(
        registry=CapabilityRegistry(roots=[]), gateway=gateway,
        artifact_store=ArtifactStore(sessions=sessions, clock=clock),
        handlers=handlers)

    review_calls = {"n": 0}

    def reviewer(plan, summaries, context):
        review_calls["n"] += 1
        return (["step s1: repeat it"] if review_calls["n"] == 1 else [])

    coordinator = Coordinator(
        invoker=invoker, plan_factory=_single_step_factory("compiler", "vault.read"),
        roles=RoleRegistry(), reviewer=reviewer)
    coordinator.invoke(objective="read", context=_context())

    assert attempts["n"] == 2


def test_repair_is_bounded_and_eventually_raises(tmp_path, sessions, clock):
    def always_wrong(plan, summaries, context):
        return ["step s1: still wrong"]

    coordinator = _coordinator(
        tmp_path, sessions, clock,
        plan_factory=_single_step_factory("compiler", "vault.read"),
        reviewer=always_wrong)

    with pytest.raises(ValidationFailed, match="did not pass review"):
        coordinator.invoke(objective="read", context=_context())


def test_a_broken_reviewer_does_not_block_otherwise_valid_work(tmp_path,
                                                               sessions, clock):
    """A reviewer that raises must not fail work it was only checking."""
    def broken(plan, summaries, context):
        raise RuntimeError("reviewer crashed")

    coordinator = _coordinator(
        tmp_path, sessions, clock,
        plan_factory=_single_step_factory("compiler", "vault.read"),
        reviewer=broken)
    result = coordinator.invoke(objective="read", context=_context())

    assert result.response["status"] == "succeeded"


def test_a_missing_result_is_a_deterministic_finding_not_an_exception_first(
        tmp_path, sessions, clock):
    """A step producing nothing becomes a finding, which then exhausts repair
    rather than raising immediately — repair gets a chance before giving up."""
    def failing_handler(args, context):
        raise RuntimeError("boom")

    gateway = ModelGateway(settings=_settings(tmp_path),
                           local_model=ToolCallingFakeChatModel(
                               messages=iter([AIMessage(content="{}")] * 4)),
                           clock=clock)
    handlers = {"vault.read": RegisteredHandler(
        failing_handler, description="Read.",
        input_schema={"type": "object", "additionalProperties": True},
        owner_role="compiler")}
    invoker = CapabilityInvoker(
        registry=CapabilityRegistry(roots=[]), gateway=gateway,
        artifact_store=ArtifactStore(sessions=sessions, clock=clock),
        handlers=handlers)
    coordinator = Coordinator(
        invoker=invoker, plan_factory=_single_step_factory("compiler", "vault.read"),
        roles=RoleRegistry())

    with pytest.raises(RuntimeError, match="boom"):
        coordinator.invoke(objective="read", context=_context())
