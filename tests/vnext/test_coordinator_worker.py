"""A claimed job that actually invokes the graph (agent-stack §1, §4).

The gap this closes: `LoopService.tick()` fires triggers and dispatches the
outbox, deterministically, with no model. That must stay true — but nothing
in the system was demonstrated to *complete* a coordinator run because a job
was claimed for it. These tests are that demonstration, through a real job
queue and a real (async, checkpointed) coordinator.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage

from loop.agents.coordinator import Coordinator
from loop.ai.budget import BudgetLimits
from loop.ai.model_gateway import ModelGateway
from loop.capabilities.registry import CapabilityRegistry
from loop.capabilities.runners import (
    ArtifactStore,
    CapabilityInvoker,
    RegisteredHandler,
)
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.core.settings import Settings
from loop.runtime.checkpointer import open_production_checkpointer
from loop.runtime.coordinator_worker import (
    RESUME_KIND,
    START_KIND,
    CoordinatorJobWorker,
    build_resume_payload,
    build_start_payload,
    enqueue_coordinator_resume,
    enqueue_coordinator_start,
)
from loop.runtime.jobs import JobQueue
from loop.runtime.operations import OperationLedger
from loop.runtime.runs import RunStatus, RunStore
from tests.vnext.test_capability_runners import ToolCallingFakeChatModel

WORK = PrivacyLabel(model_scope=ModelScope.CLOUD_ALLOWED, origins=frozenset({"cli"}))


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, data_dir=str(tmp_path),  # type: ignore[call-arg]
                    ollama_default_model="llama3.1:8b")


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
# No due work — the deterministic case
# --------------------------------------------------------------------------- #
async def test_no_due_job_dispatches_nothing(tmp_path, sessions, clock):
    """Idle means idle: nothing is invoked when nothing was enqueued (I6)."""
    jobs = JobQueue(sessions=sessions, clock=clock)
    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, [],
                                   runs=RunStore(sessions=sessions, clock=clock),
                                   approvals=None)
        worker = CoordinatorJobWorker(jobs=jobs, coordinator=coordinator)
        outcome = await worker.run_one()

    assert outcome is None


# --------------------------------------------------------------------------- #
# A claimed start job actually runs the graph
# --------------------------------------------------------------------------- #
async def test_a_claimed_start_job_completes_a_real_run(tmp_path, sessions, clock):
    jobs = JobQueue(sessions=sessions, clock=clock)
    payload = build_start_payload(owner="owner", root_id="root-1",
                                  objective="read", operation="vault.read")
    enqueue_coordinator_start(jobs, dedupe_key="job-1", payload=payload)

    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, [],
                                   runs=RunStore(sessions=sessions, clock=clock),
                                   approvals=None)
        worker = CoordinatorJobWorker(jobs=jobs, coordinator=coordinator)
        outcome = await worker.run_one()

    assert outcome is not None
    assert outcome.state == "succeeded"
    assert outcome.result.response["status"] == "succeeded"


def test_the_job_is_actually_marked_succeeded_in_the_queue(tmp_path, sessions,
                                                           clock):
    """Not just an in-memory return value — the durable row changes state."""
    import asyncio

    jobs = JobQueue(sessions=sessions, clock=clock)
    payload = build_start_payload(owner="owner", root_id="root-1",
                                  objective="read", operation="vault.read")
    job_id = enqueue_coordinator_start(jobs, dedupe_key="job-2", payload=payload)

    async def run():
        async with open_production_checkpointer(tmp_path) as saver:
            coordinator = _coordinator(
                tmp_path, sessions, clock, saver, [],
                runs=RunStore(sessions=sessions, clock=clock), approvals=None)
            await CoordinatorJobWorker(jobs=jobs, coordinator=coordinator).run_one()

    asyncio.run(run())
    assert jobs.get(job_id).state == "succeeded"


# --------------------------------------------------------------------------- #
# A start job that pauses is a completed job, not a failed one
# --------------------------------------------------------------------------- #
async def test_a_start_job_that_pauses_still_succeeds_as_a_job(tmp_path, sessions,
                                                               clock):
    """The run keeps waiting; the job's own work — starting it — is done."""
    jobs = JobQueue(sessions=sessions, clock=clock)
    runs = RunStore(sessions=sessions, clock=clock)
    ledger = OperationLedger(sessions=sessions, clock=clock)
    payload = build_start_payload(owner="owner", root_id="root-1",
                                  objective="send it", operation="email.send",
                                  arguments={"body": "hello"})
    enqueue_coordinator_start(jobs, dedupe_key="job-3", payload=payload)

    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, [],
                                   runs=runs, approvals=ledger)
        outcome = await CoordinatorJobWorker(
            jobs=jobs, coordinator=coordinator).run_one()

    assert outcome.state == "succeeded"
    assert outcome.result.is_paused is True
    assert runs.get(outcome.result.run_id).status is RunStatus.PAUSED


