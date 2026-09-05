"""Durable job queue with leases and fencing (runtime §7).

The problem this solves: a worker can stop being the rightful owner of a job
*while it is still running* — its process paused, its lease expired, another
worker took over. When it wakes up mid-call and tries to commit, nothing about
its own state reveals that it is now stale.

A **fencing token** does. Every claim increments the token; every commit passes
the token it was issued and is rejected unless it still matches the row. A stale
worker therefore cannot write results or start a new effect, no matter how
convinced it is that it holds the job (D03).

Claiming uses `BEGIN IMMEDIATE` and a compare-and-set so two workers polling the
same due job cannot both win (D02). Cancellation is re-checked immediately before
each effect, so a job cancelled while queued does not fire (D11).

Retries follow the contract: at most 5 attempts at 5s/30s/120s/600s with jitter,
and permanent classes — schema, permission, unsupported capability, auth — do not
retry at all. Retrying an authentication failure just burns the account.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from typing import Any

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.errors import Conflict, ErrorCode
from loop.core.ids import new_id

logger = logging.getLogger(__name__)

#: Lease duration and renewal cadence (runtime §7).
LEASE_SECONDS = 60
RENEW_SECONDS = 20

#: Transient retry schedule in seconds, with up to 20% jitter.
RETRY_DELAYS = (5, 30, 120, 600)
MAX_ATTEMPTS = 5
JITTER_FRACTION = 0.2

#: Failures that must never be retried blindly. Retrying an auth failure or a
#: schema error cannot succeed and can lock an account.
PERMANENT_FAILURES = frozenset({
    ErrorCode.VALIDATION_FAILED,
    ErrorCode.INVALID_INPUT,
    ErrorCode.PRIVACY_BLOCKED,
    ErrorCode.APPROVAL_REQUIRED,
    ErrorCode.AUTH_REQUIRED,
})


@dataclass
class Job:
    """A claimed unit of work."""

    id: str
    kind: str
    payload: dict[str, Any]
    dedupe_key: str
    state: str
    attempts: int
    max_attempts: int
    fencing_token: int
    lease_owner: str | None = None
    lease_until: int | None = None
    run_after: int = 0
    deadline_at: int | None = None
    last_error_code: str | None = None


class StaleLeaseError(Conflict):
    """Raised when a worker acts with a fencing token that is no longer current."""


def retry_delay(attempt: int, *, rand: random.Random | None = None) -> int:
    """Delay before the next attempt, with jitter. ``attempt`` is 1-based."""
    rng = rand or random
    index = min(attempt - 1, len(RETRY_DELAYS) - 1)
    base = RETRY_DELAYS[index]
    return int(base + rng.uniform(0, base * JITTER_FRACTION))


class JobQueue:
    """Enqueues, claims, renews and completes durable jobs."""

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None,
                 lease_seconds: int = LEASE_SECONDS) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._lease_seconds = lease_seconds

    # ------------------------------------------------------------------ #
    # Enqueue
    # ------------------------------------------------------------------ #
    def enqueue(self, kind: str, *, dedupe_key: str,
                payload: dict[str, Any] | None = None,
                run_after: int | None = None,
                deadline_at: int | None = None,
                max_attempts: int = MAX_ATTEMPTS,
                session: Session | None = None) -> str | None:
        """Queue a job. Returns ``None`` when ``dedupe_key`` already exists.

        A duplicate is a normal outcome, not an error: the same logical work
        arriving twice must produce one job.
        """
        from sqlalchemy.exc import IntegrityError

        now = to_micros(self._clock.now())
        job_id = new_id()
        params = {
            "id": job_id, "now": now, "kind": kind,
            "payload": json.dumps(payload or {}, sort_keys=True),
            "dedupe_key": dedupe_key,
            "run_after": run_after if run_after is not None else now,
            "deadline_at": deadline_at, "max_attempts": max_attempts,
        }
        statement = text(
            "INSERT INTO jobs (id, version, created_at, updated_at, kind, "
            "payload_json, dedupe_key, state, run_after, deadline_at, attempts, "
            "max_attempts, lease_owner, lease_until, fencing_token) "
            "VALUES (:id, 1, :now, :now, :kind, :payload, :dedupe_key, 'queued', "
            ":run_after, :deadline_at, 0, :max_attempts, NULL, NULL, 0)")
        try:
            if session is not None:
                session.execute(statement, params)
            else:
                with self._sessions() as own:
                    own.execute(statement, params)
                    own.commit()
        except IntegrityError:
            logger.info("Job with dedupe key %s already queued", dedupe_key)
            return None
        return job_id

    # ------------------------------------------------------------------ #
    # Claim
    # ------------------------------------------------------------------ #
    def claim(self, worker_id: str, *, kinds: list[str] | None = None) -> Job | None:
        """Claim one due job, or return ``None``.

        Two phases, deliberately separated (see :meth:`select_candidate` and
        :meth:`apply_claim`): read a candidate, then compare-and-set it. The
        compare-and-set is the correctness mechanism — the immediate transaction
        only makes losing the race rarer, never impossible — so the loser must
        detect a zero row count and return ``None`` rather than a job it does
        not own.
        """
        now = to_micros(self._clock.now())
        lease_until = now + self._lease_seconds * 1_000_000

        with self._sessions() as session:
            # BEGIN IMMEDIATE takes the write lock up front, which narrows the
            # window between the read and the update.
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                row = self.select_candidate(session, now=now, kinds=kinds)
                if row is None:
                    session.rollback()
                    return None
                job = self.apply_claim(session, row, worker_id=worker_id,
                                       now=now, lease_until=lease_until)
                if job is None:
                    session.rollback()
                    return None
                session.commit()
                return job
            except Exception:
                session.rollback()
                raise

    def select_candidate(self, session: Session, *, now: int,
                         kinds: list[str] | None = None) -> Any | None:
        """Phase one: find a due, unclaimed job. Returns the raw row."""
        clause = ""
        params: dict[str, Any] = {"now": now}
        if kinds:
            placeholders = ", ".join(f":k{i}" for i in range(len(kinds)))
            clause = f" AND kind IN ({placeholders})"
            params.update({f"k{i}": k for i, k in enumerate(kinds)})

        return session.execute(text(
            "SELECT id, kind, payload_json, dedupe_key, state, attempts, "
            "max_attempts, fencing_token, run_after, deadline_at "
            "FROM jobs WHERE state IN ('queued', 'retry_wait') "
            "AND run_after <= :now" + clause +
            " ORDER BY run_after LIMIT 1"), params).first()

    def apply_claim(self, session: Session, row: Any, *, worker_id: str,
                    now: int, lease_until: int) -> Job | None:
        """Phase two: compare-and-set the claim. ``None`` means we lost."""
        token = row[7] + 1
        result: CursorResult[Any] = session.execute(text(  # type: ignore[assignment]
            "UPDATE jobs SET state = 'running', lease_owner = :owner, "
            "lease_until = :until, fencing_token = :token, "
            "attempts = attempts + 1, updated_at = :now, "
            "version = version + 1 WHERE id = :id AND fencing_token = :prev "
            "AND state IN ('queued', 'retry_wait')"
        ), {"owner": worker_id, "until": lease_until, "token": token,
            "now": now, "id": row[0], "prev": row[7]})

        if result.rowcount == 0:
            # Someone claimed this row between our read and our write.
            return None

        return Job(id=row[0], kind=row[1], payload=json.loads(row[2]),
                   dedupe_key=row[3], state="running", attempts=row[5] + 1,
                   max_attempts=row[6], fencing_token=token,
                   lease_owner=worker_id, lease_until=lease_until,
                   run_after=row[8], deadline_at=row[9])

    # ------------------------------------------------------------------ #
    # Lease validity
    # ------------------------------------------------------------------ #
    def holds_lease(self, job: Job) -> bool:
        """True when this worker's fencing token is still the current one."""
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT fencing_token, lease_owner, lease_until, state "
                "FROM jobs WHERE id = :id"), {"id": job.id}).first()
        if row is None:
            return False
        token, owner, until, state = row
        if token != job.fencing_token or owner != job.lease_owner:
            return False
        if state == "cancelled":
            return False
        return until is not None and until >= to_micros(self._clock.now())

    def require_lease(self, job: Job) -> None:
        """Raise unless the worker may still act. Call before every effect."""
        if not self.holds_lease(job):
            raise StaleLeaseError(
                "Lease is no longer held; this worker may not commit.",
                details={"job_id": job.id, "fencing_token": job.fencing_token},
            )

    def renew(self, job: Job) -> bool:
        """Extend the lease. Returns False if it was already lost."""
        now = to_micros(self._clock.now())
        until = now + self._lease_seconds * 1_000_000
        with self._sessions() as session:
            result: CursorResult[Any] = session.execute(text(  # type: ignore[assignment]
                "UPDATE jobs SET lease_until = :until, updated_at = :now "
                "WHERE id = :id AND fencing_token = :token AND lease_owner = :owner"
            ), {"until": until, "now": now, "id": job.id,
                "token": job.fencing_token, "owner": job.lease_owner})
            session.commit()
        if result.rowcount:
            job.lease_until = until
            return True
        return False

    def reclaim_expired(self) -> int:
        """Return expired running jobs to the queue, invalidating their tokens.

        Runtime §7: orphan recovery MUST invalidate the prior token, otherwise a
        paused worker could wake and commit against the job someone else now owns.
        """
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            result: CursorResult[Any] = session.execute(text(  # type: ignore[assignment]
                "UPDATE jobs SET state = 'queued', lease_owner = NULL, "
                "lease_until = NULL, fencing_token = fencing_token + 1, "
                "updated_at = :now, version = version + 1 "
                "WHERE state = 'running' AND lease_until IS NOT NULL "
                "AND lease_until < :now"), {"now": now})
            session.commit()
        return result.rowcount

    # ------------------------------------------------------------------ #
    # Completion
    # ------------------------------------------------------------------ #
    def succeed(self, job: Job) -> None:
        """Mark a job succeeded, only if the worker still holds the lease."""
        self.require_lease(job)
        self._finish(job, "succeeded", None)

    def fail(self, job: Job, *, code: ErrorCode,
             rand: random.Random | None = None) -> str:
        """Record a failure and decide whether to retry.

        Returns the resulting state: ``retry_wait`` or ``failed``.
        """
        self.require_lease(job)
        permanent = code in PERMANENT_FAILURES
        exhausted = job.attempts >= job.max_attempts

        if permanent or exhausted:
            self._finish(job, "failed", code.value)
            return "failed"

        now = to_micros(self._clock.now())
        delay = retry_delay(job.attempts, rand=rand)
        with self._sessions() as session:
            session.execute(text(
                "UPDATE jobs SET state = 'retry_wait', run_after = :run_after, "
                "lease_owner = NULL, lease_until = NULL, last_error_code = :code, "
                "updated_at = :now, version = version + 1 "
                "WHERE id = :id AND fencing_token = :token"
            ), {"run_after": now + delay * 1_000_000, "code": code.value,
                "now": now, "id": job.id, "token": job.fencing_token})
            session.commit()
        return "retry_wait"

    def cancel(self, job_id: str) -> bool:
        """Cancel a job. An in-flight worker discovers this at its next check."""
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            result: CursorResult[Any] = session.execute(text(  # type: ignore[assignment]
                "UPDATE jobs SET state = 'cancelled', updated_at = :now, "
                "version = version + 1 WHERE id = :id "
                "AND state NOT IN ('succeeded', 'failed')"),
                {"now": now, "id": job_id})
            session.commit()
        return bool(result.rowcount)

    def _finish(self, job: Job, state: str, code: str | None) -> None:
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE jobs SET state = :state, lease_owner = NULL, "
                "lease_until = NULL, last_error_code = :code, updated_at = :now, "
                "version = version + 1 WHERE id = :id AND fencing_token = :token"
            ), {"state": state, "code": code, "now": now, "id": job.id,
                "token": job.fencing_token})
            session.commit()

    # ------------------------------------------------------------------ #
    # Inspection
    # ------------------------------------------------------------------ #
    def get(self, job_id: str) -> Job | None:
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT id, kind, payload_json, dedupe_key, state, attempts, "
                "max_attempts, fencing_token, lease_owner, lease_until, "
                "run_after, deadline_at, last_error_code FROM jobs WHERE id = :id"
            ), {"id": job_id}).first()
        if row is None:
            return None
        return Job(id=row[0], kind=row[1], payload=json.loads(row[2]),
                   dedupe_key=row[3], state=row[4], attempts=row[5],
                   max_attempts=row[6], fencing_token=row[7], lease_owner=row[8],
                   lease_until=row[9], run_after=row[10], deadline_at=row[11],
                   last_error_code=row[12])
