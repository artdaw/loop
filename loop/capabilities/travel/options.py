"""Itinerary options: feasibility, ranking and refusal (travel §5).

Ranking is deterministic and its order is fixed by the spec, because "which trip
is better" is a question a model will answer enthusiastically and differently
each time. Feasible options come first — an infeasible plan is not a cheaper
alternative, it is not a plan — then the user's stated priorities in order.

When nothing is feasible, the answer is `no_feasible_plan` *with the conflicting
constraints named and concrete relaxations offered*. Silently dropping the
budget to produce three options would answer a question nobody asked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from loop.capabilities.travel.brief import MAX_OPTIONS, TripBrief
from loop.capabilities.travel.money import BudgetFit, CostTotal
from loop.capabilities.travel.schedule import ScheduleFinding, Segment

logger = logging.getLogger(__name__)


class Feasibility(str, Enum):
    FEASIBLE = "feasible"
    TENTATIVE = "tentative"
    INFEASIBLE = "infeasible"

    @property
    def rank(self) -> int:
        return {"feasible": 0, "tentative": 1, "infeasible": 2}[self.value]


class ResultStatus(str, Enum):
    READY = "ready"
    PARTIAL = "partial"
    NO_FEASIBLE_PLAN = "no_feasible_plan"
    NEEDS_INPUT = "needs_input"

    @property
    def claims_a_plan(self) -> bool:
        return self is ResultStatus.READY


@dataclass
class ConstraintCheck:
    name: str
    satisfied: bool | None          # None means unknown, not satisfied
    detail: str = ""

    @property
    def is_violation(self) -> bool:
        return self.satisfied is False

    @property
    def is_unknown(self) -> bool:
        return self.satisfied is None


@dataclass
class Option:
    """One candidate itinerary."""

    id: str
    summary: str
    segments: list[Segment] = field(default_factory=list)
    costs: CostTotal | None = None
    budget_fit: BudgetFit = BudgetFit.TENTATIVE
    constraint_checks: list[ConstraintCheck] = field(default_factory=list)
    findings: list[ScheduleFinding] = field(default_factory=list)
    matched_interests: dict[str, str] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    why_recommended: str = ""

    @property
    def feasibility(self) -> Feasibility:
        """Known violations exclude; unknowns make it tentative (travel §5)."""
        if any(check.is_violation for check in self.constraint_checks):
            return Feasibility.INFEASIBLE
        if any(finding.is_violation for finding in self.findings):
            return Feasibility.INFEASIBLE
        if self.budget_fit is BudgetFit.OVER:
            return Feasibility.INFEASIBLE
        if (any(check.is_unknown for check in self.constraint_checks)
                or self.unresolved
                or any(not f.is_violation for f in self.findings)
                or self.budget_fit in (BudgetFit.TENTATIVE,
                                       BudgetFit.UNCOMPARABLE)):
            return Feasibility.TENTATIVE
        return Feasibility.FEASIBLE

    @property
    def travel_burden_minutes(self) -> int:
        from loop.capabilities.travel.schedule import SegmentType

        return sum(s.duration_minutes for s in self.segments
                   if s.type is SegmentType.TRANSPORT)

    @property
    def cost_upper_minor(self) -> int:
        if self.costs is None or not self.costs.by_currency:
            return 0
        return sum(high for _, high in self.costs.by_currency.values())

    def interest_coverage(self, brief: TripBrief) -> float | None:
        """Matched weight / requested weight. None when none were requested.

        A trip with no stated interests should skip this criterion rather than
        score zero on it — scoring zero would rank every option identically and
        pretend that meant something.
        """
        if not brief.interests:
            return None
        total = sum(brief.interests.values())
        if total <= 0:
            return None
        matched = sum(weight for tag, weight in brief.interests.items()
                      if tag in self.matched_interests)
        return matched / total

    def pace_violations(self) -> int:
        return sum(1 for finding in self.findings if finding.code == "pace")

    def confirmed_bookings(self) -> list[Segment]:
        return [s for s in self.segments if s.is_confirmed_booking]


DEFAULT_PRIORITIES = ("interest_coverage", "pace_fit", "travel_burden", "cost")


def rank_options(options: list[Option], brief: TripBrief, *,
                 priorities: tuple[str, ...] = ()) -> list[Option]:
    """Order options deterministically (travel §5).

    Feasible before tentative before infeasible, then the user's ordered soft
    priorities, then fewer unresolved facts, then the option ID. The final tie
    break on ID exists so the same inputs always produce the same order.
    """
    order = priorities or brief.priorities or DEFAULT_PRIORITIES

    def criterion(option: Option, name: str) -> float:
        if name == "interest_coverage":
            coverage = option.interest_coverage(brief)
            return 0.0 if coverage is None else -coverage   # higher is better
        if name == "pace_fit":
            return float(option.pace_violations())
        if name == "travel_burden":
            return float(option.travel_burden_minutes)
        if name == "cost":
            return float(option.cost_upper_minor)
        return 0.0

    def key(option: Option) -> tuple:
        return (option.feasibility.rank,
                *(criterion(option, name) for name in order),
                len(option.unresolved),
                option.id)

    return sorted(options, key=key)


@dataclass
class Relaxation:
    """A concrete change that would make some option feasible (TR04)."""

    constraint: str
    proposal: str


@dataclass
class PlanResult:
    trip_id: str
    revision_id: str
    brief_version: int
    generated_at: int
    status: ResultStatus
    options: list[Option] = field(default_factory=list)
    recommended_option_id: str = ""
    missing_information: list[str] = field(default_factory=list)
    relaxations: list[Relaxation] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)

    @property
    def feasible_options(self) -> list[Option]:
        return [o for o in self.options
                if o.feasibility is not Feasibility.INFEASIBLE]


def build_result(options: list[Option], brief: TripBrief, *, trip_id: str,
                 revision_id: str, generated_at: int,
                 missing: list[str] | None = None,
                 relaxations: list[Relaxation] | None = None,
                 max_options: int = MAX_OPTIONS) -> PlanResult:
    """Assemble a result, refusing rather than padding (travel §5)."""
    blocking = list(missing or [])
    ranked = rank_options(options, brief)
    presentable = [o for o in ranked
                   if o.feasibility is not Feasibility.INFEASIBLE][:max_options]

    result = PlanResult(
        trip_id=trip_id, revision_id=revision_id, brief_version=brief.version,
        generated_at=generated_at, status=ResultStatus.NEEDS_INPUT,
        options=presentable, missing_information=blocking,
        relaxations=list(relaxations or []))

    if blocking:
        result.status = ResultStatus.NEEDS_INPUT
        return result

    if not presentable:
        result.status = ResultStatus.NO_FEASIBLE_PLAN
        result.relaxations = list(relaxations or []) or _derive_relaxations(ranked)
        return result

    result.recommended_option_id = presentable[0].id
    presentable[0].why_recommended = _explain(presentable[0], brief)

    if any(o.feasibility is Feasibility.TENTATIVE for o in presentable):
        result.status = ResultStatus.PARTIAL
    else:
        result.status = ResultStatus.READY
    return result


def _derive_relaxations(options: list[Option]) -> list[Relaxation]:
    """Name what actually blocked things, from the recorded checks."""
    seen: dict[str, Relaxation] = {}
    for option in options:
        for check in option.constraint_checks:
            if check.is_violation and check.name not in seen:
                seen[check.name] = Relaxation(
                    check.name, f"relax or change: {check.detail}")
        if option.budget_fit is BudgetFit.OVER and "budget" not in seen:
            seen["budget"] = Relaxation(
                "budget", "raise the budget, shorten the trip, or change dates")
    return [seen[name] for name in sorted(seen)]


def _explain(option: Option, brief: TripBrief) -> str:
    parts = [f"feasibility: {option.feasibility.value}"]
    coverage = option.interest_coverage(brief)
    if coverage is not None:
        parts.append(f"matches {coverage:.0%} of your stated interests")
    parts.append(f"{option.travel_burden_minutes} minutes of travel")
    if option.costs is not None and option.costs.by_currency:
        parts.append("; ".join(option.costs.describe()))
    return ", ".join(parts)