async def test_a_resume_job_completes_the_paused_run_after_a_restart(
        tmp_path, sessions, clock):
    """The full path: start job pauses, process restarts, resume job finishes it."""
    jobs = JobQueue(sessions=sessions, clock=clock)
    runs = RunStore(sessions=sessions, clock=clock)
    ledger = OperationLedger(sessions=sessions, clock=clock)
    sent: list[str] = []

    start_payload = build_start_payload(owner="owner", root_id="root-1",
                                        objective="send it",
                                        operation="email.send",
                                        arguments={"body": "hello"})
    enqueue_coordinator_start(jobs, dedupe_key="job-4a", payload=start_payload)

    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, sent,
                                   runs=runs, approvals=ledger)
        start_outcome = await CoordinatorJobWorker(
            jobs=jobs, coordinator=coordinator).run_one()

    assert sent == []
    run_id = start_outcome.result.run_id

    resume_payload = build_resume_payload(
        owner="owner", root_id="root-1", run_id=run_id,
        decision={"approved": True, "actor": "owner"}, actor="owner")
    enqueue_coordinator_resume(jobs, dedupe_key="job-4b", payload=resume_payload)

    # A fresh checkpointer instance against the same file: the restart.
    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, sent,
                                   runs=runs, approvals=ledger)
        resume_outcome = await CoordinatorJobWorker(
            jobs=jobs, coordinator=coordinator).run_one()

    assert resume_outcome.state == "succeeded"
    assert resume_outcome.result.is_paused is False
    assert sent == ["hello"]
    assert runs.get(run_id).status is RunStatus.SUCCEEDED


# --------------------------------------------------------------------------- #
# A real failure does not retry forever
# --------------------------------------------------------------------------- #
async def test_an_unenabled_operation_fails_the_job_permanently(tmp_path,
                                                                sessions, clock):
    jobs = JobQueue(sessions=sessions, clock=clock)
    payload = build_start_payload(owner="owner", root_id="root-1",
                                  objective="do it", operation="shell.run")
    enqueue_coordinator_start(jobs, dedupe_key="job-5", payload=payload)

    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, [],
                                   runs=RunStore(sessions=sessions, clock=clock),
                                   approvals=None)
        outcome = await CoordinatorJobWorker(
            jobs=jobs, coordinator=coordinator).run_one()

    assert outcome.state == "failed"
    assert jobs.claim("someone-else", kinds=[START_KIND, RESUME_KIND]) is None


# --------------------------------------------------------------------------- #
# The payload carries inputs, not a live object
# --------------------------------------------------------------------------- #
def test_the_start_payload_is_plain_json_serialisable():
    import json

    payload = build_start_payload(owner="owner", root_id="root-1",
                                  objective="x",
                                  privacy=PrivacyLabel(sensitive=True),
                                  granted_scopes=frozenset({"vault.read"}),
                                  budget_limits=BudgetLimits(model_calls=3))
    json.dumps(payload)                     # raises if anything is a live object

    assert payload["budget_limits"]["model_calls"] == 3
    assert payload["granted_scopes"] == ["vault.read"]


def test_the_resume_payload_is_plain_json_serialisable():
    import json

    payload = build_resume_payload(owner="owner", root_id="root-1",
                                   run_id="r1", decision={"approved": True},
                                   actor="owner")
    json.dumps(payload)


async def test_the_worker_never_claims_an_unrelated_job_kind(tmp_path, sessions,
                                                              clock):
    """A trigger/outbox job enqueued by other parts of the service must not be
    swallowed by this worker — it is scoped to coordinator jobs only."""
    jobs = JobQueue(sessions=sessions, clock=clock)
    jobs.enqueue("deliver", dedupe_key="unrelated-1", payload={"outbox_id": "o1"})

    async with open_production_checkpointer(tmp_path) as saver:
        coordinator = _coordinator(tmp_path, sessions, clock, saver, [],
                                   runs=RunStore(sessions=sessions, clock=clock),
                                   approvals=None)
        outcome = await CoordinatorJobWorker(
            jobs=jobs, coordinator=coordinator).run_one()

    assert outcome is None
    assert jobs.claim("someone-else", kinds=["deliver"]) is not None
