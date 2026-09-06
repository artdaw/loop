"""The Coordinator across an actual process restart, using AsyncSqliteSaver.

`test_coordinator_durability.py` proves pause/resume with the synchronous
`SqliteSaver`. Production uses `AsyncSqliteSaver` (agent-stack §4), whose
async-only interface needs `ainvoke`/`aget_state` rather than `invoke`/
`get_state` — a sync call against it raises rather than degrading quietly, so
the two paths are not interchangeable and both need their own proof.

The restart itself is real: two separate `AsyncSqliteSaver` instances are
opened against the *same file*, with the first closed before the second
opens — the closest a single test process gets to "the coordinator was killed
and a new one started against the same data directory" (agent-stack §4,
M2 exit evidence: "actual process restart and approval resume").
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from loop.agents.coordinator import Coordinator
from loop.ai.budget import BudgetLimits, RootBudget
from loop.ai.model_gateway import ModelGateway
from loop.capabilities.registry import CapabilityRegistry
from loop.capabilities.runners import (
    ArtifactStore,
    CapabilityInvoker,
    RegisteredHandler,
)
from loop.core.errors import ApprovalRequired
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.core.settings import Settings
from loop.runtime.authority import AuthorityContext
from loop.runtime.checkpointer import open_production_checkpointer
from loop.runtime.operations import OperationLedger
from loop.runtime.runs import RunStatus, RunStore
from tests.vnext.test_capability_runners import ToolCallingFakeChatModel

pytestmark = pytest.mark.asyncio

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


def _handlers(sent: list[str]) -> dict[str, RegisteredHandler]:
    def deliver(args, context):
        sent.append(args.get("body", ""))
        return {"answer": "sent", "sources": []}

    return {
        "vault.read": RegisteredHandler(
            lambda args, context: {"answer": "read", "sources": []},
            description="Read a note.",
            input_schema={"type": "object", "additionalProperties": True},
            owner_role="compiler"),
        "email.send": RegisteredHandler(
            deliver, description="Send an email.",
            input_schema={"type": "object", "additionalProperties": True},
            needs_approval=True, owner_role="commitments"),
    }


def _coordinator(tmp_path, sessions, clock, saver, sent, *, runs, approvals
                 ) -> Coordinator:
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
# The production factory itself
# --------------------------------------------------------------------------- #
async def test_the_production_checkpointer_opens_at_the_documented_path(tmp_path):
    async with open_production_checkpointer(tmp_path) as saver:
        assert isinstance(saver, AsyncSqliteSaver)

    path = tmp_path / "graph-checkpoints.sqlite"
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"


async def test_reopening_the_same_path_reaches_the_same_database(tmp_path):
    """A checkpoint written under one saver is visible to a fresh one at the
    same path — the whole basis for a restart being able to resume anything."""
    config = {"configurable": {"thread_id": "probe", "checkpoint_ns": ""}}
    checkpoint = {"v": 1, "ts": "", "id": "c1", "channel_values": {},
                 "channel_versions": {}, "versions_seen": {}}

    async with open_production_checkpointer(tmp_path) as saver:
        await saver.aput(config, checkpoint, {}, {})

    async with open_production_checkpointer(tmp_path) as saver:
        found = await saver.aget(config)
        assert found is not None
        assert found["id"] == "c1"


# --------------------------------------------------------------------------- #
# The coordinator, async, without a restart
# --------------------------------------------------------------------------- #
async def test_an_async_run_completes_without_a_restart(tmp_path, sessions, clock):
    sent: list[str] = []
    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, sent,
                                   runs=RunStore(sessions=sessions, clock=clock),
                                   approvals=None)
        result = await coordinator.ainvoke(
            objective="read", operation="vault.read", arguments={},
            context=_context())

    assert result.response["status"] == "succeeded"


async def test_a_sync_call_against_the_async_only_saver_is_refused(tmp_path,
                                                                   sessions, clock):
    """Proves the two paths are not silently interchangeable.

    `AsyncSqliteSaver` refuses a synchronous call from the same thread/loop it
    was opened on, rather than serving a stale or partial read — the failure
    mode is loud, not a silent wrong answer.
    """
    import asyncio

    sent: list[str] = []
    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, sent,
                                   runs=RunStore(sessions=sessions, clock=clock),
                                   approvals=None)
        with pytest.raises(asyncio.InvalidStateError, match="async interface"):
            coordinator.invoke(objective="read", operation="vault.read",
                               arguments={}, context=_context())


# --------------------------------------------------------------------------- #
# The actual restart
# --------------------------------------------------------------------------- #
async def test_an_approval_pause_survives_reopening_the_checkpoint_file(
        tmp_path, sessions, clock):
    """Two separate AsyncSqliteSaver instances against the same file — the
    first closed before the second opens — is a real restart, not a re-used
    object standing in for one."""
    sent: list[str] = []
    runs = RunStore(sessions=sessions, clock=clock)
    ledger = OperationLedger(sessions=sessions, clock=clock)

    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, sent,
                                   runs=runs, approvals=ledger)
        paused = await coordinator.ainvoke(
            objective="send it", operation="email.send",
            arguments={"body": "hello"}, context=_context())

    assert paused.is_paused is True
    assert sent == []
    assert runs.get(paused.run_id).status is RunStatus.PAUSED

    # The first saver is fully closed (the `async with` block exited) before
    # this one opens. Nothing is shared except the file on disk.
    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, sent,
                                   runs=runs, approvals=ledger)
        resumed = await coordinator.aresume(
            run_id=paused.run_id,
            decision={"approved": True, "actor": "owner"},
            actor="owner", context=_context())

    assert resumed.is_paused is False
    assert sent == ["hello"]
    assert runs.get(paused.run_id).status is RunStatus.SUCCEEDED


async def test_a_declined_decision_after_restart_performs_nothing(tmp_path,
                                                                  sessions, clock):
    sent: list[str] = []
    runs = RunStore(sessions=sessions, clock=clock)
    ledger = OperationLedger(sessions=sessions, clock=clock)

    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, sent,
                                   runs=runs, approvals=ledger)
        paused = await coordinator.ainvoke(
            objective="send it", operation="email.send",
            arguments={"body": "hello"}, context=_context())

    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, sent,
                                   runs=runs, approvals=ledger)
        with pytest.raises(ApprovalRequired):
            await coordinator.aresume(
                run_id=paused.run_id,
                decision={"approved": False, "actor": "owner"},
                actor="owner", context=_context())

    assert sent == []


async def test_cancellation_during_the_restart_window_blocks_resume(tmp_path,
                                                                    sessions,
                                                                    clock):
    """A signal that arrived while the process was down still counts."""
    sent: list[str] = []
    runs = RunStore(sessions=sessions, clock=clock)
    ledger = OperationLedger(sessions=sessions, clock=clock)

    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, sent,
                                   runs=runs, approvals=ledger)
        paused = await coordinator.ainvoke(
            objective="send it", operation="email.send",
            arguments={"body": "hello"}, context=_context())

    runs.cancel(paused.run_id)                       # arrives while "down"

    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, sent,
                                   runs=runs, approvals=ledger)
        with pytest.raises(Exception, match="cancelled"):
            await coordinator.aresume(
                run_id=paused.run_id,
                decision={"approved": True, "actor": "owner"},
                actor="owner", context=_context())

    assert sent == []
