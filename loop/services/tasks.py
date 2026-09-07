"""Task service — durable commitments (runtime §6).

The lifecycle is deliberately stable while the *content* is open-ended: a chore,
a research objective, a waiting-for obligation and a vague "investigate better
insurance" all share these transitions. That is why there is no task-type enum.

Invariants enforced here rather than trusted from callers:

* **Every accepted task is stored**, even with no date and no way to execute it.
  A task Loop cannot automate is still a commitment it must not lose (T03, T13).
* **`due_date` and `due_at` are mutually exclusive.** A day-level obligation is
  never silently promoted to an instant (runtime §6).
* **Updates require `expected_version`.** A mismatch is a conflict, never
  last-write-wins (T14).
* **Only the listed transitions are legal**, and `done`/`cancelled` reopen only
  by explicit user action (T11).
* **Extracted actions keep their owner.** A meeting action belonging to someone
  else is never silently assigned to the user (T12).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.errors import Conflict, InvalidInput, NotFound
from loop.core.ids import new_id
from loop.core.privacy import PrivacyLabel

logger = logging.getLogger(__name__)

#: Lifecycle transitions (runtime §6). Terminal states reopen only to `ready`,
#: and only through an explicit user action.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "inbox": frozenset({"ready", "blocked", "cancelled", "done"}),
    "ready": frozenset({"in_progress", "waiting", "blocked", "done", "cancelled"}),
    "in_progress": frozenset({"ready", "waiting", "blocked", "done", "cancelled"}),
    "waiting": frozenset({"ready", "in_progress", "done", "cancelled"}),
    "blocked": frozenset({"ready", "in_progress", "done", "cancelled"}),
    "done": frozenset({"ready"}),
    "cancelled": frozenset({"ready"}),
}

TERMINAL_STATES = frozenset({"done", "cancelled"})
VALID_PRIORITIES = frozenset({"low", "normal", "high"})
OWNER = "owner"


@dataclass
class Task:
    """A durable commitment, detached from the session."""

    id: str
    title: str
    status: str
    priority: str
    owner: str
    version: int
    timezone: str
    description: str = ""
    due_date: str | None = None
    due_at: int | None = None
    project_ref: str | None = None
    waiting_on: str | None = None
    blocked_reason: str | None = None
    parent_task_id: str | None = None
    original_event_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    completed_at: int | None = None
    cancelled_at: int | None = None
    privacy: PrivacyLabel = field(default_factory=PrivacyLabel.for_unlabelled_import)

    @property
    def is_open(self) -> bool:
        return self.status not in TERMINAL_STATES

    @property
    def is_mine(self) -> bool:
        return self.owner == OWNER


_COLUMNS = (
    "id, version, title, description, original_event_id, owner, status, priority, "
    "project_ref, due_date, due_at, timezone, blocked_reason, waiting_on, "
    "parent_task_id, context_json, completed_at, cancelled_at, privacy"
)


def _row_to_task(row: Any) -> Task:
    return Task(
        id=row[0], version=row[1], title=row[2], description=row[3],
        original_event_id=row[4], owner=row[5], status=row[6], priority=row[7],
        project_ref=row[8], due_date=row[9], due_at=row[10], timezone=row[11],
        blocked_reason=row[12], waiting_on=row[13], parent_task_id=row[14],
        context=json.loads(row[15] or "{}"), completed_at=row[16],
        cancelled_at=row[17],
        privacy=PrivacyLabel.from_json(json.loads(row[18] or "{}")),
    )


class TaskService:
    """Creates and mutates tasks under the lifecycle rules."""

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None,
                 default_timezone: str = "Europe/Berlin") -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._default_timezone = default_timezone

    # ------------------------------------------------------------------ #
    # Creation
    # ------------------------------------------------------------------ #
    def create(self, title: str, *, status: str = "ready",
               description: str = "", priority: str = "normal",
               due_date: str | None = None, due_at: int | None = None,
               timezone: str | None = None, owner: str = OWNER,
               project_ref: str | None = None, waiting_on: str | None = None,
               parent_task_id: str | None = None,
               original_event_id: str | None = None,
               context: dict[str, Any] | None = None,
               privacy: PrivacyLabel | None = None,
               session: Session | None = None) -> Task:
        """Store a task.

        ``status`` defaults to ``ready``; callers pass ``inbox`` when details
        remain unresolved (runtime §6). A task with neither date is normal, not
        an error — it simply has no reminder.
        """
        title = (title or "").strip()
        if not title:
            raise InvalidInput("A task needs a title.")
        if due_date and due_at is not None:
            # Storing both would leave two competing answers to "when is this
            # due", and every later reader would have to guess which wins.
            raise InvalidInput(
                "A task has either a due_date (a day) or a due_at (an instant), "
                "never both.")
        if priority not in VALID_PRIORITIES:
            raise InvalidInput(f"priority must be one of {sorted(VALID_PRIORITIES)}")
        if status not in ALLOWED_TRANSITIONS:
            raise InvalidInput(f"unknown status {status!r}")

        now = to_micros(self._clock.now())
        task = Task(
            id=new_id(), title=title, description=description, status=status,
            priority=priority, owner=owner, version=1,
            timezone=timezone or self._default_timezone,
            due_date=due_date, due_at=due_at, project_ref=project_ref,
            waiting_on=waiting_on, parent_task_id=parent_task_id,
            original_event_id=original_event_id, context=context or {},
            privacy=privacy or PrivacyLabel.for_unlabelled_import(),
        )

        params = {
            "id": task.id, "now": now, "title": task.title,
            "description": task.description,
            "original_event_id": task.original_event_id, "owner": task.owner,
            "status": task.status, "priority": task.priority,
            "project_ref": task.project_ref, "due_date": task.due_date,
            "due_at": task.due_at, "timezone": task.timezone,
            "waiting_on": task.waiting_on, "parent_task_id": task.parent_task_id,
            "context": json.dumps(task.context, sort_keys=True),
            "privacy": json.dumps(task.privacy.to_json()),
        }
        statement = text(
            "INSERT INTO tasks (id, version, created_at, updated_at, title, "
            "description, original_event_id, owner, status, priority, "
            "project_ref, due_date, due_at, timezone, blocked_reason, "
            "waiting_on, parent_task_id, context_json, completed_at, "
            "cancelled_at, privacy) VALUES (:id, 1, :now, :now, :title, "
            ":description, :original_event_id, :owner, :status, :priority, "
            ":project_ref, :due_date, :due_at, :timezone, NULL, :waiting_on, "
            ":parent_task_id, :context, NULL, NULL, :privacy)")

        if session is not None:
            # Sharing the caller's session lets task + trigger commit atomically
            # (runtime §6: "an explicit reminder creates task + trigger in one
            # DB transaction").
            session.execute(statement, params)
        else:
            with self._sessions() as own:
                own.execute(statement, params)
                own.commit()
        return task

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def get(self, task_id: str) -> Task | None:
        with self._sessions() as session:
            row = session.execute(
                text(f"SELECT {_COLUMNS} FROM tasks WHERE id = :id"),
                {"id": task_id}).first()
        return _row_to_task(row) if row else None

    def list(self, *, status: str | None = None, owner: str | None = OWNER,
             open_only: bool = False) -> list[Task]:
        clauses, params = [], {}
        if status:
            clauses.append("status = :status")
            params["status"] = status
        if owner:
            clauses.append("owner = :owner")
            params["owner"] = owner
        if open_only:
            clauses.append("status NOT IN ('done', 'cancelled')")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._sessions() as session:
            rows = session.execute(
                text(f"SELECT {_COLUMNS} FROM tasks{where} ORDER BY created_at"),
                params).all()
        return [_row_to_task(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Mutation
    # ------------------------------------------------------------------ #
    def update(self, task_id: str, *, expected_version: int,
               session: Session | None = None, **changes: Any) -> Task:
        """Apply changes under optimistic concurrency control.

        ``expected_version`` is required: two callers editing from the same
        snapshot must not silently overwrite each other (T14).
        """
        allowed = {"title", "description", "priority", "due_date", "due_at",
                   "project_ref", "waiting_on", "blocked_reason", "status",
                   "timezone", "parent_task_id"}
        unknown = set(changes) - allowed
        if unknown:
            raise InvalidInput(f"unknown task fields: {sorted(unknown)}")

        def _apply(active: Session) -> Task:
            current = active.execute(
                text(f"SELECT {_COLUMNS} FROM tasks WHERE id = :id"),
                {"id": task_id}).first()
            if current is None:
                raise NotFound(f"No task with id {task_id!r}.")
            task = _row_to_task(current)

            if task.version != expected_version:
                raise Conflict(
                    "Task was modified by someone else.",
                    details={"task_id": task_id, "expected_version":
                             expected_version, "current_version": task.version},
                )

            new_status = changes.get("status", task.status)
            if new_status != task.status:
                self._check_transition(task.status, new_status)

            if "due_date" in changes and changes["due_date"] and (
                    changes.get("due_at", task.due_at) is not None):
                raise InvalidInput("A task has either a due_date or a due_at.")

            now = to_micros(self._clock.now())
            assignments = ["version = version + 1", "updated_at = :now"]
            params: dict[str, Any] = {"id": task_id, "now": now}

            for key, value in changes.items():
                assignments.append(f"{key} = :{key}")
                params[key] = value

            if new_status == "done" and task.status != "done":
                assignments.append("completed_at = :now")
            elif new_status == "cancelled" and task.status != "cancelled":
                assignments.append("cancelled_at = :now")
            elif new_status == "ready" and task.status in TERMINAL_STATES:
                # An explicit reopen clears the terminal stamps so the task
                # reads as genuinely open again.
                assignments += ["completed_at = NULL", "cancelled_at = NULL"]

            active.execute(
                text(f"UPDATE tasks SET {', '.join(assignments)} WHERE id = :id"),
                params)

            refreshed = active.execute(
                text(f"SELECT {_COLUMNS} FROM tasks WHERE id = :id"),
                {"id": task_id}).one()
            return _row_to_task(refreshed)

        if session is not None:
            return _apply(session)
        with self._sessions() as own:
            result = _apply(own)
            own.commit()
            return result

    def complete(self, task_id: str, *, expected_version: int,
                 session: Session | None = None) -> Task:
        """Mark a task done. Any nonterminal task may be completed explicitly."""
        return self.update(task_id, expected_version=expected_version,
                           status="done", session=session)

    def cancel(self, task_id: str, *, expected_version: int,
               session: Session | None = None) -> Task:
        return self.update(task_id, expected_version=expected_version,
                           status="cancelled", session=session)

    def reopen(self, task_id: str, *, expected_version: int,
               session: Session | None = None) -> Task:
        """Reopen a terminal task to ``ready`` by explicit user action (T11)."""
        return self.update(task_id, expected_version=expected_version,
                           status="ready", session=session)

    # ------------------------------------------------------------------ #
    # Rules
    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_transition(current: str, target: str) -> None:
        allowed: Iterable[str] = ALLOWED_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise InvalidInput(
                f"Cannot move a task from {current!r} to {target!r}.",
                details={"from": current, "to": target,
                         "allowed": sorted(allowed)},
            )
