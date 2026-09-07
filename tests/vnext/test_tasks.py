"""Task service and lifecycle (runtime §6).

Covers acceptance T03, T06, T11, T12, T13, T14.
"""

from __future__ import annotations

import pytest

from loop.core.errors import Conflict, InvalidInput, NotFound
from loop.services.tasks import TaskService


@pytest.fixture
def tasks(sessions, clock) -> TaskService:
    return TaskService(sessions=sessions, clock=clock)


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #
def test_a_task_is_stored_and_readable(tasks):
    created = tasks.create("Call the repair shop")
    assert tasks.get(created.id).title == "Call the repair shop"


def test_a_new_task_starts_at_version_one(tasks):
    assert tasks.create("x").version == 1


def test_an_empty_title_is_rejected(tasks):
    with pytest.raises(InvalidInput):
        tasks.create("   ")


def test_an_unknown_priority_is_rejected(tasks):
    with pytest.raises(InvalidInput):
        tasks.create("x", priority="urgent")


# --------------------------------------------------------------------------- #
# T03 — a task without timing is still a task
# --------------------------------------------------------------------------- #
def test_t03_a_task_without_a_date_is_retained(tasks):
    task = tasks.create("Find a better approach to insurance")

    stored = tasks.get(task.id)
    assert stored.status == "ready"
    assert stored.due_date is None
    assert stored.due_at is None


def test_t03_no_due_date_is_invented(tasks):
    """The failure mode here is helpfully guessing a deadline nobody set."""
    task = tasks.get(tasks.create("Investigate insurance").id)
    assert (task.due_date, task.due_at) == (None, None)


def test_t13_an_unautomatable_task_is_still_stored(tasks):
    """T13: an unsupported execution request keeps the task and records the gap."""
    task = tasks.create("Book the flight via an unavailable adapter",
                        context={"missing_capability": "travel.book"})

    stored = tasks.get(task.id)
    assert stored.is_open is True
    assert stored.context["missing_capability"] == "travel.book"


# --------------------------------------------------------------------------- #
# Due representation
# --------------------------------------------------------------------------- #
def test_a_day_level_obligation_stays_a_date(tasks):
    task = tasks.get(tasks.create("Submit the form", due_date="2026-09-10").id)
    assert task.due_date == "2026-09-10"
    assert task.due_at is None


def test_both_due_representations_at_once_is_rejected(tasks):
    """Runtime §6: they are mutually exclusive."""
    with pytest.raises(InvalidInput):
        tasks.create("x", due_date="2026-09-10", due_at=123456)


def test_update_cannot_introduce_both_representations(tasks):
    task = tasks.create("x", due_at=123456)
    with pytest.raises(InvalidInput):
        tasks.update(task.id, expected_version=1, due_date="2026-09-10")


# --------------------------------------------------------------------------- #
# T06 — task intent versus knowledge capture
# --------------------------------------------------------------------------- #
def test_t06_a_task_with_unresolved_timing_can_start_in_inbox(tasks):
    """"Remind me later to call" keeps the task while timing stays open."""
    task = tasks.create("Call", status="inbox",
                        context={"needs_input": ["reminder_time"]})

    stored = tasks.get(task.id)
    assert stored.status == "inbox"
    assert stored.context["needs_input"] == ["reminder_time"]


# --------------------------------------------------------------------------- #
# T12 — ownership
# --------------------------------------------------------------------------- #
def test_t12_an_extracted_action_keeps_its_owner(tasks):
    task = tasks.create("Anna sends the revised quote", owner="anna@acme.com")

    stored = tasks.get(task.id)
    assert stored.owner == "anna@acme.com"
    assert stored.is_mine is False


def test_t12_another_persons_action_is_not_in_the_owners_list(tasks):
    tasks.create("Mine to do")
    tasks.create("Anna's action", owner="anna@acme.com")

    mine = tasks.list(owner="owner")
    assert [t.title for t in mine] == ["Mine to do"]


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def test_completing_a_task_stamps_it(tasks):
    task = tasks.create("x")
    done = tasks.complete(task.id, expected_version=1)

    assert done.status == "done"
    assert done.completed_at is not None
    assert done.version == 2


