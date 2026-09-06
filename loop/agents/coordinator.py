"""Stable, domain-neutral LangGraph coordinator (agent-stack §2, §4).

Every request crosses the same stages. Capability metadata and typed plans
decide what runs; the graph has no edges for individual domains or pack names.

Three things make this a durable coordinator rather than a function call:

* **A checkpointer.** Compiled with one, the graph's execution position survives
  the process. Without it a restart mid-plan silently re-runs completed steps,
  and "resume" means "start again".
* **A real approval interrupt.** An operation needing approval persists a bound
  approval and calls `interrupt()`, which releases the worker and consumes no
  model calls while waiting. Resuming revalidates actor, payload hash, expiry,
  cancellation and permissions — the pause proves nothing about the present.
* **A run mapping.** The domain record of which thread belongs to which root
  run, pinned to the graph, schema and pack versions it started under, so a
  later upgrade pauses visibly instead of deserialising into a different graph.

Interrupt nodes restart from their beginning on resume, so everything before the
`interrupt()` call in that node must be replay-safe.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from loop.agents.roles import COORDINATOR, ROLE_IDS, RoleRegistry
from loop.capabilities.runners import CapabilityInvoker, InvocationResult
from loop.core.errors import ApprovalRequired, Conflict, ValidationFailed
from loop.runtime.authority import AuthorityContext
from loop.runtime.planner import PlanStep, TypedPlan, parse_plan, ready_steps
from loop.runtime.runs import RunStatus, RunStore

logger = logging.getLogger(__name__)

#: Bumped when the stage graph changes shape. Pinned per run.
GRAPH_VERSION = "1"
STATE_SCHEMA_VERSION = "1"


class CoordinatorState(TypedDict, total=False):
    """Checkpointed state.

    Everything here must survive `msgpack` and a process restart, so it holds
    identifiers and plain data only. The `AuthorityContext` is deliberately
    absent: it carries a live budget, granted scopes and a privacy label, and
    agent-stack §4 requires graph state to hold typed identifiers rather than
    open clients or credentials. It is supplied per invocation and *reloaded*
    on resume, which is also what the spec asks for — the authority that
    matters is the one in force now, not the one snapshotted when the run
    paused.
    """

    objective: str
    operation: str | None
    arguments: dict[str, Any]
    discovered: list[str]
    catalogue: dict[str, str]
    plan_payload: dict[str, Any]
    result_summaries: dict[str, dict[str, Any]]
    approval_required: list[str]
    approved: list[str]
    repair_attempts: int
    review_findings: list[str]
    response: dict[str, Any]
    stage_trace: list[str]
    run_id: str


@dataclass
class CoordinatorResult:
    """Grounded response and the results from which it was composed."""

    response: dict[str, Any]
    results: dict[str, InvocationResult]
    stage_trace: list[str] = field(default_factory=list)
    run_id: str = ""
    thread_id: str = ""
    #: Set when the run paused at an approval instead of finishing.
    awaiting_approval: list[str] = field(default_factory=list)

    @property
    def is_paused(self) -> bool:
        return bool(self.awaiting_approval)


PlanFactory = Callable[[str, list[str]], dict[str, Any]]

#: A Reviewer returns findings. An empty list means it found nothing wrong —
#: which is not the same as granting permission for anything.
Reviewer = Callable[[TypedPlan, dict[str, dict[str, Any]], AuthorityContext],
                    list[str]]

#: Repair attempts after the first execution. Bounded, and each spends the root
#: budget (agent-stack §3).
MAX_REPAIR_ATTEMPTS = 1


class Coordinator:
    """Run explicit operations or injected typed planning through one graph."""

    STAGES = (
        "load_context", "discover_operations", "produce_plan",
        "invoke_ready_assignments", "validate_results", "resolve_approval",
        "compose_response",
    )

    def __init__(self, *, invoker: CapabilityInvoker,
                 plan_factory: PlanFactory | None = None,
                 checkpointer: Any | None = None,
                 runs: RunStore | None = None,
                 approvals: Any | None = None,
                 roles: RoleRegistry | None = None,
                 planning_role: str = COORDINATOR,
                 reviewer: Reviewer | None = None) -> None:
        self.invoker = invoker
        self.plan_factory = plan_factory
        self.roles = roles or RoleRegistry()
        self.planning_role = planning_role
        self.reviewer = reviewer
        self.checkpointer = checkpointer
        self.runs = runs
        # The operation ledger, when supplied, is what makes an approval bound
        # to a payload rather than a yes/no the user vaguely remembers giving.
        self.approvals = approvals
        # Rich per-run objects that must not enter the checkpoint: the
        # authority context, the parsed plan and the invocation results. Keyed
        # by run id, so a leased worker advancing one thread cannot read
        # another's. Rebuilt on resume rather than restored.
        self._live: dict[str, dict[str, Any]] = {}
        self.graph = self._compile()

    def _context_for(self, state: CoordinatorState) -> AuthorityContext:
        live = self._live.get(state.get("run_id", ""))
        if live is None or "context" not in live:
            raise ValidationFailed(
                "No authority context is loaded for this run. A resumed run "
                "must be given the current context rather than a stored one.")
        return live["context"]

    # ------------------------------------------------------------------ #
    # Running
    # ------------------------------------------------------------------ #
    def invoke(self, *, objective: str, context: AuthorityContext,
               operation: str | None = None,
               arguments: dict[str, Any] | None = None,
               thread_id: str | None = None) -> CoordinatorResult:
        """Start a run synchronously. Returns paused when approval is required."""
        state, config, mapping = self._start(
            objective=objective, context=context, operation=operation,
            arguments=arguments, thread_id=thread_id)
        final = (self.graph.invoke(state, config) if config is not None
                 else self.graph.invoke(state))
        interrupts = _pending_interrupts(self.graph, config)
        return self._finish(final, interrupts, mapping)

    async def ainvoke(self, *, objective: str, context: AuthorityContext,
                      operation: str | None = None,
                      arguments: dict[str, Any] | None = None,
                      thread_id: str | None = None) -> CoordinatorResult:
        """Start a run through the async graph path (agent-stack §4).

        Required when the coordinator is compiled with `AsyncSqliteSaver`: an
        async-only checkpointer raises rather than serving a sync `.invoke()`
        call, because it has no synchronous connection to serve it from.
        """
        state, config, mapping = self._start(
            objective=objective, context=context, operation=operation,
            arguments=arguments, thread_id=thread_id)
        final = (await self.graph.ainvoke(state, config) if config is not None
                 else await self.graph.ainvoke(state))
        interrupts = await _apending_interrupts(self.graph, config)
        return self._finish(final, interrupts, mapping)

    def resume(self, *, run_id: str, decision: dict[str, Any], actor: str,
               context: AuthorityContext | None = None) -> CoordinatorResult:
        """Resume a paused run with an authenticated decision (agent-stack §4).

        The pause is not evidence about now. Everything is rechecked here —
        whether the run is still resumable, whether its pinned packs are still
        available, and whether the decision came from the right person.
        """
        mapping, config = self._prepare_resume(run_id=run_id, decision=decision,
                                               actor=actor, context=context)
        final = self.graph.invoke(Command(resume=decision), config)
        interrupts = _pending_interrupts(self.graph, config)
        return self._finish(final, interrupts, mapping)

    async def aresume(self, *, run_id: str, decision: dict[str, Any], actor: str,
                      context: AuthorityContext | None = None
                      ) -> CoordinatorResult:
        """`resume`, through the async graph path required by `AsyncSqliteSaver`."""
        mapping, config = self._prepare_resume(run_id=run_id, decision=decision,
                                               actor=actor, context=context)
        final = await self.graph.ainvoke(Command(resume=decision), config)
        interrupts = await _apending_interrupts(self.graph, config)
        return self._finish(final, interrupts, mapping)

    # ------------------------------------------------------------------ #
    # Shared setup, independent of sync/async execution
    # ------------------------------------------------------------------ #
    def _start(self, *, objective: str, context: AuthorityContext,
              operation: str | None, arguments: dict[str, Any] | None,
              thread_id: str | None
              ) -> tuple[dict[str, Any], dict[str, Any] | None, Any]:
        mapping = self._begin_run(context, thread_id=thread_id)
        run_id = mapping.id if mapping else "local"
        thread = mapping.thread_id if mapping else (thread_id or run_id)
        self._live[run_id] = {"context": context}

        state: dict[str, Any] = {
            "objective": objective,
            "operation": operation,
            "arguments": arguments or {},
            "stage_trace": [],
            "approved": [],
            "run_id": run_id,
        }
        return state, self._config(thread), mapping

    def _prepare_resume(self, *, run_id: str, decision: dict[str, Any],
                        actor: str, context: AuthorityContext | None
                        ) -> tuple[Any, dict[str, Any] | None]:
        if self.runs is None:
            raise ValidationFailed(
                "This coordinator has no run store, so it cannot resume a run.")

        mapping = self.runs.get(run_id)
        if mapping is None:
            raise ValidationFailed(f"No run {run_id!r}.")
        if mapping.status is RunStatus.CANCELLED:
            raise Conflict("This run was cancelled while it was waiting.",
                           details={"run_id": run_id})
        # Version pinning first: an upgrade during the pause must produce an
        # actionable paused status, not a silent deserialise into a different
        # graph. Then the resumability recheck, which also catches a pack
        # disabled while the run was waiting.
        self.runs.check_versions(run_id, available_packs=self._available_packs(),
                                 available_graph_versions={GRAPH_VERSION})
        self.runs.require_resumable(run_id, disabled_packs=self._disabled_packs())

        if decision.get("actor") not in (None, actor):
            raise ApprovalRequired(
                "This decision came from a different person than the one asked.",
                details={"run_id": run_id})

        # The authority in force *now*, not the one snapshotted when the run
        # paused: scopes may have been revoked and the budget may be spent.
        # A fresh process has nothing in memory and must supply it, which is
        # why this is a parameter rather than something restored from state.
        current = context or (self._live.get(run_id) or {}).get("context")
        if current is None:
            raise ValidationFailed(
                "Resuming this run needs the owner's current authority context; "
                "pass `context=` after a restart.",
                details={"run_id": run_id})
        self._live[run_id] = {"context": current}

        config = self._config(mapping.thread_id)
        self.runs.set_status(run_id, RunStatus.RUNNING)
        return mapping, config

    def _finish(self, final: dict[str, Any], interrupts: list[str],
               mapping: Any) -> CoordinatorResult:
        if interrupts:
            if self.runs is not None and mapping is not None:
                self.runs.set_status(mapping.id, RunStatus.PAUSED,
                                     reason="awaiting approval")
            return CoordinatorResult(
                response={"status": "awaiting_approval",
                          "summary": {}, "artifact_refs": [],
                          "evidence_refs": [], "uncertainties": []},
                results={},
                stage_trace=final.get("stage_trace", []),
                run_id=mapping.id if mapping else "",
                thread_id=mapping.thread_id if mapping else "",
                awaiting_approval=interrupts)

        if self.runs is not None and mapping is not None:
            self.runs.set_status(mapping.id, RunStatus.SUCCEEDED)

        run_id = final.get("run_id", "")
        results = (self._live.get(run_id) or {}).get("results", {})
        return CoordinatorResult(response=final["response"],
                                 results=results,
                                 stage_trace=final["stage_trace"],
                                 run_id=mapping.id if mapping else "",
                                 thread_id=mapping.thread_id if mapping else "")

    # ------------------------------------------------------------------ #
    # Run mapping and configuration
    # ------------------------------------------------------------------ #
    def _available_packs(self) -> dict[str, str]:
        return {entry.manifest.id: entry.manifest.version
                for entry in self.invoker.registry.entries()
                if entry.is_usable}

    def _disabled_packs(self) -> set[str]:
        return {entry.manifest.id for entry in self.invoker.registry.entries()
                if not entry.is_usable}

    def _begin_run(self, context: AuthorityContext, *,
                   thread_id: str | None) -> Any:
        """Record the domain run mapping, pinned to today's versions."""
        if self.runs is None:
            return None
        snapshot = self.invoker.registry.snapshot()
        return self.runs.create(
            root_event_id=context.root_id,
            graph_version=GRAPH_VERSION,
            state_schema_version=STATE_SCHEMA_VERSION,
            registry_revision=int(snapshot.get("revision", 0)),
            registry_hash=str(snapshot.get("hash", "")),
            policy_revision=getattr(context, "policy_revision", None),
            privacy=context.privacy,
            pinned_packs=self._available_packs(),
            thread_id=thread_id)

    def _config(self, thread_id: str | None) -> dict[str, Any] | None:
        """A thread belongs to one root run, never to a whole conversation."""
        if self.checkpointer is None or thread_id is None:
            return None
        return {"configurable": {"thread_id": thread_id}}

    def _compile(self) -> Any:
        graph = StateGraph(CoordinatorState)
        graph.add_node("load_context", self._load_context)
        graph.add_node("discover_operations", self._discover_operations)
        graph.add_node("produce_plan", self._produce_plan)
        graph.add_node("invoke_ready_assignments", self._invoke_ready)
        graph.add_node("validate_results", self._validate_results)
        graph.add_node("resolve_approval", self._resolve_approval)
        graph.add_node("compose_response", self._compose_response)
        graph.add_edge(START, "load_context")
        for before, after in zip(self.STAGES, self.STAGES[1:], strict=False):
            graph.add_edge(before, after)
        graph.add_edge("compose_response", END)
        # Without a checkpointer the graph still runs, but nothing survives the
        # process and `interrupt()` has nowhere to pause. Deterministic
        # single-shot use is fine; durable approval flows need the saver.
        if self.checkpointer is not None:
            return graph.compile(checkpointer=self.checkpointer)
        return graph.compile()

    @staticmethod
    def _traced(state: CoordinatorState, stage: str) -> list[str]:
        return [*state.get("stage_trace", []), stage]

    def _load_context(self, state: CoordinatorState) -> dict[str, Any]:
        if not state["objective"].strip() and state.get("operation") is None:
            raise ValidationFailed("A coordinator request needs an objective.")
        return {"stage_trace": self._traced(state, "load_context")}

    def _discover_operations(self, state: CoordinatorState) -> dict[str, Any]:
        operations = self.invoker.registry.enabled_operations()
        descriptions = {name: operation.description
                        for name, operation in operations.items()}
        descriptions.update({name: binding.description
                             for name, binding in self.invoker.handlers.items()})
        explicit = state.get("operation")
        if explicit is not None:
            if explicit not in descriptions:
                raise ValidationFailed(f"Operation {explicit!r} is not enabled.")
            discovered = [explicit]
        else:
            # Narrow to what the roles in play may actually reach *before* the
            # shortlist is disclosed to a model (agent-stack §2). An operation
            # no role can use is not a choice worth offering, and an agent that
            # can see a tool will eventually call it.
            visible = self._role_visible(descriptions)
            terms = set(state["objective"].lower().split())
            ranked = sorted(
                visible.items(),
                key=lambda item: len(terms & set(
                    (item[0] + " " + item[1]).lower().split())),
                reverse=True,
            )
            discovered = [name for name, _ in ranked[:8]]
        return {"discovered": discovered,
                "catalogue": {name: descriptions.get(name, "")
                              for name in discovered},
                "stage_trace": self._traced(state, "discover_operations")}

    def _role_visible(self, descriptions: dict[str, str]) -> dict[str, str]:
        """Every operation some role may use, as the union of role scopes."""
        visible: dict[str, str] = {}
        for role_id in ROLE_IDS:
            visible.update(self.roles.filter_operations(role_id, descriptions))
        return visible

    def _produce_plan(self, state: CoordinatorState) -> dict[str, Any]:
        explicit = state.get("operation")
        if explicit is not None:
            manifest, _ = self.invoker.registry.resolve_operation(explicit) or (None, None)
            if manifest is None and explicit not in self.invoker.handlers:
                raise ValidationFailed(f"Operation {explicit!r} is not enabled.")
            handler = self.invoker.handlers.get(explicit)
            owner_role = (manifest.owner_role if manifest
                          else handler.owner_role if handler else "daily_life")
            payload = {
                "schema_version": 1,
                "intents": [state["objective"]],
                "steps": [{
                    "id": "request",
                    "role": owner_role,
                    "objective": state["objective"], "capability": explicit,
                    "arguments": state["arguments"], "depends_on": [],
                }],
                "response_intent": "answer the owner from persisted outcomes",
            }
        elif self.plan_factory is not None:
            payload = self._produce_plan_payload(state)
        else:
            raise ValidationFailed(
                "No explicit operation or typed plan producer was supplied.")
        context = self._context_for(state)
        plan = parse_plan(payload, root_event_id=context.root_id,
                          known_capabilities=set(state["discovered"]))
        self._live.setdefault(state.get("run_id", ""), {})["plan"] = plan
        # The payload is what gets checkpointed; the parsed plan is rebuilt from
        # it on resume, so a restart cannot depend on an object graph surviving.
        return {"plan_payload": payload,
                "stage_trace": self._traced(state, "produce_plan")}

    def _produce_plan_payload(self, state: CoordinatorState) -> dict[str, Any]:
        """Ask the injected producer for a plan, giving it the real catalogue."""
        factory = self.plan_factory
        assert factory is not None
        produce = getattr(factory, "produce", None)
        if produce is not None:
            return produce(state["objective"], state.get("catalogue", {}),
                           context=self._context_for(state))
        return factory(state["objective"], state["discovered"])

    def _plan_for(self, state: CoordinatorState) -> TypedPlan:
        live = self._live.setdefault(state.get("run_id", ""), {})
        plan = live.get("plan")
        if plan is None:
            context = self._context_for(state)
            plan = parse_plan(state["plan_payload"], root_event_id=context.root_id,
                              known_capabilities=set(state["discovered"]))
            live["plan"] = plan
        return plan

    def _invoke_ready(self, state: CoordinatorState) -> dict[str, Any]:
        """Execute ready assignments, pausing for approval where required.

        This node restarts from the top on resume, so it recomputes rather than
        assuming anything survived. Cancellation and authority are rechecked
        before every effect — a signal that arrived while the run was waiting is
        exactly the one that matters.
        """
        completed: set[str] = set()
        results: dict[str, InvocationResult] = {}
        approved = set(state.get("approved", []))
        plan = self._plan_for(state)
        context = self._context_for(state)
        run_id = state.get("run_id", "")

        while len(completed) < len(plan.steps):
            ready = ready_steps(plan, completed)
            if not ready:
                raise ValidationFailed("The plan made no execution progress.")
            for step in ready:
                arguments = _with_dependency_results(step, results)

                if self._needs_approval(step) and step.capability not in approved:
                    decision = self._await_approval(step, arguments, state)
                    if not decision.get("approved"):
                        raise ApprovalRequired(
                            "The owner declined this operation.",
                            details={"operations": [step.capability]})
                    approved.add(step.capability)

                self._check_still_permitted(step, run_id=run_id,
                                            context=context)
                results[step.id] = self.invoker.invoke(
                    step.capability, arguments, context=context,
                    parent_run_id=run_id or None)
                completed.add(step.id)

        self._live.setdefault(run_id, {})["results"] = results
        return {"result_summaries": {step_id: _summarise(result)
                                     for step_id, result in results.items()},
                "approved": sorted(approved),
                "stage_trace": self._traced(state, "invoke_ready_assignments")}

    def _needs_approval(self, step: PlanStep) -> bool:
        """Both dispatch modes can require approval, so both are consulted."""
        resolved = self.invoker.registry.resolve_operation(step.capability)
        if resolved is not None and resolved[1].needs_approval:
            return True
        handler = self.invoker.handlers.get(step.capability)
        return handler is not None and handler.needs_approval

    def _await_approval(self, step: PlanStep, arguments: dict[str, Any],
                        state: CoordinatorState) -> dict[str, Any]:
        """Persist a bound approval, then pause (agent-stack §4).

        The approval is written *before* `interrupt()` and keyed idempotently,
        so the replay that follows a resume finds the existing request instead
        of asking a second time. Waiting itself consumes no model calls: the
        node simply stops.
        """
        request: dict[str, Any] = {
            "operation": step.capability,
            "step_id": step.id,
            "arguments": arguments,
            "run_id": state.get("run_id", ""),
        }

        if self.approvals is not None:
            operation = self.approvals.propose(
                action=step.capability,
                target=self._context_for(state).root_id,
                payload=arguments,
                idempotency_key=f"{state.get('run_id', '')}:{step.id}",
                authority_ref=self._context_for(state).owner)
            approval = self.approvals.request_approval(
                operation, destination_id="owner:telegram",
                actor_id=self._context_for(state).owner)
            request["operation_id"] = operation.id
            request["approval_id"] = approval.id
            request["input_hash"] = operation.input_hash

        return interrupt(request)

    def _check_still_permitted(self, step: PlanStep, *, run_id: str,
                               context: AuthorityContext) -> None:
        """Recheck cancellation and pack availability before an effect.

        Checked here rather than once at the start, because the interesting case
        is a run that was cancelled or a pack that was disabled *while the run
        was paused*.
        """
        if self.runs is not None and run_id:
            mapping = self.runs.get(run_id)
            if mapping is not None and mapping.status is RunStatus.CANCELLED:
                raise Conflict("This run was cancelled.",
                               details={"run_id": run_id, "step": step.id})

        resolved = self.invoker.registry.resolve_operation(step.capability)
        if resolved is None and step.capability not in self.invoker.handlers:
            raise ValidationFailed(
                f"Operation {step.capability!r} is no longer enabled.",
                details={"step": step.id})

        self._require_role_may_run(step, resolved)

    def _require_role_may_run(self, step: PlanStep, resolved: Any) -> None:
        """Check the step's role may run its capability.

        Two sources of permission, because they answer different questions:

        * A **pack** declares its own `owner_role` and `support_roles` in a
          manifest that was validated at install time. Installing and enabling
          the pack is the authority for its own namespace, so the manifest is
          consulted for pack operations. Requiring a built-in role entry instead
          would mean every new pack edits `ROLE_DEFINITIONS`, and a pack that
          cannot be added without changing core is not an extension (LG02).
        * A **shared trusted port** — `vault.*`, `task.*`, `email.*` — is not the
          pack's to grant, so those go through the role scope table. That is
          where the ceiling actually bites: a plan assigning `vault.write` to a
          read-only role is refused even though the plan parsed.
        """
        if resolved is not None:
            manifest = resolved[0]
            allowed = {manifest.owner_role, *getattr(manifest, "support_roles", [])}
            if step.role not in allowed:
                raise ValidationFailed(
                    f"Role {step.role!r} may not run {step.capability!r}.",
                    details={"step": step.id, "role": step.role,
                             "allowed_roles": sorted(allowed)})
            return

        self.roles.require_scope(step.role, _scope_for(step.capability))

    def _validate_results(self, state: CoordinatorState) -> dict[str, Any]:
        """Review results, repairing and *re-reviewing* until clean or exhausted.

        Two checks, and the order is the point (runtime §1). The deterministic
        validator runs *first and always*: a Reviewer that approves an
        unevidenced claim cannot make it evidenced, and a Reviewer that rejects
        a valid one cannot make it invalid. The review adds judgement about
        whether the result answers the objective; it grants nothing.

        The loop is internal to this node rather than a graph edge back to
        `invoke_ready_assignments`, and it is a loop rather than one repair
        pass, because a single unconditional repair-then-proceed would compose
        a response from a result nobody re-checked — declaring success without
        having verified it, which is the exact "invented success" this stage
        exists to prevent. Each repair is bounded and spends the root budget
        (agent-stack §3).
        """
        plan = self._plan_for(state)
        summaries = dict(state["result_summaries"])
        attempts = int(state.get("repair_attempts", 0))

        while True:
            findings = _deterministic_findings(plan, summaries)
            findings.extend(self._review(state, plan, summaries))

            if not findings:
                return {"result_summaries": summaries,
                        "repair_attempts": attempts, "review_findings": [],
                        "stage_trace": self._traced(state, "validate_results")}

            if attempts >= MAX_REPAIR_ATTEMPTS:
                raise ValidationFailed(
                    "The results did not pass review after the allowed "
                    "repairs.",
                    details={"findings": findings, "attempts": attempts})

            logger.info("Review returned %d finding(s); repair attempt %d",
                        len(findings), attempts + 1)
            summaries = self._repair(state, plan, summaries, findings)
            attempts += 1

    def _review(self, state: CoordinatorState, plan: TypedPlan,
                summaries: dict[str, dict[str, Any]]) -> list[str]:
        """The Reviewer's judgement. Advisory, and never a permission."""
        if self.reviewer is None:
            return []
        try:
            return list(self.reviewer(plan, summaries,
                                      self._context_for(state)))
        except Exception as exc:                       # noqa: BLE001 — reported
            # A broken reviewer must not block work it was only checking.
            logger.warning("Reviewer failed, continuing on deterministic "
                           "checks alone: %s", exc)
            return []

    def _repair(self, state: CoordinatorState, plan: TypedPlan,
                summaries: dict[str, dict[str, Any]],
                findings: list[str]) -> dict[str, dict[str, Any]]:
        """Re-run failed assignments once, against the same budget.

        Takes the *current* summaries rather than reading them back off
        `state`, because after a first repair `state["result_summaries"]` is
        still the pre-repair snapshot — the caller's loop is what carries the
        up-to-date value forward between iterations.
        """
        context = self._context_for(state)
        results: dict[str, InvocationResult] = dict(
            (self._live.get(state.get("run_id", "")) or {}).get("results", {}))
        summaries = dict(summaries)

        for step in plan.steps:
            if step.id in summaries and not _step_failed(step.id, findings):
                continue
            arguments = _with_dependency_results(step, results)
            arguments["review_findings"] = findings
            run_id = state.get("run_id", "")
            self._check_still_permitted(step, run_id=run_id, context=context)
            result = self.invoker.invoke(step.capability, arguments,
                                         context=context,
                                         parent_run_id=run_id or None)
            results[step.id] = result
            summaries[step.id] = _summarise(result)

        self._live.setdefault(state.get("run_id", ""), {})["results"] = results
        return summaries

    def _resolve_approval(self, state: CoordinatorState) -> dict[str, Any]:
        """Record which operations needed approval and got it.

        By the time this stage runs, every approval has already been resolved
        in `invoke_ready_assignments` — an unapproved operation pauses there
        rather than executing and asking afterwards.
        """
        return {"approval_required": list(state.get("approved", [])),
                "stage_trace": self._traced(state, "resolve_approval")}

    def _compose_response(self, state: CoordinatorState) -> dict[str, Any]:
        summaries = state["result_summaries"]
        ordered = [summaries[step.id] for step in self._plan_for(state).steps]
        response = {
            "status": "succeeded",
            "summary": ordered[-1]["output"] if ordered else {},
            "artifact_refs": [item["artifact_id"] for item in ordered
                              if item.get("artifact_id")],
            "evidence_refs": [ref for item in ordered
                              for ref in item.get("evidence", [])],
            "uncertainties": [],
        }
        return {"response": response,
                "stage_trace": self._traced(state, "compose_response")}


