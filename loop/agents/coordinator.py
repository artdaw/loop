"""Stable, domain-neutral LangGraph coordinator.

Every request crosses the same stages.  Capability metadata and typed plans
decide what runs; the graph has no edges for individual domains or pack names.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from loop.capabilities.runners import CapabilityInvoker, InvocationResult
from loop.core.errors import ApprovalRequired, ValidationFailed
from loop.runtime.authority import AuthorityContext
from loop.runtime.planner import PlanStep, TypedPlan, parse_plan, ready_steps


class CoordinatorState(TypedDict, total=False):
    objective: str
    operation: str | None
    arguments: dict[str, Any]
    context: AuthorityContext
    discovered: list[str]
    plan: TypedPlan
    results: dict[str, InvocationResult]
    approval_required: list[str]
    response: dict[str, Any]
    stage_trace: list[str]


@dataclass
class CoordinatorResult:
    """Grounded response and the results from which it was composed."""

    response: dict[str, Any]
    results: dict[str, InvocationResult]
    stage_trace: list[str] = field(default_factory=list)


PlanFactory = Callable[[str, list[str]], dict[str, Any]]


class Coordinator:
    """Run explicit operations or injected typed planning through one graph."""

    STAGES = (
        "load_context", "discover_operations", "produce_plan",
        "invoke_ready_assignments", "validate_results", "resolve_approval",
        "compose_response",
    )

    def __init__(self, *, invoker: CapabilityInvoker,
                 plan_factory: PlanFactory | None = None) -> None:
        self.invoker = invoker
        self.plan_factory = plan_factory
        self.graph = self._compile()

    def invoke(self, *, objective: str, context: AuthorityContext,
               operation: str | None = None,
               arguments: dict[str, Any] | None = None) -> CoordinatorResult:
        final = self.graph.invoke({
            "objective": objective,
            "operation": operation,
            "arguments": arguments or {},
            "context": context,
            "stage_trace": [],
        })
        return CoordinatorResult(response=final["response"],
                                 results=final["results"],
                                 stage_trace=final["stage_trace"])

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
            terms = set(state["objective"].lower().split())
            ranked = sorted(
                descriptions.items(),
                key=lambda item: len(terms & set(
                    (item[0] + " " + item[1]).lower().split())),
                reverse=True,
            )
            discovered = [name for name, _ in ranked[:8]]
        return {"discovered": discovered,
                "stage_trace": self._traced(state, "discover_operations")}

    def _produce_plan(self, state: CoordinatorState) -> dict[str, Any]:
        explicit = state.get("operation")
        if explicit is not None:
            manifest, _ = self.invoker.registry.resolve_operation(explicit) or (None, None)
            if manifest is None and explicit not in self.invoker.handlers:
                raise ValidationFailed(f"Operation {explicit!r} is not enabled.")
            payload = {
                "schema_version": 1,
                "intents": [state["objective"]],
                "steps": [{
                    "id": "request",
                    "role": manifest.owner_role if manifest else "daily_life",
                    "objective": state["objective"], "capability": explicit,
                    "arguments": state["arguments"], "depends_on": [],
                }],
                "response_intent": "answer the owner from persisted outcomes",
            }
        elif self.plan_factory is not None:
            payload = self.plan_factory(state["objective"], state["discovered"])
        else:
            raise ValidationFailed(
                "No explicit operation or typed plan producer was supplied.")
        plan = parse_plan(payload, root_event_id=state["context"].root_id,
                          known_capabilities=set(state["discovered"]))
        return {"plan": plan,
                "stage_trace": self._traced(state, "produce_plan")}

    def _invoke_ready(self, state: CoordinatorState) -> dict[str, Any]:
        completed: set[str] = set()
        results: dict[str, InvocationResult] = {}
        plan = state["plan"]
        while len(completed) < len(plan.steps):
            ready = ready_steps(plan, completed)
            if not ready:
                raise ValidationFailed("The plan made no execution progress.")
            for step in ready:
                resolved = self.invoker.registry.resolve_operation(step.capability)
                if resolved is not None and resolved[1].needs_approval:
                    raise ApprovalRequired(
                        "This operation requires bound approval before execution.",
                        details={"operations": [step.capability]})
                arguments = _with_dependency_results(step, results)
                results[step.id] = self.invoker.invoke(
                    step.capability, arguments, context=state["context"])
                completed.add(step.id)
        return {"results": results,
                "stage_trace": self._traced(state, "invoke_ready_assignments")}

    def _validate_results(self, state: CoordinatorState) -> dict[str, Any]:
        if len(state["results"]) != len(state["plan"].steps):
            raise ValidationFailed("Not every planned assignment produced a result.")
        return {"stage_trace": self._traced(state, "validate_results")}

    def _resolve_approval(self, state: CoordinatorState) -> dict[str, Any]:
        return {"approval_required": [],
                "stage_trace": self._traced(state, "resolve_approval")}

    def _compose_response(self, state: CoordinatorState) -> dict[str, Any]:
        ordered = [state["results"][step.id] for step in state["plan"].steps]
        response = {
            "status": "succeeded",
            "summary": ordered[-1].output if ordered else {},
            "artifact_refs": [result.artifact_id for result in ordered
                              if result.artifact_id],
            "evidence_refs": [item for result in ordered
                              for item in result.evidence],
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
