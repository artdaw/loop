"""Durable job queue: leases, fencing, retries, cancellation (runtime §7).

Covers acceptance D02, D03, D05, D11 and the restart half of D01/D14.
"""

from __future__ import annotations

import random

import pytest

from loop.core.errors import ErrorCode
from loop.runtime.jobs import (
    MAX_ATTEMPTS,
    RETRY_DELAYS,
    JobQueue,
    StaleLeaseError,
    retry_delay,
)


@pytest.fixture
def queue(sessions, clock) -> JobQueue:
    return JobQueue(sessions=sessions, clock=clock)


# --------------------------------------------------------------------------- #
# Enqueue and dedupe
# --------------------------------------------------------------------------- #
def test_a_job_can_be_queued_and_read_back(queue):
    job_id = queue.enqueue("reminder.send", dedupe_key="k1",
                           payload={"task_id": "t1"})

    stored = queue.get(job_id)
    assert stored.state == "queued"
    assert stored.payload == {"task_id": "t1"}


def test_the_same_dedupe_key_produces_one_job(queue):
    first = queue.enqueue("x", dedupe_key="same")
    second = queue.enqueue("x", dedupe_key="same")

    assert first is not None
    assert second is None


def test_a_future_job_is_not_claimable_yet(queue, clock):
    from loop.core.clock import to_micros

    queue.enqueue("x", dedupe_key="k",
                  run_after=to_micros(clock.now()) + 3_600_000_000)
    assert queue.claim("worker-1") is None


def test_a_job_becomes_claimable_when_due(queue, clock):
    from loop.core.clock import to_micros

    queue.enqueue("x", dedupe_key="k",
                  run_after=to_micros(clock.now()) + 3_600_000_000)
    clock.advance(hours=2)

    assert queue.claim("worker-1") is not None


# --------------------------------------------------------------------------- #
# D02 — two workers, one winner
# --------------------------------------------------------------------------- #
def test_d02_only_one_worker_claims_a_due_job(queue):
    queue.enqueue("x", dedupe_key="k")

    first = queue.claim("worker-1")
    second = queue.claim("worker-2")

    assert first is not None
    assert second is None


def test_d02_the_claim_increments_the_fencing_token(queue):
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")
    assert job.fencing_token == 1


def test_d02_only_the_current_token_may_commit(queue):
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")

    # Simulate a takeover: the lease expires and another worker reclaims.
    stale = job
    queue.cancel(job.id)  # any state change invalidating the holder

    with pytest.raises(StaleLeaseError):
        queue.succeed(stale)


# --------------------------------------------------------------------------- #
# D03 — a worker that lost its lease cannot commit
# --------------------------------------------------------------------------- #
def test_d03_an_expired_lease_is_not_held(queue, clock):
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")

    clock.advance(seconds=120)  # past the 60s lease

    assert queue.holds_lease(job) is False


def test_d03_a_stale_worker_cannot_succeed(queue, clock):
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")
    clock.advance(seconds=120)
    queue.reclaim_expired()

    with pytest.raises(StaleLeaseError):
        queue.succeed(job)


def test_d03_reclaiming_invalidates_the_prior_token(queue, clock):
    """Runtime §7: orphan recovery MUST invalidate the prior token."""
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")
    clock.advance(seconds=120)

    assert queue.reclaim_expired() == 1
    assert queue.get(job.id).fencing_token > job.fencing_token


def test_d03_a_second_worker_can_take_over_after_expiry(queue, clock):
    queue.enqueue("x", dedupe_key="k")
    first = queue.claim("worker-1")
    clock.advance(seconds=120)
    queue.reclaim_expired()

    second = queue.claim("worker-2")

    assert second is not None
    assert second.fencing_token > first.fencing_token


def test_d03_the_new_owner_can_commit(queue, clock):
    queue.enqueue("x", dedupe_key="k")
    queue.claim("worker-1")
    clock.advance(seconds=120)
    queue.reclaim_expired()
    second = queue.claim("worker-2")

    queue.succeed(second)
    assert queue.get(second.id).state == "succeeded"


def test_renewing_extends_a_held_lease(queue, clock):
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")
    clock.advance(seconds=30)

    assert queue.renew(job) is True
    assert queue.holds_lease(job) is True


def test_renewing_a_lost_lease_fails(queue, clock):
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")
    clock.advance(seconds=120)
    queue.reclaim_expired()

    assert queue.renew(job) is False


