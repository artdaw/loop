"""Executing a scheduled trip check (TR15–TR19 — R4).

`TripMonitorScheduler` turned an approved plan into durable `trip.check` jobs
and nothing consumed them, so monitoring scheduled work that never ran. This
is the worker, and its shape is set by what a check is allowed to conclude.

**A check produces one of four outcomes, and three of them are not "nothing
changed".** `success` (a material change, with evidence), `no_change`,
`partial` (some sources answered, some did not) and `failed`. Collapsing the
middle two is how an outage becomes a reassuring silence — the owner hears
nothing and believes the trip is fine, when in fact nobody looked.

**A no-change check invokes no model** (travel §7). Comparison is arithmetic
over quotes and durations; a model in that path would spend budget on every
scheduled check of every trip, forever, to conclude nothing.

**Authority is rechecked at execution, not at scheduling.** A job may sit in
the queue while the trip is cancelled, the option reselected, or the scope
narrowed. So the worker re-reads monitoring state, the trip's selection and the
job's lease before any read, and refuses rather than acting on an approval that
has since been withdrawn.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from loop.ai.budget import BudgetLimits, RootBudget
from loop.capabilities.travel.monitor import (
    ChangeKind,
    ChangeReport,
    Check,
    NoticeLedger,
    compare_cost,
    compare_transport,
)
from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.errors import ErrorCode, LoopError
from loop.core.ids import new_id
from loop.runtime.jobs import Job, JobQueue
from loop.runtime.outbox import NotificationOutbox
from loop.runtime.trip_monitor import TRIP_JOB_KIND, TripMonitorScheduler

logger = logging.getLogger(__name__)


class CheckOutcome(str):
    SUCCESS = "success"
    NO_CHANGE = "no_change"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass
class TripCheckResult:
    """What one scheduled check concluded, persisted as the job's outcome."""

    job_id: str
    trip_id: str
    outcome: str
    checks_run: list[str] = field(default_factory=list)
    checks_unavailable: dict[str, str] = field(default_factory=dict)
    changes: list[ChangeReport] = field(default_factory=list)
    notified: list[str] = field(default_factory=list)
    reason: str = ""
    model_calls: int = 0

    @property
    def material(self) -> bool:
        return any(change.kind.notifies for change in self.changes)

    def as_record(self) -> dict[str, Any]:
        """JSON-safe durable representation stored with the fenced job."""
        return asdict(self)


class TripCheckSource:
    """What a check reads. Injected, so the worker never opens a socket.

    Every shipped implementation of this is unavailable, and that is the
    honest state: fares and schedules need an account, and travel §4 requires
    configured budget authority before an adapter that incurs charges may run.
    An unavailable source is *reported*, never treated as "no change".
    """

    name = "unconfigured"

    def read(self, *, trip_id: str, option_id: str,
             check: Check) -> dict[str, Any] | None:
        del trip_id, option_id, check
        return None

    def unavailable_reason(self, check: Check) -> str:
        return (f"no provider is configured for {check.value} checks; "
                "each needs an account and budget authority (travel §4)")


