"""Scoped trip monitoring: checkpoints become triggers, and stay scoped.

`travel/monitor.py` was the same shape as every other gap this milestone
closed — complete rules with no binding. `plan_monitoring` produced a bounded
checkpoint set, `NoticeLedger` deduplicated notices per material revision, and
`resolve_checks` named the checks it could not do. Nothing turned any of it
into work that would actually happen.

*Scoped* is the operative word in the milestone's exit criteria, and it means
four separate things here, each of which this module is responsible for:

1. **Scoped to the checks the owner asked for.** A request to watch for
   closures does not silently become price monitoring. The resolved check set
   is stored with the activation and is what the dispatcher passes on.
2. **Scoped to a selected option.** Monitoring a trip with no selection has
   nothing to compare against; `plan_monitoring` already refuses, and this
   refuses to activate a plan naming checks it cannot perform (TR23) rather
   than activating a partial one silently.
3. **Scoped in time.** Only future checkpoints become triggers — activating
   three days before departure must not fire the seven-days-before check
   immediately — and every trigger is one-shot, so monitoring ends by running
   out rather than by anyone remembering to stop it.
4. **Scoped by cancellation.** Cancelling a trip disables its remaining
   triggers *and* clears its pending notices (TR19). A cancelled trip that
   still messages about a delayed train is the failure this prevents.

The trigger row is the durable part. Its subject type is distinct from a
routine's, so `RoutineDispatcher` and this dispatcher can share one sweep
without either seeing the other's work.

`Trip.monitoring_routine_id` is deliberately **not** written from here.
`trip_monitoring` is the authority on what is being watched and with which
scope; a second copy of that fact on the trip row could disagree with it, and
the failure mode of disagreeing monitoring state is a trip that either reports
itself watched while nothing is scheduled, or the reverse.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from loop.capabilities.travel.monitor import Check, MonitorPlan, NoticeLedger
from loop.core.clock import Clock, SystemClock, from_micros, to_micros
from loop.core.errors import InvalidInput, Unavailable
from loop.runtime.jobs import JobQueue
from loop.runtime.triggers import Trigger, TriggerService

logger = logging.getLogger(__name__)

#: Distinct from `routine`, so one sweep serves both without cross-talk.
TRIP_SUBJECT = "trip_check"
TRIP_JOB_KIND = "trip.check"


@dataclass
class MonitoringState:
    """What the owner activated, and what it is allowed to watch."""

    trip_id: str
    revision_id: str
    option_id: str
    checks: tuple[Check, ...]
    activation_event_id: str
    active: bool = True
    assumptions: tuple[str, ...] = ()

    @property
    def check_names(self) -> list[str]:
        return [check.value for check in self.checks]


@dataclass
class ActivationReport:
    """The outcome of activating monitoring, including what it will not do."""

    trip_id: str
    scheduled: int = 0
    checks: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    next_check: str = ""
    skipped_past: int = 0

    @property
    def monitoring(self) -> bool:
        return self.scheduled > 0


class TripMonitorScheduler:
    """Turns an approved monitoring plan into durable, scoped triggers."""

    def __init__(self, *, triggers: TriggerService,
                 sessions: sessionmaker[Session],
                 notices: NoticeLedger | None = None,
                 clock: Clock | None = None) -> None:
        self.triggers = triggers
        self.notices = notices or NoticeLedger()
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS trip_monitoring ("
                " trip_id TEXT PRIMARY KEY,"
                " revision_id TEXT NOT NULL,"
                " option_id TEXT NOT NULL,"
                " checks_json TEXT NOT NULL,"
                " assumptions_json TEXT NOT NULL DEFAULT '[]',"
                " activation_event_id TEXT NOT NULL,"
                " active INTEGER NOT NULL DEFAULT 1,"
                " updated_at INTEGER NOT NULL)"))
            session.commit()

    # ------------------------------------------------------------------ #
    # Activation
    # ------------------------------------------------------------------ #
    def activate(self, plan: MonitorPlan, *, activation_event_id: str
                 ) -> ActivationReport:
        """Schedule a plan's checkpoints. Activation is authority (TR15).

        A plan naming checks that cannot be performed is refused rather than
        activated for the subset: the owner asked to be told about something,
        and monitoring that silently drops one of them looks identical to
        monitoring that found nothing.
        """
        if not activation_event_id:
            raise InvalidInput(
                "Monitoring must name the request that authorised it.")
        if plan.unsupported:
            raise Unavailable(
                "These checks cannot be performed, so monitoring was not "
                f"started: {', '.join(plan.unsupported)}",
                details={"unsupported": list(plan.unsupported)})
        if not plan.checkpoints:
            raise InvalidInput(
                "Every checkpoint for this trip is already in the past.",
                details={"trip_id": plan.trip_id})

        state = MonitoringState(
            trip_id=plan.trip_id, revision_id=plan.revision_id,
            option_id=plan.option_id, checks=plan.checks,
            activation_event_id=activation_event_id,
            assumptions=plan.assumptions)
        self._store(state)

        now = self._clock.now()
        scheduled = 0
        skipped = 0
        for checkpoint in plan.checkpoints:
            if checkpoint.at_utc <= now:
                # `build_checkpoints` already filters against its own `now`;
                # this catches the gap between planning and approving, which
                # can be long enough for a checkpoint to pass.
                skipped += 1
                continue
            self.triggers.create_at(
                subject_type=TRIP_SUBJECT, subject_id=plan.trip_id,
                instant=checkpoint.at_utc,
                timezone=checkpoint.timezone or "UTC",
                original_local=checkpoint.reason,
                # A missed check is worth running late — a closure found the
                # morning after is still worth knowing — so these catch up.
                catch_up=True)
            scheduled += 1

        upcoming = [c for c in plan.checkpoints if c.at_utc > now]
        return ActivationReport(
            trip_id=plan.trip_id, scheduled=scheduled,
            checks=tuple(state.check_names), assumptions=plan.assumptions,
            next_check=upcoming[0].at_utc.isoformat() if upcoming else "",
            skipped_past=skipped)

    def deactivate(self, trip_id: str) -> int:
        """Stop monitoring and drop what it was about to say (TR19)."""
        disabled = 0
        for trigger in self.triggers.for_subject(TRIP_SUBJECT, trip_id):
            if self.triggers.disable(trigger.id):
                disabled += 1
        self.notices.clear_trip(trip_id)

        with self._sessions() as session:
            session.execute(text(
                "UPDATE trip_monitoring SET active = 0, updated_at = :now "
                "WHERE trip_id = :trip"),
                {"now": to_micros(self._clock.now()), "trip": trip_id})
            session.commit()
        return disabled

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def state_for(self, trip_id: str) -> MonitoringState | None:
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT trip_id, revision_id, option_id, checks_json,"
                " assumptions_json, activation_event_id, active"
                " FROM trip_monitoring WHERE trip_id = :trip"),
                {"trip": trip_id}).first()
        if row is None:
            return None
        return MonitoringState(
            trip_id=row[0], revision_id=row[1], option_id=row[2],
            checks=tuple(Check(name) for name in json.loads(row[3])),
            assumptions=tuple(json.loads(row[4])),
            activation_event_id=row[5], active=bool(row[6]))

    def pending_checks(self, trip_id: str) -> list[Trigger]:
        return self.triggers.for_subject(TRIP_SUBJECT, trip_id)

    def _store(self, state: MonitoringState) -> None:
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO trip_monitoring (trip_id, revision_id, option_id,"
                " checks_json, assumptions_json, activation_event_id, active,"
                " updated_at) VALUES (:trip, :revision, :option, :checks,"
                " :assumptions, :event, 1, :now)"
                " ON CONFLICT(trip_id) DO UPDATE SET"
                " revision_id = excluded.revision_id,"
                " option_id = excluded.option_id,"
                " checks_json = excluded.checks_json,"
                " assumptions_json = excluded.assumptions_json,"
                " activation_event_id = excluded.activation_event_id,"
                " active = 1, updated_at = excluded.updated_at"), {
                    "trip": state.trip_id, "revision": state.revision_id,
                    "option": state.option_id,
                    "checks": json.dumps(state.check_names),
                    "assumptions": json.dumps(list(state.assumptions)),
                    "event": state.activation_event_id,
                    "now": to_micros(self._clock.now())})
            session.commit()


@dataclass
class TripCheckDispatcher:
    """`on_trigger` for trip checkpoints: queue durable work, call no model."""

    jobs: JobQueue
    scheduler: TripMonitorScheduler

    def __call__(self, trigger: Any, decision: str,
                 occurrence_key: str,
                 session: Session | None = None) -> str | None:
        # Redundant for *correctness* with the `state_for` lookup below — a
        # routine's subject id has no monitoring row, so it would return None
        # anyway — and kept for cost: this runs for every fired trigger on
        # every sweep, and a string comparison is cheaper than a query. Stated
        # plainly because a mutation removing it is not caught by any test,
        # and a reader should know that is expected rather than a gap.
        if getattr(trigger, "subject_type", "") != TRIP_SUBJECT:
            return None
        trip_id = str(trigger.subject_id)
        state = self.scheduler.state_for(trip_id)
        if state is None or not state.active:
            logger.info("Skipping trip check for unmonitored trip %s", trip_id)
            return None

        return self.jobs.enqueue(
            TRIP_JOB_KIND,
            dedupe_key=f"{TRIP_JOB_KIND}:{trip_id}:{occurrence_key}",
            payload={
                "trip_id": trip_id,
                "revision_id": state.revision_id,
                "option_id": state.option_id,
                # The scope travels with the job. A worker that re-derived the
                # check list could widen it; this cannot.
                "checks": state.check_names,
                "reason": str(trigger.definition.get("original_local") or ""),
                "occurrence_key": occurrence_key,
                "catch_up": decision,
            }, session=session)


@dataclass
class CompositeTriggerDispatcher:
    """Routes a fired trigger to whichever dispatcher owns its subject type.

    `LoopService` takes one callback, and there are now two kinds of scheduled
    subject. Each dispatcher already ignores subjects that are not its own, so
    this simply offers the trigger to each in turn — which keeps the routing
    rule in the dispatchers rather than duplicating a subject-type table here.
    """

    dispatchers: list[Any] = field(default_factory=list)

    def __call__(self, trigger: Any, decision: str,
                 occurrence_key: str,
                 session: Session | None = None) -> list[str]:
        queued: list[str] = []
        for dispatcher in self.dispatchers:
            result = dispatcher(trigger, decision, occurrence_key, session)
            if result:
                queued.append(str(result))
        return queued


def next_check_at(triggers: list[Trigger]) -> str:
    """The earliest pending check, for status output."""
    upcoming = [t.next_fire_at for t in triggers if t.next_fire_at is not None]
    return from_micros(min(upcoming)).isoformat() if upcoming else ""
