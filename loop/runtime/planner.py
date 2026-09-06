"""Typed plans, work items and dependency validation (runtime §5, agent-stack §2).

A plan produced by a model is a **proposal**, and this module is where it stops
being trusted. Every field is validated before anything runs: roles and
capabilities must exist, references must resolve, the dependency graph must be
acyclic, and unknown fields are a rejection rather than something to ignore.

The rejection of *extra* fields matters more than it looks. A model that emits
`{"role": "scribe", "authority": "owner", ...}` is not attacking anything — it
is pattern-matching on plausible JSON. But an executor that quietly accepts the
field has handed plan authorship the ability to grant authority. So unknown keys
fail loudly (A02).

**Effect slots** are the other half. The Coordinator allocates a stable slot per
intended mutation, keyed by root event + intent index + action + subject. Two
specialists proposing the same slot share one operation ID, so the effect happens
once (A04). Similar wording from *unrelated* requests is not evidence of the same
intent and must not merge — which is why the key includes the root event, not
just the text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from loop.agents.roles import ROLE_IDS
from loop.core.errors import InvalidInput, ValidationFailed
from loop.core.ids import content_hash, new_id

logger = logging.getLogger(__name__)

#: Registry role IDs (runtime §1). A plan naming anything else is invalid.
#: Imported rather than restated: three copies of the list drift, and the one
#: that drifts is whichever validator gets edited least.
ROLES = ROLE_IDS

#: Fields a plan step may carry. Anything else is rejected.
ALLOWED_STEP_FIELDS = frozenset({
    "id", "role", "objective", "capability", "arguments", "input_refs",
    "depends_on", "expected_result", "estimated_cost",
})

#: Bounded autonomy limits (runtime §2).
MAX_CHILD_ASSIGNMENTS = 4
MAX_DEPENDENCY_DEPTH = 4
MAX_CAUSAL_HOPS = 8


@dataclass
class PlanStep:
    """One assignment inside a plan."""

    id: str
    role: str
    objective: str
    capability: str
    arguments: dict[str, Any] = field(default_factory=dict)
    input_refs: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    expected_result: str = ""
    estimated_cost: int = 0


@dataclass
class TypedPlan:
    """A validated plan. Constructing one does not execute anything."""

    schema_version: int
    intents: list[str]
    steps: list[PlanStep]
    response_intent: str = ""
    needs_input: list[str] = field(default_factory=list)
    root_event_id: str = ""

    @property
    def step_ids(self) -> set[str]:
        return {step.id for step in self.steps}


def parse_plan(payload: dict[str, Any], *, root_event_id: str,
               known_capabilities: set[str]) -> TypedPlan:
    """Validate a model-produced plan, or raise.

    Nothing here trusts the payload: IDs, privacy, authority, deadlines and
    state are assigned by the executor afterwards, never read from the model
    (runtime §5).
    """
    problems: list[str] = []

    if not isinstance(payload, dict):
        raise ValidationFailed("A plan must be an object.")
    if payload.get("schema_version") != 1:
        problems.append("schema_version must be 1")

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        raise ValidationFailed("A plan requires a steps array.",
                               details={"problems": problems + ["steps missing"]})

    steps: list[PlanStep] = []
    seen_ids: set[str] = set()

    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            problems.append(f"step {index} is not an object")
            continue

        unknown = set(raw) - ALLOWED_STEP_FIELDS
        if unknown:
            # A model supplying `authority` or `approved` is the case this
            # guards: accepting the field would let plan authorship grant
            # permissions the executor is supposed to own.
            problems.append(
                f"step {raw.get('id', index)}: unknown fields {sorted(unknown)}")
            continue

        step_id = str(raw.get("id") or "")
        if not step_id:
            problems.append(f"step {index} has no id")
            continue
        if step_id in seen_ids:
            problems.append(f"duplicate step id {step_id!r}")
            continue
        seen_ids.add(step_id)

        role = str(raw.get("role") or "")
        if role not in ROLES:
            problems.append(f"step {step_id}: unknown role {role!r}")

        capability = str(raw.get("capability") or "")
        if capability not in known_capabilities:
            # A fabricated tool name must fail before anything is dispatched.
            problems.append(f"step {step_id}: unknown capability {capability!r}")

        steps.append(PlanStep(
            id=step_id, role=role, objective=str(raw.get("objective") or ""),
            capability=capability,
            arguments=dict(raw.get("arguments") or {}),
            input_refs=[str(r) for r in (raw.get("input_refs") or [])],
            depends_on=[str(d) for d in (raw.get("depends_on") or [])],
            expected_result=str(raw.get("expected_result") or ""),
            estimated_cost=int(raw.get("estimated_cost") or 0)))

    if len(steps) > MAX_CHILD_ASSIGNMENTS:
        problems.append(
            f"{len(steps)} steps exceeds the limit of {MAX_CHILD_ASSIGNMENTS}")

    # Dependencies must resolve *within* the plan.
    for step in steps:
        for dependency in step.depends_on:
            if dependency not in seen_ids:
                problems.append(
                    f"step {step.id}: depends_on unknown step {dependency!r}")

    if problems:
        raise ValidationFailed("Plan rejected before execution.",
                               details={"problems": problems})

    plan = TypedPlan(
        schema_version=1,
        intents=[str(i) for i in (payload.get("intents") or [])],
        steps=steps,
        response_intent=str(payload.get("response_intent") or ""),
        needs_input=[str(n) for n in (payload.get("needs_input") or [])],
        root_event_id=root_event_id)

    validate_dag(plan)
    return plan


def validate_dag(plan: TypedPlan) -> None:
    """Reject cycles and over-deep dependency chains (A02, A03)."""
    graph = {step.id: list(step.depends_on) for step in plan.steps}

    #: 0 = unvisited, 1 = on the current path, 2 = finished.
    state: dict[str, int] = dict.fromkeys(graph, 0)
    cycle: list[str] = []

    def visit(node: str, path: list[str]) -> bool:
        if state[node] == 1:
            cycle.extend([*path, node])
            return True
        if state[node] == 2:
            return False
        state[node] = 1
        for dependency in graph.get(node, []):
            if visit(dependency, [*path, node]):
                return True
        state[node] = 2
        return False

    for step_id in graph:
        if state[step_id] == 0 and visit(step_id, []):
            raise ValidationFailed(
                "Plan dependency graph contains a cycle.",
                details={"cycle": cycle})

    # Within a single plan the step-count limit usually binds first (depth can
    # never exceed the number of steps). This check exists for chains assembled
    # across *nested* plans, where a step spawns a child plan of its own, and is
    # exercised directly by validate_dag tests.
    depth = max((_depth(step_id, graph, set()) for step_id in graph), default=0)
    if depth > MAX_DEPENDENCY_DEPTH:
        raise ValidationFailed(
            f"Dependency depth {depth} exceeds the limit of "
            f"{MAX_DEPENDENCY_DEPTH}.", details={"depth": depth})


def _depth(node: str, graph: dict[str, list[str]], seen: set[str]) -> int:
    if node in seen:
        return 0
    seen = seen | {node}
    dependencies = graph.get(node, [])
    if not dependencies:
        return 1
    return 1 + max(_depth(d, graph, seen) for d in dependencies)


def ready_steps(plan: TypedPlan, completed: set[str]) -> list[PlanStep]:
    """Steps whose dependencies are all satisfied.

    Independent steps come back together, which is what makes bounded parallel
    evidence gathering possible (A01).
    """
    return [step for step in plan.steps
            if step.id not in completed
            and all(d in completed for d in step.depends_on)]


# --------------------------------------------------------------------------- #
# Effect slots (A04)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EffectSlot:
    """A stable identity for one intended mutation."""

    root_event_id: str
    intent_index: int
    action: str
    subject: str

    @property
    def key(self) -> str:
        return content_hash(
            f"{self.root_event_id}|{self.intent_index}|{self.action}|{self.subject}"
        )[:32]


class EffectSlotAllocator:
    """Ensures two proposals for the same slot share one operation.

    Keyed by the root event, so two *unrelated* requests that happen to be
    worded similarly get different slots. Merging them would silently drop one
    of the user's actual requests.
    """

    def __init__(self) -> None:
        self._slots: dict[str, str] = {}          # slot key -> operation id
        self._payloads: dict[str, str] = {}       # slot key -> input hash

    def allocate(self, slot: EffectSlot, *, input_hash: str) -> str:
        """Return the operation ID for this slot, creating it if new.

        A second proposal with a *different* payload for the same slot is a
        conflict: two specialists disagreeing about what the effect should be is
        not something to resolve by picking one.
        """
        existing = self._slots.get(slot.key)
        if existing is None:
            operation_id = new_id()
            self._slots[slot.key] = operation_id
            self._payloads[slot.key] = input_hash
            return operation_id

        if self._payloads[slot.key] != input_hash:
            raise InvalidInput(
                "Two different payloads were proposed for the same effect slot.",
                details={"slot": slot.key,
                         "action": slot.action, "subject": slot.subject})
        return existing

    def operation_for(self, slot: EffectSlot) -> str | None:
        return self._slots.get(slot.key)

    @property
    def allocated(self) -> int:
        return len(self._slots)
