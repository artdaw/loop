"""Durable child run identities for agent-mode tool calls (agent-stack §2, M2).

A dynamic graph invoked through a tool is invisible to the parent's own static
graph inspection — nothing about the coordinator's compiled edges names it. So
when a `RunStore` is available, an agent-mode capability call records itself as
a child of the run that triggered it, which is what makes it findable for
status or cancellation at all.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from loop.agents.coordinator import Coordinator
from loop.ai.model_gateway import ModelGateway
from loop.capabilities.runners import ArtifactStore, CapabilityInvoker
from loop.core.errors import LoopError
from loop.runtime.runs import RunStatus, RunStore
from tests.vnext.test_capability_runners import (
    ToolCallingFakeChatModel,
    _context,
    _handlers,
    _registry,
    _settings,
)


def _fern_advice_model() -> ToolCallingFakeChatModel:
    return ToolCallingFakeChatModel(messages=iter([
        AIMessage(content="", tool_calls=[{
            "name": "vault_search_" + "0" * 10,          # unused unless bound
            "args": {"query": "fern watering"},
            "id": "search-1", "type": "tool_call",
        }]),
        AIMessage(content=json.dumps({
            "answer": "Water after the top soil dries.",
            "sources": ["note://plants/fern"],
        })),
    ]))


def _invoker_with_runs(tmp_path, sessions, clock, runs):
    from tests.vnext.test_capability_runners import _tool_name

    registry = _registry(tmp_path / "caps")
    messages = iter([
        AIMessage(content="", tool_calls=[{
            "name": _tool_name("vault.search"),
            "args": {"query": "fern watering"},
            "id": "search-1", "type": "tool_call",
        }]),
        AIMessage(content=json.dumps({
            "answer": "Water after the top soil dries.",
            "sources": ["note://plants/fern"],
        })),
    ])
    gateway = ModelGateway(settings=_settings(tmp_path),
                           local_model=ToolCallingFakeChatModel(messages=messages),
                           clock=clock)
    return CapabilityInvoker(
        registry=registry, gateway=gateway,
        artifact_store=ArtifactStore(sessions=sessions, clock=clock),
        handlers=_handlers(), runs=runs)


# --------------------------------------------------------------------------- #
# No tracking without both a RunStore and a parent_run_id
# --------------------------------------------------------------------------- #
def test_no_child_is_recorded_without_a_run_store(tmp_path, sessions, clock):
    """Most unit tests build a bare CapabilityInvoker; that must keep working."""
    invoker = _invoker_with_runs(tmp_path, sessions, clock, runs=None)
    result = invoker.invoke("plantcare.advise", {"query": "fern watering"},
                            context=_context(), parent_run_id="root-1")

    assert result.output["answer"] == "Water after the top soil dries."


def test_no_child_is_recorded_without_a_parent_run_id(tmp_path, sessions, clock):
    """A direct call outside any coordinator run has nothing to attach to."""
    runs = RunStore(sessions=sessions, clock=clock)
    invoker = _invoker_with_runs(tmp_path, sessions, clock, runs=runs)
    invoker.invoke("plantcare.advise", {"query": "fern watering"},
                   context=_context())

    with sessions() as session:
        from sqlalchemy import text
        count = session.execute(
            text("SELECT COUNT(*) FROM run_mappings")).scalar()
    assert count == 0


# --------------------------------------------------------------------------- #
# A tracked agent-mode call records a real, findable child
# --------------------------------------------------------------------------- #
def test_an_agent_mode_call_records_a_child_run(tmp_path, sessions, clock):
    runs = RunStore(sessions=sessions, clock=clock)
    invoker = _invoker_with_runs(tmp_path, sessions, clock, runs=runs)

    invoker.invoke("plantcare.advise", {"query": "fern watering"},
                   context=_context(), parent_run_id="parent-1")

    children = runs.children_of("parent-1")
    assert len(children) == 1
    assert children[0].parent_run_id == "parent-1"


def test_the_child_is_findable_by_status_query_alone(tmp_path, sessions, clock):
    """This is the whole point: no static edge names it, so lookup must."""
    runs = RunStore(sessions=sessions, clock=clock)
    invoker = _invoker_with_runs(tmp_path, sessions, clock, runs=runs)

    invoker.invoke("plantcare.advise", {"query": "fern watering"},
                   context=_context(), parent_run_id="parent-2")

    child = runs.children_of("parent-2")[0]
    refetched = runs.get(child.id)
    assert refetched is not None
    assert refetched.status is RunStatus.SUCCEEDED


def test_a_successful_call_marks_the_child_succeeded(tmp_path, sessions, clock):
    runs = RunStore(sessions=sessions, clock=clock)
    invoker = _invoker_with_runs(tmp_path, sessions, clock, runs=runs)

    invoker.invoke("plantcare.advise", {"query": "fern watering"},
                   context=_context(), parent_run_id="parent-3")

    assert runs.children_of("parent-3")[0].status is RunStatus.SUCCEEDED


def test_a_failing_call_marks_the_child_failed_not_left_running(tmp_path,
                                                                sessions, clock):
    """A crash must not leave a child mapping stuck showing 'running' forever."""
    import pytest

    registry = _registry(tmp_path / "caps")
    broken_model = ToolCallingFakeChatModel(messages=iter([
        AIMessage(content="not valid json at all"),
    ]))
    gateway = ModelGateway(settings=_settings(tmp_path), local_model=broken_model,
                           clock=clock)
    runs = RunStore(sessions=sessions, clock=clock)
    invoker = CapabilityInvoker(
        registry=registry, gateway=gateway,
        artifact_store=ArtifactStore(sessions=sessions, clock=clock),
        handlers=_handlers(), runs=runs)

    with pytest.raises(LoopError):
        invoker.invoke("plantcare.advise", {"query": "fern watering"},
                       context=_context(), parent_run_id="parent-4")

    child = runs.children_of("parent-4")[0]
    assert child.status is RunStatus.FAILED


def test_two_children_of_the_same_parent_are_both_recorded(tmp_path, sessions,
                                                           clock):
    runs = RunStore(sessions=sessions, clock=clock)
    invoker = _invoker_with_runs(tmp_path, sessions, clock, runs=runs)

    invoker.invoke("plantcare.advise", {"query": "a"}, context=_context(),
                   parent_run_id="parent-5")
    # A second registry/model pair is needed since the fake model's message
    # iterator is single-use; re-invoking the same invoker with the same
    # capability would exhaust it, not the tracking logic under test.
    invoker2 = _invoker_with_runs(tmp_path, sessions, clock, runs=runs)
    invoker2.invoke("plantcare.advise", {"query": "b"}, context=_context(),
                    parent_run_id="parent-5")

    assert len(runs.children_of("parent-5")) == 2


def test_children_of_different_parents_do_not_mix(tmp_path, sessions, clock):
    runs = RunStore(sessions=sessions, clock=clock)
    invoker_a = _invoker_with_runs(tmp_path, sessions, clock, runs=runs)
    invoker_a.invoke("plantcare.advise", {"query": "a"}, context=_context(),
                     parent_run_id="parent-6")
    invoker_b = _invoker_with_runs(tmp_path, sessions, clock, runs=runs)
    invoker_b.invoke("plantcare.advise", {"query": "b"}, context=_context(),
                     parent_run_id="parent-7")

    assert len(runs.children_of("parent-6")) == 1
    assert len(runs.children_of("parent-7")) == 1


# --------------------------------------------------------------------------- #
# The coordinator threads its own run id into every step it invokes
# --------------------------------------------------------------------------- #
def test_the_coordinator_threads_its_run_id_as_the_parent(tmp_path, sessions,
                                                          clock):
    runs = RunStore(sessions=sessions, clock=clock)
    invoker = _invoker_with_runs(tmp_path, sessions, clock, runs=runs)
    coordinator = Coordinator(invoker=invoker, runs=runs)

    result = coordinator.invoke(
        objective="How often should I water my fern?",
        operation="plantcare.advise", arguments={"query": "fern watering"},
        context=_context())

    children = runs.children_of(result.run_id)
    assert len(children) == 1
    assert children[0].status is RunStatus.SUCCEEDED


def test_a_local_run_with_no_run_store_records_no_children(tmp_path, sessions,
                                                           clock):
    """Coordinator itself has no RunStore; nothing is tracked, and nothing breaks."""
    invoker = _invoker_with_runs(tmp_path, sessions, clock, runs=None)
    coordinator = Coordinator(invoker=invoker)

    result = coordinator.invoke(
        objective="How often should I water my fern?",
        operation="plantcare.advise", arguments={"query": "fern watering"},
        context=_context())

    assert result.response["status"] == "succeeded"
