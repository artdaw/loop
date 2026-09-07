"""Public reminder operations over tasks, triggers, and delivery state.

The three records are one commitment.  A task without its trigger is not a
scheduled reminder, and completing a task without cancelling its future trigger
and queued notification leaves a stale message able to escape after restart.
This service owns those transaction boundaries for every interface.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from loop.core.privacy import PrivacyLabel
from loop.runtime.outbox import NotificationOutbox
from loop.runtime.triggers import Trigger, TriggerService
from loop.services.tasks import OWNER, Task, TaskService


@dataclass(frozen=True)
class ScheduledReminder:
    task: Task
    trigger: Trigger


class ReminderService:
    """Schedule and resolve reminders through one durable transaction."""

    def __init__(self, *, sessions: sessionmaker[Session], tasks: TaskService,
                 triggers: TriggerService, outbox: NotificationOutbox) -> None:
        self._sessions = sessions
        self.tasks = tasks
        self.triggers = triggers
        self.outbox = outbox

    def schedule(self, title: str, *, instant: dt.datetime, timezone: str,
                 original_local: str | None = None, owner: str = OWNER,
                 original_event_id: str | None = None,
                 privacy: PrivacyLabel | None = None) -> ScheduledReminder:
        if instant.tzinfo is None:
            raise ValueError("A reminder instant must include a timezone.")
        with self._sessions() as session:
            task = self.tasks.create(
                title, owner=owner, timezone=timezone,
                original_event_id=original_event_id, privacy=privacy,
                session=session)
            trigger = self.triggers.create_at(
                subject_type="task", subject_id=task.id, instant=instant,
                timezone=timezone, original_local=original_local,
                privacy=privacy, session=session)
            session.commit()
        return ScheduledReminder(task=task, trigger=trigger)

    def complete(self, task_id: str, *, expected_version: int) -> Task:
        """Complete a task and suppress every unsent reminder atomically."""
        with self._sessions() as session:
            task = self.tasks.complete(
                task_id, expected_version=expected_version, session=session)
            self.triggers.disable_for_subject("task", task_id, session=session)
            self.outbox.cancel_for_subject(f"task:{task_id}", session=session)
            session.commit()
        return task

    def cancel(self, task_id: str, *, expected_version: int) -> Task:
        with self._sessions() as session:
            task = self.tasks.cancel(
                task_id, expected_version=expected_version, session=session)
            self.triggers.disable_for_subject("task", task_id, session=session)
            self.outbox.cancel_for_subject(f"task:{task_id}", session=session)
            session.commit()
        return task