def _with_dependency_results(step: PlanStep,
                             results: dict[str, InvocationResult]) -> dict[str, Any]:
    def resolve(value: Any) -> Any:
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, dict):
            return value
        if set(value) == {"$step", "path"}:
            source = results.get(str(value["$step"]))
            if source is None:
                raise ValidationFailed(
                    f"Plan references unfinished step {value['$step']!r}.")
            current: Any = source.output
            for part in filter(None, str(value["path"]).split(".")):
                if not isinstance(current, dict) or part not in current:
                    raise ValidationFailed(
                        f"Plan result path {value['path']!r} does not exist.")
                current = current[part]
            return current
        return {key: resolve(item) for key, item in value.items()}

    return resolve(step.arguments)


def _extract_interrupts(snapshot: Any) -> list[str]:
    pending: list[str] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            value = getattr(item, "value", None)
            if isinstance(value, dict) and value.get("operation"):
                pending.append(str(value["operation"]))
    return pending


def _pending_interrupts(graph: Any, config: dict[str, Any] | None) -> list[str]:
    """Operations the run is currently paused on, if any.

    Read from the graph's own state rather than tracked separately: two records
    of "is it paused" drift, and the checkpointer's answer is the real one.
    """
    if config is None:
        return []
    try:
        snapshot = graph.get_state(config)
    except Exception:                                  # noqa: BLE001 — best effort
        return []
    return _extract_interrupts(snapshot)


