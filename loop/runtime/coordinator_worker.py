"""Turning a claimed job into an actual coordinator run (agent-stack §1, §4).

The plan brief is specific: *"A due-job dispatcher must actually invoke the
graph; a tick that only fires triggers and dispatches an outbox is not the
complete service."* `LoopService.tick()` stays exactly as deterministic as it
is today — firing a trigger there must never call a model, or the idle-service
invariant (no inference with nothing to do) stops holding. What was missing is
the other half: a job whose *execution*, once claimed, is genuinely "run the
coordinator", not "compute the reply and pretend a graph decided it".

**The payload is inputs, not a live context.** `AuthorityContext` carries a
`RootBudget` and cannot survive a JSON column, so a job stores what is needed
to rebuild one at dispatch time — owner, scopes, privacy as JSON, budget
limits — never the object itself. The same applies to a resume decision.

**A job succeeds when the coordinator has finished with it, not before.** An
`awaiting_approval` result is not a job failure and not a bug: the run itself
is durable in the graph checkpoint, and the *job's* work — starting or
resuming that run — is done. The job completes; the run keeps waiting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from loop.agents.coordinator import Coordinator, CoordinatorResult
from loop.ai.budget import BudgetLimits, RootBudget
from loop.core.errors import ErrorCode, LoopError
from loop.core.ids import new_id
from loop.core.privacy import PrivacyLabel
from loop.runtime.authority import AuthorityContext
from loop.runtime.jobs import Job, JobQueue

logger = logging.getLogger(__name__)

#: The job kind a claim must have to reach this dispatcher. Distinct from
#: trigger/outbox jobs so `JobQueue.claim(kinds=[...])` can route by kind
#: without this worker seeing unrelated work.
START_KIND = "coordinator.start"
RESUME_KIND = "coordinator.resume"


def _context_from_payload(payload: dict[str, Any]) -> AuthorityContext:
    """Rebuild the authority a job needs, from data rather than a stored object."""
    limits = payload.get("budget_limits") or {}
    return AuthorityContext(
        owner=str(payload["owner"]), root_id=str(payload["root_id"]),
        privacy=PrivacyLabel.from_json(payload.get("privacy")),
        budget=RootBudget(limits=BudgetLimits(**limits),
                          root_id=str(payload["root_id"])),
        granted_scopes=frozenset(payload.get("granted_scopes", [])),
        policy_revision=payload.get("policy_revision"))


def build_start_payload(*, owner: str, root_id: str, objective: str,
                        operation: str | None = None,
                        arguments: dict[str, Any] | None = None,
                        privacy: PrivacyLabel | None = None,
                        granted_scopes: frozenset[str] = frozenset(),
                        budget_limits: BudgetLimits | None = None,
                        thread_id: str | None = None) -> dict[str, Any]:
    """The job payload for starting a coordinator run (agent-stack §2)."""
    limits = budget_limits or BudgetLimits()
    return {
        "owner": owner, "root_id": root_id, "objective": objective,
        "operation": operation, "arguments": arguments or {},
        "privacy": (privacy or PrivacyLabel.for_unlabelled_import()).to_json(),
        "granted_scopes": sorted(granted_scopes),
        "budget_limits": {"wall_clock_seconds": limits.wall_clock_seconds,
                          "model_calls": limits.model_calls,
                          "total_tokens": limits.total_tokens,
                          "tool_calls": limits.tool_calls},
        "thread_id": thread_id,
    }


def build_resume_payload(*, owner: str, root_id: str, run_id: str,
                         decision: dict[str, Any], actor: str,
                         privacy: PrivacyLabel | None = None,
                         granted_scopes: frozenset[str] = frozenset(),
                         budget_limits: BudgetLimits | None = None
                         ) -> dict[str, Any]:
    """The job payload for resuming a paused run (agent-stack §4)."""
    limits = budget_limits or BudgetLimits()
    return {
        "owner": owner, "root_id": root_id, "run_id": run_id,
        "decision": decision, "actor": actor,
        "privacy": (privacy or PrivacyLabel.for_unlabelled_import()).to_json(),
        "granted_scopes": sorted(granted_scopes),
        "budget_limits": {"wall_clock_seconds": limits.wall_clock_seconds,
                          "model_calls": limits.model_calls,
                          "total_tokens": limits.total_tokens,
                          "tool_calls": limits.tool_calls},
    }


def enqueue_coordinator_start(jobs: JobQueue, *, dedupe_key: str,
                              payload: dict[str, Any]) -> str | None:
    return jobs.enqueue(START_KIND, dedupe_key=dedupe_key, payload=payload)


def enqueue_coordinator_resume(jobs: JobQueue, *, dedupe_key: str,
                               payload: dict[str, Any]) -> str | None:
    return jobs.enqueue(RESUME_KIND, dedupe_key=dedupe_key, payload=payload)


@dataclass
class DispatchOutcome:
    job_id: str
    kind: str
    state: str                              # succeeded | failed | retry_wait
    result: CoordinatorResult | None = None
    error: str = ""


class CoordinatorJobWorker:
    """Claims coordinator jobs and actually runs the graph.

    Deliberately separate from `LoopService`: the deterministic sweep must
    keep working with no model configured at all (I6, "idle service performs
    no inference"), and this worker is the one place a claimed job is allowed
    to reach a model. A process may run both, or run this one only where a
    model is actually configured.
    """

    def __init__(self, *, jobs: JobQueue, coordinator: Coordinator) -> None:
        self.jobs = jobs
        self.coordinator = coordinator

    async def run_one(self, *, worker_id: str | None = None) -> DispatchOutcome | None:
        """Claim and execute one coordinator job, or return None if none is due."""
        job = self.jobs.claim(worker_id or new_id(), kinds=[START_KIND, RESUME_KIND])
        if job is None:
            return None
        return await self._execute(job)

    async def _execute(self, job: Job) -> DispatchOutcome:
        try:
            result = await self._invoke(job)
        except LoopError as exc:
            state = self.jobs.fail(job, code=exc.code)
            logger.warning("Coordinator job %s (%s) failed: %s", job.id,
                           job.kind, exc)
            return DispatchOutcome(job.id, job.kind, state, error=str(exc))
        except Exception as exc:                       # noqa: BLE001 — reported
            state = self.jobs.fail(job, code=ErrorCode.INTERNAL_ERROR)
            logger.exception("Coordinator job %s (%s) raised", job.id, job.kind)
            return DispatchOutcome(job.id, job.kind, state, error=str(exc))

        # A paused run is not a job failure: the run is durable in the graph
        # checkpoint, and this job's own work — starting or resuming it — is
        # done either way.
        self.jobs.succeed(job)
        return DispatchOutcome(job.id, job.kind, "succeeded", result=result)

    async def _invoke(self, job: Job) -> CoordinatorResult:
        payload = job.payload
        if job.kind == START_KIND:
            context = _context_from_payload(payload)
            return await self.coordinator.ainvoke(
                objective=str(payload["objective"]), context=context,
                operation=payload.get("operation"),
                arguments=payload.get("arguments") or {},
                thread_id=payload.get("thread_id"))

        context = _context_from_payload(payload)
        return await self.coordinator.aresume(
            run_id=str(payload["run_id"]), decision=dict(payload["decision"]),
            actor=str(payload["actor"]), context=context)
