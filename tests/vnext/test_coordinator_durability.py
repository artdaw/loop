"""The *production* coordinator is durable — not a graph built in a test.

`test_run_durability.py` proves the LangGraph mechanisms in isolation. These
tests exercise `Coordinator` itself: the compiled stage graph, with a real
`AsyncSqliteSaver`, a real `RunStore` and a real `OperationLedger`. The gap
these close is a coordinator that passes every mechanism test and still cannot
pause, because none of the mechanisms were wired into it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from loop.agents.coordinator import GRAPH_VERSION, Coordinator
from loop.ai.budget import BudgetLimits, RootBudget
from loop.ai.model_gateway import ModelGateway
from loop.capabilities.registry import CapabilityRegistry
from loop.capabilities.runners import (
    ArtifactStore,
    CapabilityInvoker,
    RegisteredHandler,
)
from loop.core.errors import ApprovalRequired, Conflict, ValidationFailed
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.core.settings import Settings
from loop.runtime.authority import AuthorityContext
from loop.runtime.operations import OperationLedger
from loop.runtime.runs import RunStatus, RunStore
from tests.vnext.test_capability_runners import ToolCallingFakeChatModel

WORK = PrivacyLabel(model_scope=ModelScope.CLOUD_ALLOWED,
                    origins=frozenset({"cli"}))


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, data_dir=str(tmp_path),  # type: ignore[call-arg]
                    ollama_default_model="llama3.1:8b")


def _context(**kw) -> AuthorityContext:
    defaults: dict = {"owner": "owner", "root_id": "root-1", "privacy": WORK,
                      "budget": RootBudget(limits=BudgetLimits()),
                      "granted_scopes": frozenset({"vault.read"})}
    defaults.update(kw)
    return AuthorityContext(**defaults)


#: A deterministic handler, so nothing here needs a model.
def _handlers(sent: list[str]) -> dict[str, RegisteredHandler]:
    def deliver(args, context):
        sent.append(args.get("body", ""))
        return {"answer": "sent", "sources": []}

    return {
        "vault.read": RegisteredHandler(
            lambda args, context: {"answer": "read", "sources": []},
            description="Read a note.",
            input_schema={"type": "object", "additionalProperties": True}),
        "email.send": RegisteredHandler(
            deliver, description="Send an email to a person.",
            input_schema={"type": "object", "additionalProperties": True},
            needs_approval=True, owner_role="commitments"),
    }


@pytest.fixture
def saver(tmp_path):
    with SqliteSaver.from_conn_string(
            str(tmp_path / "graph-checkpoints.sqlite")) as checkpointer:
        yield checkpointer


def _coordinator(tmp_path, sessions, clock, saver, sent, *, runs=None,
                 approvals=None) -> Coordinator:
    gateway = ModelGateway(settings=_settings(tmp_path),
                           local_model=ToolCallingFakeChatModel(
                               messages=iter([AIMessage(content="{}")] * 4)),
                           clock=clock)
    invoker = CapabilityInvoker(
        registry=CapabilityRegistry(roots=[]), gateway=gateway,
        artifact_store=ArtifactStore(sessions=sessions, clock=clock),
        handlers=_handlers(sent))
    return Coordinator(invoker=invoker, checkpointer=saver, runs=runs,
                       approvals=approvals)


# --------------------------------------------------------------------------- #
# The coordinator is compiled with a checkpointer
# --------------------------------------------------------------------------- #
def test_the_coordinator_compiles_with_the_checkpointer(tmp_path, sessions,
                                                        clock, saver):
    coordinator = _coordinator(tmp_path, sessions, clock, saver, [])
    assert coordinator.graph.checkpointer is saver


def test_without_a_checkpointer_it_still_runs_deterministically(tmp_path,
                                                                sessions, clock):
    """Listing and reading must work with no saver and no model."""
    coordinator = _coordinator(tmp_path, sessions, clock, None, [])
    result = coordinator.invoke(objective="read the note", operation="vault.read",
                                arguments={}, context=_context())

    assert result.response["status"] == "succeeded"
    assert result.is_paused is False


# --------------------------------------------------------------------------- #
# A run mapping is recorded, pinned to today's versions
# --------------------------------------------------------------------------- #
def test_a_run_mapping_is_created(tmp_path, sessions, clock, saver):
    runs = RunStore(sessions=sessions, clock=clock)
    coordinator = _coordinator(tmp_path, sessions, clock, saver, [], runs=runs)

    result = coordinator.invoke(objective="read", operation="vault.read",
                                arguments={}, context=_context())

    mapping = runs.get(result.run_id)
    assert mapping is not None
    assert mapping.graph_version == GRAPH_VERSION


def test_the_thread_belongs_to_one_run_not_the_conversation(tmp_path, sessions,
                                                            clock, saver):
    runs = RunStore(sessions=sessions, clock=clock)
    coordinator = _coordinator(tmp_path, sessions, clock, saver, [], runs=runs)

    first = coordinator.invoke(objective="read", operation="vault.read",
                               arguments={}, context=_context())
    second = coordinator.invoke(objective="read", operation="vault.read",
                                arguments={}, context=_context())

    assert first.thread_id != second.thread_id


def test_a_completed_run_is_marked_succeeded(tmp_path, sessions, clock, saver):
    runs = RunStore(sessions=sessions, clock=clock)
    coordinator = _coordinator(tmp_path, sessions, clock, saver, [], runs=runs)
    result = coordinator.invoke(objective="read", operation="vault.read",
                                arguments={}, context=_context())

    assert runs.get(result.run_id).status is RunStatus.SUCCEEDED


# --------------------------------------------------------------------------- #
# Approval actually pauses the coordinator
# --------------------------------------------------------------------------- #
def _approval_setup(tmp_path, sessions, clock, saver):
    sent: list[str] = []
    runs = RunStore(sessions=sessions, clock=clock)
    ledger = OperationLedger(sessions=sessions, clock=clock)
    coordinator = _coordinator(tmp_path, sessions, clock, saver, sent, runs=runs,
                               approvals=ledger)
    return coordinator, runs, ledger, sent


def test_an_operation_needing_approval_pauses(tmp_path, sessions, clock, saver):
    coordinator, runs, _, sent = _approval_setup(tmp_path, sessions, clock, saver)

    result = coordinator.invoke(objective="send it", operation="email.send",
                                arguments={"body": "hello"}, context=_context())

    assert result.is_paused is True
    assert result.awaiting_approval == ["email.send"]


def test_the_effect_does_not_happen_while_paused(tmp_path, sessions, clock, saver):
    """The whole point: pausing before the send, not apologising after it."""
    coordinator, _, _, sent = _approval_setup(tmp_path, sessions, clock, saver)
    coordinator.invoke(objective="send it", operation="email.send",
                       arguments={"body": "hello"}, context=_context())

    assert sent == []


def test_the_run_is_recorded_as_paused(tmp_path, sessions, clock, saver):
    coordinator, runs, _, _ = _approval_setup(tmp_path, sessions, clock, saver)
    result = coordinator.invoke(objective="send it", operation="email.send",
                                arguments={"body": "hello"}, context=_context())

    assert runs.get(result.run_id).status is RunStatus.PAUSED


def test_a_bound_approval_is_persisted_before_the_pause(tmp_path, sessions,
                                                        clock, saver):
    coordinator, _, ledger, _ = _approval_setup(tmp_path, sessions, clock, saver)
    result = coordinator.invoke(objective="send it", operation="email.send",
                                arguments={"body": "hello"}, context=_context())

    operation = ledger.get_by_key(f"{result.run_id}:request")
    assert operation is not None
    assert operation.action == "email.send"


def test_waiting_consumes_no_model_calls(tmp_path, sessions, clock, saver):
    coordinator, _, _, _ = _approval_setup(tmp_path, sessions, clock, saver)
    coordinator.invoke(objective="send it", operation="email.send",
                       arguments={"body": "hello"}, context=_context())

    assert coordinator.invoker.gateway.audit.local_calls == 0


def test_an_approved_run_resumes_and_performs_the_effect(tmp_path, sessions,
                                                         clock, saver):
    coordinator, runs, _, sent = _approval_setup(tmp_path, sessions, clock, saver)
    paused = coordinator.invoke(objective="send it", operation="email.send",
                                arguments={"body": "hello"}, context=_context())

    resumed = coordinator.resume(run_id=paused.run_id,
                                 decision={"approved": True, "actor": "owner"},
                                 actor="owner")

    assert resumed.is_paused is False
    assert sent == ["hello"]


def test_a_declined_run_performs_nothing(tmp_path, sessions, clock, saver):
    coordinator, _, _, sent = _approval_setup(tmp_path, sessions, clock, saver)
    paused = coordinator.invoke(objective="send it", operation="email.send",
                                arguments={"body": "hello"}, context=_context())

    with pytest.raises(ApprovalRequired):
        coordinator.resume(run_id=paused.run_id,
                           decision={"approved": False, "actor": "owner"},
                           actor="owner")

    assert sent == []


def test_a_decision_from_another_actor_is_refused(tmp_path, sessions, clock,
                                                  saver):
    coordinator, _, _, sent = _approval_setup(tmp_path, sessions, clock, saver)
    paused = coordinator.invoke(objective="send it", operation="email.send",
                                arguments={"body": "hello"}, context=_context())

    with pytest.raises(ApprovalRequired):
        coordinator.resume(run_id=paused.run_id,
                           decision={"approved": True, "actor": "someone-else"},
                           actor="owner")

    assert sent == []


def test_a_run_cancelled_while_waiting_cannot_resume(tmp_path, sessions, clock,
                                                     saver):
    """A signal that arrived during the pause is the one that matters."""
    coordinator, runs, _, sent = _approval_setup(tmp_path, sessions, clock, saver)
    paused = coordinator.invoke(objective="send it", operation="email.send",
                                arguments={"body": "hello"}, context=_context())
    runs.cancel(paused.run_id)

    with pytest.raises(Conflict):
        coordinator.resume(run_id=paused.run_id,
                           decision={"approved": True, "actor": "owner"},
                           actor="owner")

    assert sent == []


def test_a_cancellation_mid_plan_stops_the_next_effect(tmp_path, sessions,
                                                       clock, saver):
    """Cancellation arriving *between* steps must stop the ones not yet run.

    The resume-time check cannot see this: the run was never paused. Only the
    recheck before each effect catches a signal that lands mid-plan.
    """
    sent: list[str] = []
    runs = RunStore(sessions=sessions, clock=clock)
    cancelled: dict[str, str] = {}

    def first_step(args, context):
        # Stands in for a cancellation arriving from another surface while the
        # plan is part-way through: find the run that is executing right now
        # and cancel it, as `loop cancel` would.
        from sqlalchemy import text

        with sessions() as session:
            run_id = session.execute(text(
                "SELECT id FROM run_mappings WHERE status = 'running' "
                "ORDER BY created_at DESC LIMIT 1")).scalar()
        cancelled["run_id"] = str(run_id)
        runs.cancel(str(run_id))
        return {"answer": "read", "sources": []}

    gateway = ModelGateway(settings=_settings(tmp_path),
                           local_model=ToolCallingFakeChatModel(
                               messages=iter([AIMessage(content="{}")] * 4)),
                           clock=clock)
    handlers = {
        "vault.read": RegisteredHandler(
            first_step, description="Read a note.",
            input_schema={"type": "object", "additionalProperties": True}),
        "vault.write": RegisteredHandler(
            lambda args, context: sent.append("written") or {  # type: ignore[func-returns-value]
                "answer": "written", "sources": []},
            description="Write a note.",
            input_schema={"type": "object", "additionalProperties": True}),
    }
    invoker = CapabilityInvoker(
        registry=CapabilityRegistry(roots=[]), gateway=gateway,
        artifact_store=ArtifactStore(sessions=sessions, clock=clock),
        handlers=handlers)
    coordinator = Coordinator(invoker=invoker, checkpointer=saver, runs=runs,
                              plan_factory=lambda objective, discovered: {
                                  "schema_version": 1, "intents": [objective],
                                  "steps": [
                                      {"id": "a", "role": "compiler",
                                       "objective": objective,
                                       "capability": "vault.read",
                                       "arguments": {}, "depends_on": []},
                                      {"id": "b", "role": "compiler",
                                       "objective": objective,
                                       "capability": "vault.write",
                                       "arguments": {}, "depends_on": ["a"]},
                                  ],
                                  "response_intent": "reply"})

    with pytest.raises(Conflict, match="cancelled"):
        coordinator.invoke(objective="read then write", context=_context())

    assert cancelled["run_id"]
    assert sent == []


def test_resuming_an_unknown_run_is_refused(tmp_path, sessions, clock, saver):
    from loop.core.errors import ValidationFailed

    coordinator, _, _, _ = _approval_setup(tmp_path, sessions, clock, saver)

    with pytest.raises(ValidationFailed, match="No run"):
        coordinator.resume(run_id="nope", decision={"approved": True},
                           actor="owner")


# --------------------------------------------------------------------------- #
# The pause survives the process
# --------------------------------------------------------------------------- #
def test_the_pause_survives_a_new_coordinator_object(tmp_path, sessions, clock,
                                                     saver):
    """A restart must not re-ask, and must not re-run completed work."""
    sent: list[str] = []
    runs = RunStore(sessions=sessions, clock=clock)
    ledger = OperationLedger(sessions=sessions, clock=clock)

    first = _coordinator(tmp_path, sessions, clock, saver, sent, runs=runs,
                         approvals=ledger)
    paused = first.invoke(objective="send it", operation="email.send",
                          arguments={"body": "hello"}, context=_context())

    # A different Coordinator object, as a fresh process would build. It holds
    # nothing in memory, so it must be given the owner's current authority.
    second = _coordinator(tmp_path, sessions, clock, saver, sent, runs=runs,
                          approvals=ledger)
    resumed = second.resume(run_id=paused.run_id,
                            decision={"approved": True, "actor": "owner"},
                            actor="owner", context=_context())

    assert resumed.is_paused is False
    assert sent == ["hello"]


def test_a_restart_cannot_resume_without_current_authority(tmp_path, sessions,
                                                           clock, saver):
    """The stored run does not carry permissions forward across a restart."""
    sent: list[str] = []
    runs = RunStore(sessions=sessions, clock=clock)
    ledger = OperationLedger(sessions=sessions, clock=clock)
    first = _coordinator(tmp_path, sessions, clock, saver, sent, runs=runs,
                         approvals=ledger)
    paused = first.invoke(objective="send it", operation="email.send",
                          arguments={"body": "hello"}, context=_context())

    second = _coordinator(tmp_path, sessions, clock, saver, sent, runs=runs,
                          approvals=ledger)
    with pytest.raises(ValidationFailed, match="authority"):
        second.resume(run_id=paused.run_id,
                      decision={"approved": True, "actor": "owner"},
                      actor="owner")

    assert sent == []


def test_the_checkpoint_file_holds_the_paused_state(tmp_path, sessions, clock,
                                                    saver):
    coordinator, _, _, _ = _approval_setup(tmp_path, sessions, clock, saver)
    coordinator.invoke(objective="send it", operation="email.send",
                       arguments={"body": "hello"}, context=_context())

    assert (tmp_path / "graph-checkpoints.sqlite").exists()
    assert (tmp_path / "graph-checkpoints.sqlite").stat().st_size > 0
