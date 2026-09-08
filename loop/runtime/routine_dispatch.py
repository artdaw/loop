"""Activated routine → schedule → durable job → advice → outbox.

Every piece of this path already existed and none of them were connected.
`RoutineService` recorded that the owner activated a routine, `TriggerService`
knew when a local wall-clock schedule was next due, `LoopService.tick()` fired
whatever was due, `CapabilityInvoker` ran capabilities, `NotificationManager`
ruled on whether a message may be sent now, and `NotificationOutbox` delivered
exactly once. The composition root's trigger callback was, literally,
``del trigger, decision`` — so a routine could be activated, its trigger could
fire on time, and no work was ever produced by any of it.

Three seams, deliberately separate:

1. `RoutineScheduler` — activation writes the trigger row. Activation *is*
   authority (P01), and a schedule that only exists in memory would leave a
   restarted process with a routine the owner approved and nothing that ever
   runs it. Pausing disables the trigger rather than deleting it, so the
   firing history that proves which occurrences already ran survives.
2. `RoutineDispatcher` — the `on_trigger` callback. **Deterministic**: it
   enqueues a durable job and returns. The sweep runs on a timer whether or
   not anything is due, so a model call here would break the idle-service
   invariant (D16/D17, I6) for every tick of every day.
3. `RoutineJobWorker` — claims that job and actually runs the routine's steps
   through the same `CapabilityInvoker` every other caller uses, applies
   notification policy to the result, and enqueues the message. Separate from
   `LoopService` for the same reason `CoordinatorJobWorker` is: a process with
   no model configured must still sweep.

**The job is the restart boundary, and that is the point.** A trigger that
fired in one process leaves a row in `jobs`; a completely different process,
started later against the same database, claims that row, runs the routine and
delivers the result. Nothing in memory bridges the two.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy.orm import Session

from loop.ai.budget import BudgetLimits, RootBudget
from loop.capabilities.runners import CapabilityInvoker
from loop.core.clock import Clock, SystemClock, from_micros, to_micros
from loop.core.errors import (
    ErrorCode,
    InvalidInput,
    LoopError,
    Unavailable,
)
from loop.core.ids import new_id
from loop.core.privacy import PrivacyLabel
from loop.runtime.authority import AuthorityContext
from loop.runtime.jobs import Job, JobQueue
from loop.runtime.notify_policy import Candidate, Category, Decision, NotificationManager
from loop.runtime.outbox import NotificationOutbox
from loop.runtime.routines import Routine, RoutineService
from loop.runtime.triggers import Trigger, TriggerService

logger = logging.getLogger(__name__)

#: The job kind this worker claims. Distinct from the coordinator's kinds so
#: `JobQueue.claim(kinds=[...])` routes by kind and neither worker sees the
#: other's work.
ROUTINE_KIND = "routine.run"

#: Subject type for a routine's trigger row.
ROUTINE_SUBJECT = "routine"

#: Trigger kinds a clock can schedule. `event` and `condition` routines are
#: valid documents — they are simply not driven by a wall clock, and quietly
#: inventing a daily schedule for one would run it at a time nobody chose.
CLOCK_KINDS = ("local_schedule", "at")


@dataclass
class ScheduleReport:
    """What reconciliation actually changed, for `status` and for tests."""

    scheduled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    unschedulable: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.scheduled or self.disabled)


class RoutineScheduler:
    """Keeps the trigger table agreeing with which routines are active."""

    def __init__(self, *, routines: RoutineService, triggers: TriggerService,
                 clock: Clock | None = None) -> None:
        self.routines = routines
        self.triggers = triggers
        self._clock = clock or SystemClock()

    # ------------------------------------------------------------------ #
    # Activation
    # ------------------------------------------------------------------ #
    def activate(self, slug: str, *, activation_event_id: str) -> Routine:
        """Activate a routine and put its schedule in the trigger table.

        `RoutineService.activate` refuses a routine with unmet requirements,
        so a routine missing its location never reaches the scheduler — which
        is why this does not need its own second check for that.
        """
        routine = self.routines.activate(slug,
                                         activation_event_id=activation_event_id)
        self.ensure_scheduled(routine)
        return routine

    def pause(self, slug: str) -> Routine:
        """Pause a routine and stop its trigger firing."""
        routine = self.routines.pause(slug)
        for trigger in self.triggers.for_subject(ROUTINE_SUBJECT, slug):
            self.triggers.disable(trigger.id)
        return routine

    def resume(self, slug: str) -> Routine:
        routine = self.routines.resume(slug)
        self.ensure_scheduled(routine)
        return routine

    # ------------------------------------------------------------------ #
    # Scheduling
    # ------------------------------------------------------------------ #
    def ensure_scheduled(self, routine: Routine) -> Trigger | None:
        """Create the routine's trigger, or return the one it already has.

        Idempotent on purpose: reconciliation, a repeated activation and a
        restart all call this, and a second trigger for one routine would
        deliver the same briefing twice every morning.
        """
        if not routine.is_active:
            return None
        existing = self.triggers.for_subject(ROUTINE_SUBJECT, routine.slug)
        if existing:
            return existing[0]

        definition = routine.trigger
        kind = str(definition.get("kind", ""))
        if kind not in CLOCK_KINDS:
            return None

        if kind == "local_schedule":
            return self.triggers.create_local_schedule(
                subject_type=ROUTINE_SUBJECT, subject_id=routine.slug,
                days=[str(day) for day in definition.get("days", [])],
                at=str(definition.get("at", "")),
                timezone=str(definition.get("timezone", "")),
                catch_up=str(definition.get("catch_up", "skip")))

        instant = definition.get("instant_utc")
        moment = (from_micros(int(instant)) if instant is not None
                  else self._clock.now())
        return self.triggers.create_at(
            subject_type=ROUTINE_SUBJECT, subject_id=routine.slug,
            instant=moment, timezone=str(definition.get("timezone", "UTC")))

    def shift_schedule(self, slug: str, *, minutes: int,
                       authority_event_id: str | None = None) -> str:
        """Move a routine's wall-clock time, and its live trigger with it.

        This is what makes a confirmed timing preference mean something. It
        goes through `RoutineService.apply_edit`, so the authority rules still
        hold: a *time* change is a behaviour change and applies directly, while
        anything that widened destination, tools or effects would still need
        fresh approval — moving a reminder must not become a way to edit what
        it is allowed to do.

        The old trigger is disabled and a new one created rather than the row
        being updated in place: `trigger_firings` is keyed by trigger and
        revision, and rewriting the time under an existing trigger would leave
        today's already-fired occurrence looking unfired at the new time.
        """
        routine = self._require_routine(slug)
        definition = dict(routine.trigger)
        if definition.get("kind") != "local_schedule":
            raise InvalidInput(
                f"Routine {slug!r} is not on a wall-clock schedule, so there "
                "is no time to move.",
                details={"kind": definition.get("kind")})

        current = str(definition.get("at", "00:00"))
        try:
            hour, minute = (int(part) for part in current.split(":"))
        except ValueError as exc:
            raise InvalidInput(f"Routine {slug!r} has an unreadable time "
                               f"{current!r}") from exc

        total = (hour * 60 + minute + minutes) % (24 * 60)
        moved = f"{total // 60:02d}:{total % 60:02d}"
        definition["at"] = moved

        updated = replace(routine, trigger=definition)
        self.routines.apply_edit(slug, updated,
                                 authority_event_id=authority_event_id)

        for trigger in self.triggers.for_subject(ROUTINE_SUBJECT, slug):
            self.triggers.disable(trigger.id)
        self.ensure_scheduled(self._require_routine(slug))
        return f"{slug} moved from {current} to {moved}"

    def _require_routine(self, slug: str) -> Routine:
        routine = self.routines.get(slug)
        if routine is None:
            raise Unavailable(f"No routine named {slug!r}.",
                              details={"routine": slug})
        return routine

    def reconcile(self) -> ScheduleReport:
        """Make the trigger table match the routine table.

        Called at startup. Activation and trigger creation are two writes to
        two tables, so a crash between them leaves a routine the owner
        approved with nothing that will ever run it — a silent failure that no
        error surface would ever mention. This is the sweep that finds it.
        """
        report = ScheduleReport()
        for routine in self.routines.list_routines():
            existing = self.triggers.for_subject(ROUTINE_SUBJECT, routine.slug)
            if routine.is_active and not existing:
                created = self.ensure_scheduled(routine)
                if created is None:
                    report.unschedulable[routine.slug] = (
                        f"trigger kind {routine.trigger.get('kind')!r} is not "
                        "driven by a clock")
                else:
                    report.scheduled.append(routine.slug)
            elif not routine.is_active and existing:
                for trigger in existing:
                    if self.triggers.disable(trigger.id):
                        report.disabled.append(routine.slug)
        return report


class RoutineDispatcher:
    """`on_trigger`: turn a fired routine trigger into one durable job."""

    def __init__(self, *, jobs: JobQueue, routines: RoutineService) -> None:
        self.jobs = jobs
        self.routines = routines

    def __call__(self, trigger: Any, decision: str,
                 occurrence_key: str,
                 session: Session | None = None) -> str | None:
        if getattr(trigger, "subject_type", "") != ROUTINE_SUBJECT:
            return None
        slug = str(trigger.subject_id)
        routine = self.routines.get(slug)
        if routine is None:
            logger.warning("Trigger %s names unknown routine %s",
                           trigger.id, slug)
            return None
        if not routine.is_active:
            # A paused routine whose trigger was still enabled: do the safe
            # thing now and let reconciliation disable the trigger.
            logger.info("Skipping fired trigger for inactive routine %s", slug)
            return None

        return self.jobs.enqueue(
            ROUTINE_KIND,
            dedupe_key=f"{ROUTINE_KIND}:{slug}:{occurrence_key}",
            payload={"slug": slug, "occurrence_key": occurrence_key,
                     "catch_up": decision, "trigger_id": trigger.id},
            session=session)


@dataclass
class RoutineOutcome:
    """What one routine run produced, stated precisely enough to report."""

    job_id: str
    slug: str
    occurrence_key: str
    state: str                       # succeeded | failed | retry_wait
    decision: str = ""               # the notification policy decision
    reason: str = ""
    notification_id: str | None = None
    message: str = ""
    sources: list[str] = field(default_factory=list)
    error: str = ""


def category_for(routine: Routine) -> Category:
    """Which notification category this routine's output carries.

    `each_occurrence` is the "every morning give me weather" shape from
    interfaces §6: the owner asked for the message itself, so it is a
    requested routine, not a discretionary nudge. Anything else is
    discretionary and lives under the daily cap — the conservative reading,
    because getting this backwards silently uncaps a subscription.
    """
    mode = str(routine.notification.get("mode") or "")
    if mode == "digest":
        return Category.DIGEST
    if mode in ("each_occurrence", "once"):
        return Category.REQUESTED_ROUTINE
    return Category.DISCRETIONARY


class RoutineJobWorker:
    """Claims `routine.run` jobs and executes the routine's steps."""

    def __init__(self, *, jobs: JobQueue, routines: RoutineService,
                 invoker: CapabilityInvoker, outbox: NotificationOutbox,
                 notifications: NotificationManager,
                 owner: str = "owner", default_destination: str = "",
                 clock: Clock | None = None,
                 budget_limits: BudgetLimits | None = None,
                 barrier: Any = None) -> None:
        self.jobs = jobs
        self.routines = routines
        self.invoker = invoker
        self.outbox = outbox
        self.notifications = notifications
        self.owner = owner
        self.default_destination = default_destination
        self._clock = clock or SystemClock()
        self._limits = budget_limits or BudgetLimits()
        self.barrier = barrier

    # ------------------------------------------------------------------ #
    # One job
    # ------------------------------------------------------------------ #
    def run_one(self, *, worker_id: str | None = None) -> RoutineOutcome | None:
        """Claim and run one routine job, or return None if none is due."""
        if self.barrier is not None and self.barrier.held():
            return None          # a snapshot is running; admit nothing new
        job = self.jobs.claim(worker_id or new_id(), kinds=[ROUTINE_KIND])
        if job is None:
            return None
        return self.execute(job)

    def run_due(self, *, limit: int = 20,
                worker_id: str | None = None) -> list[RoutineOutcome]:
        """Drain the routine queue, bounded."""
        outcomes: list[RoutineOutcome] = []
        for _ in range(limit):
            outcome = self.run_one(worker_id=worker_id)
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    def execute(self, job: Job) -> RoutineOutcome:
        slug = str(job.payload.get("slug", ""))
        occurrence = str(job.payload.get("occurrence_key", ""))
        try:
            return self._execute(job, slug, occurrence)
        except LoopError as exc:
            state = self.jobs.fail(job, code=exc.code)
            logger.warning("Routine job %s (%s) failed: %s", job.id, slug, exc)
            return RoutineOutcome(job.id, slug, occurrence, state,
                                  error=str(exc))
        except Exception as exc:                       # noqa: BLE001 — reported
            state = self.jobs.fail(job, code=ErrorCode.INTERNAL_ERROR)
            logger.exception("Routine job %s (%s) raised", job.id, slug)
            return RoutineOutcome(job.id, slug, occurrence, state,
                                  error=str(exc))

    def _execute(self, job: Job, slug: str, occurrence: str) -> RoutineOutcome:
        routine = self.routines.get(slug)
        if routine is None:
            raise Unavailable(f"Routine {slug!r} no longer exists.",
                              details={"routine": slug})
        if not routine.is_active:
            # Paused between firing and claiming. The job is done — there is
            # nothing left to run — and nothing is delivered.
            self.jobs.succeed(job)
            return RoutineOutcome(job.id, slug, occurrence, "succeeded",
                                  decision=Decision.SUPPRESS.value,
                                  reason="the routine was paused before this "
                                         "occurrence ran")

        message, sources = self._run_steps(routine, job)
        outcome = self._deliver(routine, job, occurrence, message, sources)
        self.jobs.succeed(job)
        return outcome

    # ------------------------------------------------------------------ #
    # Steps
    # ------------------------------------------------------------------ #
    def _run_steps(self, routine: Routine,
                   job: Job) -> tuple[str, list[str]]:
        """Run each step through the shared invoker, in order.

        The context is rebuilt from the routine and the job rather than held
        anywhere: an `AuthorityContext` carries a live `RootBudget` and cannot
        survive a JSON column, and a budget restored from a payload would let
        every retry start spending again from zero.
        """
        context = AuthorityContext(
            owner=self.owner, root_id=job.id,
            privacy=PrivacyLabel.for_unlabelled_import(),
            budget=RootBudget(limits=self._limits, root_id=job.id),
            granted_scopes=frozenset(routine.declared_tools))

        parts: list[str] = []
        sources: list[str] = []
        for step in routine.steps:
            operation = str(step.get("capability", ""))
            if not operation:
                continue
            result = self.invoker.invoke(
                operation, dict(step.get("arguments") or {}), context=context,
                parent_run_id=job.id)
            answer = result.output.get("answer")
            if isinstance(answer, str) and answer.strip():
                parts.append(answer.strip())
            for source in result.output.get("sources") or []:
                if isinstance(source, str) and source not in sources:
                    sources.append(source)
        return "\n\n".join(parts), sources

    # ------------------------------------------------------------------ #
    # Delivery
    # ------------------------------------------------------------------ #
    def _deliver(self, routine: Routine, job: Job, occurrence: str,
                 message: str, sources: list[str]) -> RoutineOutcome:
        """Apply notification policy, then queue at most one message."""
        if not message.strip():
            return RoutineOutcome(
                job.id, routine.slug, occurrence, "succeeded",
                decision=Decision.SUPPRESS.value,
                reason="the routine produced nothing to say",
                sources=sources)

        destination = routine.destination or self.default_destination
        if not destination:
            # An unknown destination is not a delivery to guess at: sending
            # the owner's briefing to a default chat nobody configured is a
            # disclosure, not a fallback (A18).
            return RoutineOutcome(
                job.id, routine.slug, occurrence, "succeeded",
                decision=Decision.SUPPRESS.value,
                reason="no destination is configured for this routine",
                message=message, sources=sources)

        category = category_for(routine)
        candidate = Candidate(
            category=category, subject_ref=f"routine:{routine.slug}",
            occurrence_key=occurrence, scope=str(routine.limits.get("scope", "")),
            why_now=str(routine.notification.get("why_now")
                        or f"{routine.title} ran on schedule"),
            created_at=self._clock.now(),
            # The owner chose the *trigger*, not the hour a forecast turns
            # actionable, so this cannot claim a quiet-hours exception (WF20).
            timing_chosen_by_user=(
                str(routine.trigger.get("kind", "")) in CLOCK_KINDS))
        outcome = self.notifications.decide(candidate)

        if outcome.decision in (Decision.SUPPRESS, Decision.EXPIRE):
            return RoutineOutcome(
                job.id, routine.slug, occurrence, "succeeded",
                decision=outcome.decision.value, reason=outcome.reason,
                message=message, sources=sources)

        deliver_at = outcome.deliver_at or self._clock.now()
        notification_id = self.outbox.enqueue(
            category=category.value, subject_ref=f"routine:{routine.slug}",
            occurrence_key=occurrence, destination_id=destination,
            payload={"text": message, "sources": sources,
                     "routine": routine.slug},
            not_before=to_micros(deliver_at))
        if outcome.decision is Decision.SEND:
            self.notifications.record_sent(candidate)

        return RoutineOutcome(
            job.id, routine.slug, occurrence, "succeeded",
            decision=outcome.decision.value, reason=outcome.reason,
            notification_id=notification_id, message=message, sources=sources)


def next_local_fire(trigger: Trigger) -> dt.datetime | None:
    """The trigger's next firing as a datetime, for status output."""
    return None if trigger.next_fire_at is None else from_micros(trigger.next_fire_at)
