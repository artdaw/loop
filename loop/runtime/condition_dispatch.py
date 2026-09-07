"""Condition routines: fire on the edge, and stay fired across restarts (P05).

`observations.py` shipped complete and unreachable. It could record facts with
freshness, evaluate a typed predicate in three-valued logic, and — in
`EdgeState` — fire only on a transition *into* true. Nothing ever called it, so
a routine with `trigger.kind: condition` could be written, validated and
activated while never running at all.

This is the dispatcher that closes that, and the one thing it adds beyond
wiring is the reason it exists as a module rather than three lines in the
composition root: **the edge is durable**.

`EdgeState` is an in-memory dataclass. Held only in memory, a restart forgets
that the condition was already true and already announced — so the next sweep
sees `unknown → true`, calls that an edge, and tells the owner again. The
condition in P05 *"remains true through repeated sensor updates"*, and a
process restart is just another update as far as the owner is concerned. A
notification that repeats every deploy is exactly the failure the edge rule
exists to prevent, and it is invisible in any test that never restarts.

Three-valued logic is carried through rather than flattened: `unknown` rearms
the trigger, because not knowing is not the same as knowing it is false, and a
condition that lapses into unknown and comes back deserves to be announced
again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros
from loop.runtime.jobs import JobQueue
from loop.runtime.routine_dispatch import ROUTINE_KIND
from loop.runtime.routines import Routine, RoutineService
from loop.services.observations import (
    EdgeState,
    ObservationStore,
    Truth,
    evaluate,
)

logger = logging.getLogger(__name__)

#: Trigger kind this dispatcher owns. Clock kinds belong to `RoutineScheduler`.
CONDITION_KIND = "condition"


@dataclass
class ConditionOutcome:
    """What one condition evaluated to, and whether it fired."""

    slug: str
    truth: Truth
    fired: bool = False
    job_id: str | None = None
    missing: list[str] = field(default_factory=list)
    reason: str = ""


class ConditionEdges:
    """Durable edge state, one row per routine.

    Stored rather than held because the whole point of an edge is that it is
    remembered: a restart that forgets it turns "still true" into "newly true".
    """

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS condition_edges ("
                " slug TEXT PRIMARY KEY,"
                " last_truth TEXT NOT NULL,"
                " fired_for_current_true INTEGER NOT NULL,"
                " fired_count INTEGER NOT NULL DEFAULT 0,"
                " updated_at INTEGER NOT NULL)"))
            # An existing database keeps its original columns forever, so the
            # new one is added under a guard rather than assumed present.
            columns = {row[1] for row in session.execute(
                text("PRAGMA table_info(condition_edges)")).all()}
            if "fired_count" not in columns:
                session.execute(text(
                    "ALTER TABLE condition_edges ADD COLUMN fired_count"
                    " INTEGER NOT NULL DEFAULT 0"))
            session.commit()

    def load(self, slug: str) -> EdgeState:
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT last_truth, fired_for_current_true FROM condition_edges"
                " WHERE slug = :slug"), {"slug": slug}).first()
        if row is None:
            return EdgeState()
        return EdgeState(last=Truth(row[0]),
                         fired_for_current_true=bool(row[1]))

    def save(self, slug: str, edge: EdgeState) -> None:
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO condition_edges (slug, last_truth,"
                " fired_for_current_true, updated_at)"
                " VALUES (:slug, :truth, :fired, :now)"
                " ON CONFLICT(slug) DO UPDATE SET"
                " last_truth = excluded.last_truth,"
                " fired_for_current_true = excluded.fired_for_current_true,"
                " updated_at = excluded.updated_at"), {
                    "slug": slug, "truth": edge.last.value,
                    "fired": 1 if edge.fired_for_current_true else 0,
                    "now": to_micros(self._clock.now())})
            session.commit()

    def record_fire(self, slug: str) -> int:
        """Count this edge and return its ordinal.

        The ordinal is the occurrence identity. Deriving it from the clock
        instead would collapse two genuine edges that happen inside the same
        tick into one job — and "the clock always moves between edges" is an
        assumption about the environment, not a property of the design.
        """
        with self._sessions() as session:
            session.execute(text(
                "UPDATE condition_edges SET fired_count = fired_count + 1,"
                " updated_at = :now WHERE slug = :slug"),
                {"slug": slug, "now": to_micros(self._clock.now())})
            row = session.execute(text(
                "SELECT fired_count FROM condition_edges WHERE slug = :slug"),
                {"slug": slug}).first()
            session.commit()
        return int(row[0]) if row else 1

    def rearm(self, slug: str) -> None:
        """Allow the next true to fire — an authorised occurrence boundary."""
        edge = self.load(slug)
        edge.rearm()
        self.save(slug, edge)

    def forget(self, slug: str) -> None:
        with self._sessions() as session:
            session.execute(
                text("DELETE FROM condition_edges WHERE slug = :slug"),
                {"slug": slug})
            session.commit()


class ConditionDispatcher:
    """Evaluates active condition routines and queues work on an edge.

    Deterministic: predicates and observations only, never a model. It runs on
    every sweep, so a model call here would break the idle-service invariant
    the same way one in the trigger callback would.
    """

    def __init__(self, *, routines: RoutineService, observations: ObservationStore,
                 edges: ConditionEdges, jobs: JobQueue,
                 clock: Clock | None = None) -> None:
        self.routines = routines
        self.observations = observations
        self.edges = edges
        self.jobs = jobs
        self._clock = clock or SystemClock()

    def condition_routines(self) -> list[Routine]:
        return [routine for routine in self.routines.list_routines()
                if routine.is_active
                and str(routine.trigger.get("kind")) == CONDITION_KIND]

    def evaluate(self, routine: Routine) -> tuple[Truth, list[str]]:
        """Resolve the routine's predicate against current observations.

        A key whose observation is absent *or stale* is reported missing, so
        the predicate sees `unknown` rather than a stale value presented as
        current — the difference between "the door is shut" and "nobody has
        heard from the sensor since Tuesday".
        """
        predicate = dict(routine.trigger.get("predicate") or {})
        if not predicate:
            return Truth.UNKNOWN, []

        subject = str(routine.trigger.get("subject_ref") or routine.slug)
        facts: dict[str, Any] = {}
        missing: set[str] = set()
        for key in _keys(predicate):
            truth, value = self.observations.resolve(key, subject)
            if truth is Truth.TRUE:
                facts[key] = value
            else:
                missing.add(key)
        return evaluate(predicate, facts, missing=missing), sorted(missing)

    def sweep(self) -> list[ConditionOutcome]:
        """Evaluate every active condition routine once."""
        outcomes: list[ConditionOutcome] = []
        for routine in self.condition_routines():
            truth, missing = self.evaluate(routine)
            edge = self.edges.load(routine.slug)
            fired = edge.should_fire(truth)
            self.edges.save(routine.slug, edge)

            outcome = ConditionOutcome(slug=routine.slug, truth=truth,
                                       missing=missing)
            if not fired:
                outcome.reason = (
                    "condition is not true" if truth is not Truth.TRUE
                    else "already announced for this occurrence")
                outcomes.append(outcome)
                continue

            occurrence = f"edge:{self.edges.record_fire(routine.slug)}"
            outcome.fired = True
            outcome.job_id = self.jobs.enqueue(
                ROUTINE_KIND,
                dedupe_key=f"{ROUTINE_KIND}:{routine.slug}:{occurrence}",
                payload={"slug": routine.slug, "occurrence_key": occurrence,
                         "catch_up": "fire", "trigger_id": ""})
            outcomes.append(outcome)
        return outcomes


def _keys(predicate: dict[str, Any]) -> set[str]:
    """Every observation key a predicate reads."""
    found: set[str] = set()
    for group in ("all", "any"):
        for clause in predicate.get(group) or []:
            found |= _keys(clause)
    if "not" in predicate:
        found |= _keys(predicate["not"])
    fact = predicate.get("fact")
    if isinstance(fact, str):
        found.add(fact)
    return found