async def _apending_interrupts(graph: Any,
                               config: dict[str, Any] | None) -> list[str]:
    """`_pending_interrupts`, through the async graph path.

    An async-only checkpointer (`AsyncSqliteSaver`) has no synchronous
    connection for `graph.get_state` to use, so this awaits `aget_state`
    instead — the same reason `ainvoke` exists alongside `invoke`.
    """
    if config is None:
        return []
    try:
        snapshot = await graph.aget_state(config)
    except Exception:                                  # noqa: BLE001 — best effort
        return []
    return _extract_interrupts(snapshot)


def _summarise(result: InvocationResult) -> dict[str, Any]:
    """The checkpointable form of a result: plain data, no live objects."""
    return {"output": result.output, "evidence": list(result.evidence),
            "artifact_id": result.artifact_id}


def _scope_for(capability: str) -> str:
    """The scope a capability requires, derived rather than declared."""
    from loop.agents.roles import _scope_of

    return _scope_of(capability)


def _deterministic_findings(plan: TypedPlan,
                            summaries: dict[str, dict[str, Any]]) -> list[str]:
    """Checks that run regardless of what a Reviewer concluded (runtime §1)."""
    findings: list[str] = []
    for step in plan.steps:
        summary = summaries.get(step.id)
        if summary is None:
            findings.append(f"step {step.id}: produced no result")
            continue
        if not isinstance(summary.get("output"), dict):
            findings.append(f"step {step.id}: result is not a structured object")
    return findings


def _step_failed(step_id: str, findings: list[str]) -> bool:
    return any(finding.startswith(f"step {step_id}:") for finding in findings)