def test_cancelling_stamps_cancelled_at(tasks):
    task = tasks.create("x")
    cancelled = tasks.cancel(task.id, expected_version=1)

    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_at is not None


def test_any_nonterminal_task_may_be_completed_explicitly(tasks):
    for status in ("inbox", "ready", "in_progress", "waiting", "blocked"):
        task = tasks.create(f"task {status}", status=status)
        assert tasks.complete(task.id, expected_version=1).status == "done"


def test_an_illegal_transition_is_rejected(tasks):
    task = tasks.create("x", status="ready")
    tasks.complete(task.id, expected_version=1)

    with pytest.raises(InvalidInput):
        tasks.update(task.id, expected_version=2, status="waiting")


# --------------------------------------------------------------------------- #
# T11 — reopening
# --------------------------------------------------------------------------- #
def test_t11_reopening_requires_an_explicit_action_and_clears_stamps(tasks):
    task = tasks.create("x")
    tasks.complete(task.id, expected_version=1)

    reopened = tasks.reopen(task.id, expected_version=2)

    assert reopened.status == "ready"
    assert reopened.completed_at is None
    assert reopened.version == 3


def test_t11_a_completed_task_does_not_reopen_by_itself(tasks):
    task = tasks.create("x")
    tasks.complete(task.id, expected_version=1)
    assert tasks.get(task.id).status == "done"


def test_t11_a_reopened_task_is_versioned_not_resurrected_in_place(tasks):
    """The version increments so stale references cannot act on it blindly."""
    task = tasks.create("x")
    tasks.complete(task.id, expected_version=1)
    reopened = tasks.reopen(task.id, expected_version=2)
    assert reopened.version > 1


# --------------------------------------------------------------------------- #
# T14 — optimistic concurrency
# --------------------------------------------------------------------------- #
def test_t14_first_writer_commits(tasks):
    task = tasks.create("x")
    updated = tasks.update(task.id, expected_version=1, title="first")
    assert updated.title == "first"


def test_t14_second_writer_with_the_same_expected_version_conflicts(tasks):
    task = tasks.create("x")
    tasks.update(task.id, expected_version=1, title="first")

    with pytest.raises(Conflict) as excinfo:
        tasks.update(task.id, expected_version=1, title="second")

    assert excinfo.value.http_status == 409


def test_t14_the_conflicting_write_does_not_overwrite(tasks):
    task = tasks.create("x")
    tasks.update(task.id, expected_version=1, title="first")

    with pytest.raises(Conflict):
        tasks.update(task.id, expected_version=1, title="second")

    assert tasks.get(task.id).title == "first"


def test_t14_conflict_reports_both_versions(tasks):
    task = tasks.create("x")
    tasks.update(task.id, expected_version=1, title="first")

    with pytest.raises(Conflict) as excinfo:
        tasks.update(task.id, expected_version=1, title="second")

    assert excinfo.value.details["current_version"] == 2
    assert excinfo.value.details["expected_version"] == 1


def test_updating_an_unknown_field_is_rejected(tasks):
    task = tasks.create("x")
    with pytest.raises(InvalidInput):
        tasks.update(task.id, expected_version=1, nonsense="value")


def test_updating_a_missing_task_is_rejected(tasks):
    """`NotFound`, not `InvalidInput`: the request was well formed (O08).

    The distinction is what a client acts on. A malformed request is worth
    correcting and retrying; a missing id is not, and a caller that cannot
    tell them apart either retries forever or abandons a typo.
    """
    with pytest.raises(NotFound):
        tasks.update("no-such-task", expected_version=1, title="x")


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def test_open_only_excludes_terminal_tasks(tasks):
    keep = tasks.create("open one")
    done = tasks.create("finished")
    tasks.complete(done.id, expected_version=1)

    assert [t.id for t in tasks.list(open_only=True)] == [keep.id]


def test_listing_by_status(tasks):
    tasks.create("a", status="inbox")
    tasks.create("b", status="ready")

    assert len(tasks.list(status="inbox")) == 1