class TripCheckWorker:
    """Claims `trip.check` jobs and runs the approved checks."""

    def __init__(self, *, jobs: JobQueue, scheduler: TripMonitorScheduler,
                 outbox: NotificationOutbox, notifications: Any = None,
                 sources: dict[str, TripCheckSource] | None = None,
                 destination: str = "", clock: Clock | None = None,
                 notices: NoticeLedger | None = None,
                 budget_limits: BudgetLimits | None = None) -> None:
        self.jobs = jobs
        self.scheduler = scheduler
        self.outbox = outbox
        self.notifications = notifications
        self.sources = dict(sources or {})
        self.destination = destination
        self._clock = clock or SystemClock()
        self.notices = notices or scheduler.notices
        self._limits = budget_limits or BudgetLimits()

    def run_one(self, *, worker_id: str | None = None) -> TripCheckResult | None:
        job = self.jobs.claim(worker_id or new_id(), kinds=[TRIP_JOB_KIND])
        if job is None:
            return None
        return self.execute(job)

    def run_due(self, *, limit: int = 20) -> list[TripCheckResult]:
        results: list[TripCheckResult] = []
        for _ in range(limit):
            result = self.run_one()
            if result is None:
                break
            results.append(result)
        return results

    def execute(self, job: Job) -> TripCheckResult:
        trip_id = str(job.payload.get("trip_id", ""))
        try:
            result = self._execute(job, trip_id)
        except LoopError as exc:
            result = TripCheckResult(job.id, trip_id, CheckOutcome.FAILED,
                                     reason=exc.message)
            state = self.jobs.fail(job, code=exc.code,
                                   result=result.as_record())
            logger.warning("Trip check %s failed: %s", job.id, exc)
            result.reason = f"{exc.message} ({state})"
            return result
        except Exception as exc:                       # noqa: BLE001 — reported
            result = TripCheckResult(job.id, trip_id, CheckOutcome.FAILED,
                                     reason=str(exc))
            state = self.jobs.fail(job, code=ErrorCode.INTERNAL_ERROR,
                                   result=result.as_record())
            logger.exception("Trip check %s raised", job.id)
            result.reason = f"{exc} ({state})"
            return result
        self.jobs.succeed(job, result=result.as_record())
        return result

    def _execute(self, job: Job, trip_id: str) -> TripCheckResult:
        # Authority is rechecked here, not trusted from scheduling time: the
        # job may have waited while the trip was cancelled or reselected.
        state = self.scheduler.state_for(trip_id)
        if state is None or not state.active:
            return TripCheckResult(
                job.id, trip_id, CheckOutcome.NO_CHANGE,
                reason="monitoring was stopped before this check ran")

        approved = set(state.check_names)
        requested = [c for c in job.payload.get("checks", []) if c in approved]
        dropped = [c for c in job.payload.get("checks", []) if c not in approved]
        if dropped:
            # The scope narrowed after the job was queued. Running the removed
            # check would act on authority that has been withdrawn.
            logger.info("Trip %s: dropping checks no longer approved: %s",
                        trip_id, dropped)

        if state.revision_id != job.payload.get("revision_id") or \
                state.option_id != job.payload.get("option_id"):
            return TripCheckResult(
                job.id, trip_id, CheckOutcome.NO_CHANGE,
                reason="a different revision or option is selected now; this "
                       "check was about the previous one")

        result = TripCheckResult(job.id, trip_id, CheckOutcome.NO_CHANGE)
        budget = RootBudget(limits=self._limits, root_id=job.id)
        del budget          # reserved for a future model-using check; none here

        for name in requested:
            # A provider read is an external effect. Recheck the fencing token
            # immediately before each one, not merely when the job was claimed.
            self.jobs.require_lease(job)
            check = Check(name)
            source = self.sources.get(name)
            if source is None:
                result.checks_unavailable[name] = (
                    TripCheckSource().unavailable_reason(check))
                continue
            reading = source.read(trip_id=trip_id,
                                  option_id=state.option_id, check=check)
            if reading is None:
                result.checks_unavailable[name] = source.unavailable_reason(check)
                continue
            result.checks_run.append(name)
            change = _compare(check, reading)
            if change.kind is not ChangeKind.NONE:
                result.changes.append(change)

        result.outcome = self._conclude(result, requested)
        self._notify(result, state)
        return result

    @staticmethod
    def _conclude(result: TripCheckResult, requested: list[str]) -> str:
        if not requested:
            return CheckOutcome.NO_CHANGE
        if result.checks_unavailable and not result.checks_run:
            # Nothing was actually checked. Saying "no change" here is the
            # failure this outcome exists to prevent.
            return CheckOutcome.FAILED
        if result.checks_unavailable:
            return CheckOutcome.PARTIAL
        return CheckOutcome.SUCCESS if result.material else CheckOutcome.NO_CHANGE

    def _notify(self, result: TripCheckResult, state: Any) -> None:
        """One notice per material change, deduplicated by its evidence."""
        if not self.destination:
            return
        for change in result.changes:
            subject = f"trip:{result.trip_id}"
            if not self.notices.should_notify(trip_id=result.trip_id,
                                              subject=subject, report=change):
                continue
            occurrence = f"{change.kind.value}:{change.before}:{change.after}"
            notification = self.outbox.enqueue(
                category="discretionary", subject_ref=subject,
                occurrence_key=occurrence, destination_id=self.destination,
                payload={"text": change.detail, "trip": result.trip_id,
                         "before": change.before, "after": change.after,
                         "kind": change.kind.value},
                not_before=to_micros(self._clock.now()))
            if notification:
                result.notified.append(notification)
            self.notices.record(trip_id=result.trip_id, subject=subject,
                                report=change)
        del state


def _compare(check: Check, reading: dict[str, Any]) -> ChangeReport:
    """Deterministic comparison. No model is invoked (travel §7)."""
    if check is Check.SCHEDULE:
        return compare_transport(int(reading.get("before_minutes", 0)),
                                 int(reading.get("after_minutes", 0)))
    if check is Check.COST:
        return compare_cost(
            int(reading.get("before_minor", 0)),
            int(reading.get("after_minor", 0)),
            before_basis=reading["before_basis"],
            after_basis=reading["after_basis"],
            currency=str(reading.get("currency", "EUR")))
    if check is Check.CLOSURE and reading.get("closed"):
        return ChangeReport(ChangeKind.CLOSURE,
                            str(reading.get("detail", "a venue is closed")),
                            before=str(reading.get("before", "open")),
                            after=str(reading.get("after", "closed")))
    return ChangeReport(ChangeKind.NONE, f"no change in {check.value}")
