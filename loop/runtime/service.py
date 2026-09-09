"""The runtime service loop (runtime §2, §7; interfaces §3).

One process owns the scheduler, the queue and the inbound poller. That ownership
is enforced with a **database-backed leader lease**, not a module-level flag: a
web reload re-imports the module, and a second bot alias is a separate process
entirely, so an in-process guard cannot see either (D15).

`tick()` performs one bounded sweep and returns what it did. The sweep is
**entirely deterministic** — it fires due triggers, claims jobs, and dispatches
the outbox without consulting a model. An idle service therefore costs zero model
calls no matter how much time passes (D16, D17): a reminder is a stored row and a
timestamp comparison, not a question for an LLM.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, from_micros, to_micros
from loop.core.ids import new_id
from loop.runtime.jobs import JobQueue
from loop.runtime.outbox import NotificationOutbox
from loop.runtime.triggers import TriggerService, occurrence_key_for_one_shot

logger = logging.getLogger(__name__)

#: How long a leader holds the singleton lease before it must renew.
LEADER_LEASE_SECONDS = 30

#: Safety bound so one sweep cannot run away.
MAX_ITEMS_PER_SWEEP = 100


@dataclass
class TickReport:
    """What one deterministic sweep did. No model was involved."""

    triggers_fired: int = 0
    triggers_skipped: int = 0
    jobs_claimed: int = 0
    notifications_sent: int = 0
    notifications_cancelled: int = 0
    #: Delivery attempted and refused — most often no transport is registered
    #: for the channel. Counted separately because "0 sent" with nothing else
    #: reported is indistinguishable from "nothing was due", and a briefing
    #: that was produced and then could not be delivered is not nothing.
    notifications_failed: int = 0
    unknown_sends: int = 0
    reclaimed: int = 0
    model_calls: int = 0
    details: list[str] = field(default_factory=list)

    @property
    def did_work(self) -> bool:
        return bool(self.triggers_fired or self.jobs_claimed
                    or self.notifications_sent or self.reclaimed)


class LeaderLease:
    """A database-backed singleton lease for the scheduler/poller role.

    Stored in ``preferences``-style key rows so it survives a restart and is
    visible to every process sharing the database — which an in-process flag is
    not (D15).
    """

    def __init__(self, *, sessions: sessionmaker[Session], clock: Clock,
                 role: str = "scheduler",
                 lease_seconds: int = LEADER_LEASE_SECONDS) -> None:
        self._sessions = sessions
        self._clock = clock
        self._role = role
        self._lease_seconds = lease_seconds
        self.owner_id = new_id()

    def _ensure_table(self) -> None:
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS leader_leases ("
                "role VARCHAR(32) PRIMARY KEY, owner_id VARCHAR(36) NOT NULL, "
                "lease_until BIGINT NOT NULL)"))
            session.commit()

    def acquire(self) -> bool:
        """Take or renew the lease. False means another process holds it."""
        self._ensure_table()
        now = to_micros(self._clock.now())
        until = now + self._lease_seconds * 1_000_000

        with self._sessions() as session:
            try:
                session.execute(text(
                    "INSERT INTO leader_leases (role, owner_id, lease_until) "
                    "VALUES (:role, :owner, :until)"),
                    {"role": self._role, "owner": self.owner_id, "until": until})
                session.commit()
                return True
            except IntegrityError:
                session.rollback()

            # Take over only an expired lease, or renew our own.
            result: CursorResult[Any] = session.execute(text(  # type: ignore[assignment]
                "UPDATE leader_leases SET owner_id = :owner, lease_until = :until "
                "WHERE role = :role AND (lease_until < :now OR owner_id = :owner)"),
                {"owner": self.owner_id, "until": until, "role": self._role,
                 "now": now})
            session.commit()
            return bool(result.rowcount)

    def held_by(self) -> str | None:
        self._ensure_table()
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT owner_id, lease_until FROM leader_leases WHERE role = :role"),
                {"role": self._role}).first()
        if row is None:
            return None
        if row[1] < to_micros(self._clock.now()):
            return None
        return str(row[0])

    def release(self) -> None:
        with self._sessions() as session:
            session.execute(text(
                "DELETE FROM leader_leases WHERE role = :role AND owner_id = :owner"),
                {"role": self._role, "owner": self.owner_id})
            session.commit()


class LoopService:
    """Runs bounded deterministic sweeps over triggers, jobs and the outbox."""

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None,
                 triggers: TriggerService | None = None,
                 jobs: JobQueue | None = None,
                 outbox: NotificationOutbox | None = None,
                 worker_id: str | None = None,
                 on_trigger: Callable[[Any, str, str, Session | None], object] | None = None,
                 barrier: Any = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self.triggers = triggers or TriggerService(sessions=sessions, clock=self._clock)
        self.jobs = jobs or JobQueue(sessions=sessions, clock=self._clock)
        self.outbox = outbox or NotificationOutbox(sessions=sessions, clock=self._clock)
        self.worker_id = worker_id or new_id()
        self.leader = LeaderLease(sessions=sessions, clock=self._clock)
        #: Admission control during a coordinated snapshot. Optional so every
        #: existing test that builds a bare service keeps working.
        self.barrier = barrier
        #: Called when a trigger fires, with the trigger, the catch-up
        #: decision and the *occurrence key* that was just claimed.
        #:
        #: The key is passed rather than recomputed by the callback: it is the
        #: exactly-once identity this sweep already committed to
        #: `trigger_firings`, and a callback deriving its own would be free to
        #: derive a different one — which is how one occurrence becomes two
        #: jobs. Deterministic by contract: this must not invoke a model, or
        #: D16/D17 stop holding.
        self._on_trigger = on_trigger

    # ------------------------------------------------------------------ #
    # One sweep
    # ------------------------------------------------------------------ #
    def tick(self) -> TickReport:
        """Process everything currently due, once. Never calls a model."""
        report = TickReport()

        if self.barrier is not None and self.barrier.held():
            # A snapshot is running. In-flight work finishes; nothing new is
            # admitted, which is what lets the backup prove consistency
            # instead of racing us (O09/LG11).
            report.details.append("snapshot in progress; not admitting work")
            return report

        if not self.leader.acquire():
            report.details.append("not leader; skipping sweep")
            return report

        self.record_heartbeat()
        report.reclaimed = self.jobs.reclaim_expired()
        self._fire_due_triggers(report)
        self._dispatch_outbox(report)
        return report

    def record_heartbeat(self) -> None:
        """Persist that a sweep actually ran (O13).

        Written from the sweep rather than from process start, because "the
        process is alive" and "scheduled work is being done" are different
        claims and only the second one matters. A laptop that slept through
        the night has a live process and a stale heartbeat.
        """
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS service_heartbeat ("
                " id INTEGER PRIMARY KEY CHECK (id = 1),"
                " last_beat_at INTEGER NOT NULL,"
                " worker_id TEXT NOT NULL)"))
            session.execute(text(
                "INSERT INTO service_heartbeat (id, last_beat_at, worker_id)"
                " VALUES (1, :now, :worker)"
                " ON CONFLICT(id) DO UPDATE SET last_beat_at = excluded.last_beat_at,"
                " worker_id = excluded.worker_id"),
                {"now": now, "worker": self.worker_id})
            session.commit()

    def last_heartbeat(self) -> int | None:
        """When a sweep last ran, in epoch seconds, or None if never."""
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS service_heartbeat ("
                " id INTEGER PRIMARY KEY CHECK (id = 1),"
                " last_beat_at INTEGER NOT NULL,"
                " worker_id TEXT NOT NULL)"))
            session.commit()
            row = session.execute(text(
                "SELECT last_beat_at FROM service_heartbeat WHERE id = 1"
            )).first()
        return int(row[0]) // 1_000_000 if row else None

    def health(self) -> Any:
        """What `status` may honestly say about the background service."""
        from loop.ops.doctor import ServiceHealth

        return ServiceHealth(last_heartbeat_at=self.last_heartbeat(),
                             now=int(self._clock.now().timestamp()))

    def _fire_due_triggers(self, report: TickReport) -> None:
        for trigger in self.triggers.due_triggers(limit=MAX_ITEMS_PER_SWEEP):
            decision = self.triggers.catch_up_decision(trigger)
            if decision in ("skip", "digest"):
                report.triggers_skipped += 1
                self._advance_or_disable(trigger)
                report.details.append(f"{trigger.id}: {decision}")
                continue

            key = (occurrence_key_for_one_shot(trigger.id)
                   if trigger.kind == "at" else self._schedule_key(trigger))
            nominal = from_micros(trigger.next_fire_at or to_micros(self._clock.now()))
            with self._sessions() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                try:
                    firing_id = self.triggers.claim_occurrence(
                        trigger, key, nominal_at=nominal,
                        effective_at=self._clock.now(), session=session)

                    if firing_id is None:
                        report.triggers_skipped += 1
                        self._advance_or_disable(trigger, session=session)
                        session.commit()
                        continue

                    if self._on_trigger is not None:
                        self._on_trigger(trigger, decision, key, session)
                    self._advance_or_disable(trigger, session=session)
                    session.commit()
                except Exception:
                    session.rollback()
                    raise

            report.triggers_fired += 1
            if decision == "fire_delayed":
                report.details.append(f"{trigger.id}: delayed")

    def _schedule_key(self, trigger: Any) -> str:
        from datetime import time as _time
        from zoneinfo import ZoneInfo

        from loop.runtime.triggers import occurrence_key_for_schedule

        definition = trigger.definition
        timezone = definition.get("timezone", "UTC")
        hour, minute = (int(p) for p in str(definition.get("at", "00:00")).split(":"))
        moment = from_micros(trigger.next_fire_at or to_micros(self._clock.now()))
        local_date = moment.astimezone(ZoneInfo(timezone)).date()
        return occurrence_key_for_schedule(local_date, _time(hour, minute), timezone)

    def _advance_or_disable(self, trigger: Any,
                            session: Session | None = None) -> None:
        """Move a schedule to its next occurrence; retire a one-shot."""
        now = to_micros(self._clock.now())
        if trigger.kind == "at":
            def _disable(active: Session) -> None:
                active.execute(text(
                    "UPDATE triggers SET enabled = 0, last_fire_at = :now, "
                    "next_fire_at = NULL, updated_at = :now, version = version + 1 "
                    "WHERE id = :id"), {"now": now, "id": trigger.id})
            if session is not None:
                _disable(session)
            else:
                with self._sessions() as own:
                    _disable(own)
                    own.commit()
            return

        from datetime import time as _time

        definition = trigger.definition
        hour, minute = (int(p) for p in str(definition.get("at", "00:00")).split(":"))
        occurrence = self.triggers.next_occurrence_after(
            self._clock.now(), days=list(definition.get("days", [])),
            wall_time=_time(hour, minute),
            timezone=definition.get("timezone", "UTC"))
        def _advance(active: Session) -> None:
            active.execute(text(
                "UPDATE triggers SET last_fire_at = :now, next_fire_at = :next, "
                "updated_at = :now, version = version + 1 WHERE id = :id"),
                {"now": now, "next": to_micros(occurrence.effective_at),
                 "id": trigger.id})
        if session is not None:
            _advance(session)
        else:
            with self._sessions() as own:
                _advance(own)
                own.commit()

    def _dispatch_outbox(self, report: TickReport) -> None:
        for item in self.outbox.due_items(limit=MAX_ITEMS_PER_SWEEP):
            state = self.outbox.dispatch(item)
            if state == "delivered":
                report.notifications_sent += 1
            elif state == "cancelled":
                report.notifications_cancelled += 1
            elif state == "unknown":
                report.unknown_sends += 1
            elif state in ("failed", "retry_wait"):
                report.notifications_failed += 1
                report.details.append(f"{item.id}: delivery {state}")

    # ------------------------------------------------------------------ #
    # Bounded run
    # ------------------------------------------------------------------ #
    def run_once(self, *, max_sweeps: int = 10) -> list[TickReport]:
        """Process due work to quiescence and return.

        Backs ``loop run --once``. Bounded so a recurring schedule cannot turn
        a test or a CI job into an infinite run (interfaces §3).
        """
        reports: list[TickReport] = []
        for _ in range(max_sweeps):
            report = self.tick()
            reports.append(report)
            if not report.did_work:
                break
        return reports

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    def next_wake_at(self) -> int | None:
        """The earliest future wake-up, for `loop status`."""
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT MIN(t) FROM ("
                "  SELECT MIN(next_fire_at) AS t FROM triggers WHERE enabled = 1 "
                "    AND next_fire_at IS NOT NULL"
                "  UNION ALL"
                "  SELECT MIN(run_after) FROM jobs WHERE state IN ('queued','retry_wait')"
                "  UNION ALL"
                "  SELECT MIN(retry_at) FROM outbox WHERE state IN ('pending','retry_wait')"
                ")")).scalar()
        return int(row) if row is not None else None

    def pending_summary(self) -> dict[str, int]:
        with self._sessions() as session:
            return {
                "triggers": session.execute(text(
                    "SELECT COUNT(*) FROM triggers WHERE enabled = 1")).scalar() or 0,
                "jobs": session.execute(text(
                    "SELECT COUNT(*) FROM jobs WHERE state IN ('queued','retry_wait')"
                )).scalar() or 0,
                "outbox": session.execute(text(
                    "SELECT COUNT(*) FROM outbox WHERE state IN ('pending','retry_wait')"
                )).scalar() or 0,
                "unknown": session.execute(text(
                    "SELECT COUNT(*) FROM outbox WHERE state = 'unknown'")).scalar() or 0,
            }


class CountingModel:
    """A model stand-in that records every call.

    Used to assert that deterministic paths invoke no model at all (D16, D17).
    Its value is that it *fails loudly* rather than quietly returning text.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke(self, prompt: str, **kwargs: Any) -> str:
        self.calls.append({"prompt": prompt, **kwargs})
        raise AssertionError(
            "A model was invoked on a path that must stay deterministic. "
            f"Prompt: {json.dumps(prompt)[:120]}")