def test_a_live_lease_is_not_reclaimed(queue, clock):
    queue.enqueue("x", dedupe_key="k")
    queue.claim("worker-1")
    clock.advance(seconds=10)

    assert queue.reclaim_expired() == 0


# --------------------------------------------------------------------------- #
# D11 — cancellation before the effect
# --------------------------------------------------------------------------- #
def test_d11_a_cancelled_job_fails_the_lease_check(queue):
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")

    queue.cancel(job.id)

    assert queue.holds_lease(job) is False


def test_d11_require_lease_blocks_the_effect_after_cancellation(queue):
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")
    queue.cancel(job.id)

    with pytest.raises(StaleLeaseError):
        queue.require_lease(job)


def test_d11_a_finished_job_cannot_be_cancelled_retroactively(queue):
    """An already-committed effect is history, not something to un-do."""
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")
    queue.succeed(job)

    assert queue.cancel(job.id) is False


# --------------------------------------------------------------------------- #
# D05 — bounded retry
# --------------------------------------------------------------------------- #
def test_d05_a_transient_failure_schedules_a_retry(queue):
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")

    assert queue.fail(job, code=ErrorCode.UNAVAILABLE) == "retry_wait"
    assert queue.get(job.id).state == "retry_wait"


def test_d05_a_retry_waits_before_becoming_claimable(queue):
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")
    queue.fail(job, code=ErrorCode.UNAVAILABLE)

    assert queue.claim("worker-2") is None


def test_d05_the_retry_becomes_claimable_after_the_delay(queue, clock):
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")
    queue.fail(job, code=ErrorCode.UNAVAILABLE)

    clock.advance(seconds=60)
    assert queue.claim("worker-2") is not None


def test_d05_attempts_are_bounded(queue, clock):
    queue.enqueue("x", dedupe_key="k", max_attempts=2)

    job = queue.claim("worker-1")
    assert queue.fail(job, code=ErrorCode.UNAVAILABLE) == "retry_wait"

    clock.advance(seconds=60)
    job = queue.claim("worker-1")
    assert queue.fail(job, code=ErrorCode.UNAVAILABLE) == "failed"


def test_retry_delays_follow_the_documented_schedule():
    for attempt, base in enumerate(RETRY_DELAYS, start=1):
        delay = retry_delay(attempt, rand=random.Random(0))
        assert base <= delay <= base * 1.2


def test_retry_delay_saturates_at_the_last_step():
    assert retry_delay(MAX_ATTEMPTS + 5, rand=random.Random(0)) >= RETRY_DELAYS[-1]


# --------------------------------------------------------------------------- #
# Permanent failures do not retry
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", [
    ErrorCode.AUTH_REQUIRED,
    ErrorCode.VALIDATION_FAILED,
    ErrorCode.PRIVACY_BLOCKED,
    ErrorCode.APPROVAL_REQUIRED,
    ErrorCode.INVALID_INPUT,
])
def test_permanent_failures_are_not_retried(queue, code):
    """Retrying an auth failure cannot succeed and can lock the account."""
    queue.enqueue("x", dedupe_key=f"k-{code.value}")
    job = queue.claim("worker-1")

    assert queue.fail(job, code=code) == "failed"


def test_the_failure_code_is_recorded(queue):
    queue.enqueue("x", dedupe_key="k")
    job = queue.claim("worker-1")
    queue.fail(job, code=ErrorCode.AUTH_REQUIRED)

    assert queue.get(job.id).last_error_code == "auth_required"


# --------------------------------------------------------------------------- #
# D01 / D14 — survival across a restart
# --------------------------------------------------------------------------- #
def test_d01_a_queued_job_survives_a_fresh_queue_instance(sessions, clock):
    """A new process (new JobQueue) still finds the durable work."""
    JobQueue(sessions=sessions, clock=clock).enqueue("x", dedupe_key="k")

    restarted = JobQueue(sessions=sessions, clock=clock)
    assert restarted.claim("worker-after-restart") is not None


def test_d14_work_in_flight_at_shutdown_is_reclaimable_after_restart(sessions, clock):
    before = JobQueue(sessions=sessions, clock=clock)
    before.enqueue("x", dedupe_key="k")
    before.claim("worker-killed")

    clock.advance(seconds=120)
    after = JobQueue(sessions=sessions, clock=clock)

    assert after.reclaim_expired() == 1
    assert after.claim("worker-new") is not None


def test_a_succeeded_job_is_not_reclaimed_after_restart(sessions, clock):
    queue = JobQueue(sessions=sessions, clock=clock)
    queue.enqueue("x", dedupe_key="k")
    queue.succeed(queue.claim("worker-1"))

    clock.advance(seconds=600)
    assert JobQueue(sessions=sessions, clock=clock).reclaim_expired() == 0


# --------------------------------------------------------------------------- #
# D02 under genuine concurrency
# --------------------------------------------------------------------------- #
def test_d02_concurrent_workers_race_and_exactly_one_wins(sessions, clock):
    """A smoke test for thread safety — it does NOT prove the locking.

    Ten threads claim at once and exactly one wins. Verified experimentally:
    this still passes with the compare-and-set removed, because thread
    scheduling rarely produces the interleaving that matters. The real proof of
    the race handling is
    `test_d02_a_claim_that_loses_the_race_returns_none`, which reproduces the
    interleaving deterministically. This test is kept only to catch crashes and
    connection errors under contention.
    """
    import threading

    queue = JobQueue(sessions=sessions, clock=clock)
    queue.enqueue("x", dedupe_key="contended")

    winners: list[str] = []
    errors: list[Exception] = []
    start = threading.Barrier(10)
    lock = threading.Lock()

    def worker(name: str) -> None:
        try:
            start.wait(timeout=5)
            claimed = JobQueue(sessions=sessions, clock=clock).claim(name)
            if claimed is not None:
                with lock:
                    winners.append(name)
        except Exception as exc:  # noqa: BLE001 - surfaced in the assertion
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, f"claim raised under contention: {errors[:3]}"
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"


def test_d02_the_single_winner_holds_a_valid_lease(sessions, clock):
    """Also a smoke test; see the note above about what thread races prove."""
    import threading

    queue = JobQueue(sessions=sessions, clock=clock)
    job_id = queue.enqueue("x", dedupe_key="contended")

    claimed: list = []
    start = threading.Barrier(5)
    lock = threading.Lock()

    def worker(name: str) -> None:
        start.wait(timeout=5)
        result = JobQueue(sessions=sessions, clock=clock).claim(name)
        if result is not None:
            with lock:
                claimed.append(result)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(claimed) == 1
    stored = queue.get(job_id)
    assert stored.state == "running"
    assert stored.lease_owner == claimed[0].lease_owner
    assert stored.fencing_token == claimed[0].fencing_token


def test_d02_a_lost_compare_and_set_yields_no_claim(sessions, clock):
    """The deterministic proof, independent of thread scheduling.

    Two workers reading the same queued row both attempt the compare-and-set
    against fencing_token 0. Exactly one UPDATE can match; the loser must return
    None rather than a Job it does not own. A thread-race test cannot reliably
    force this interleaving, so it is reproduced directly.
    """
    from sqlalchemy import text

    queue = JobQueue(sessions=sessions, clock=clock)
    job_id = queue.enqueue("x", dedupe_key="k")

    # Worker A wins the race.
    winner = queue.claim("worker-a")
    assert winner is not None

    # Worker B is still holding the pre-claim snapshot (token 0) and tries to
    # commit its claim. Its UPDATE must match nothing.
    with sessions() as session:
        result = session.execute(text(
            "UPDATE jobs SET state = 'running', lease_owner = 'worker-b', "
            "fencing_token = 1 WHERE id = :id AND fencing_token = 0 "
            "AND state IN ('queued', 'retry_wait')"), {"id": job_id})
        session.commit()

    assert result.rowcount == 0, "a stale snapshot must not be able to claim"
    assert queue.get(job_id).lease_owner == "worker-a"


def test_d02_a_claim_that_loses_the_race_returns_none(sessions, clock):
    """Reproduce the exact interleaving: read, someone else claims, then write.

    This is the test that fails if `apply_claim` stops checking its row count —
    verified by disabling the check and watching it break.
    """
    from loop.core.clock import to_micros

    queue = JobQueue(sessions=sessions, clock=clock)
    job_id = queue.enqueue("x", dedupe_key="k")
    now = to_micros(clock.now())

    with sessions() as slow_session:
        # Phase one for the slow worker: it now holds a stale snapshot.
        row = queue.select_candidate(slow_session, now=now)
        assert row is not None

        # A different worker completes a full claim in the meantime.
        assert JobQueue(sessions=sessions, clock=clock).claim("worker-fast")

        # Phase two for the slow worker must detect that it lost.
        lost = queue.apply_claim(slow_session, row, worker_id="worker-slow",
                                 now=now, lease_until=now + 60_000_000)
        slow_session.rollback()

    assert lost is None, "a worker that lost the race must not receive a job"
    assert queue.get(job_id).lease_owner == "worker-fast"
