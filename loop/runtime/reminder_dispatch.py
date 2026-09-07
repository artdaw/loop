"""Deterministic task-trigger to notification dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from loop.runtime.outbox import NotificationOutbox
from loop.services.tasks import TaskService


@dataclass
class TaskReminderDispatcher:
    """Queue one reminder for a fired task occurrence, without a model call."""

    tasks: TaskService
    outbox: NotificationOutbox
    destination_id: str

    def __call__(self, trigger: Any, decision: str, occurrence_key: str,
                 session: Session | None = None) -> str | None:
        del decision
        if getattr(trigger, "subject_type", "") != "task":
            return None
        task = self.tasks.get(str(trigger.subject_id))
        if task is None or not task.is_open:
            return None
        return self.outbox.enqueue(
            category="reminder", subject_ref=f"task:{task.id}",
            occurrence_key=occurrence_key,
            destination_id=self.destination_id,
            payload={"text": task.title, "task_id": task.id},
            session=session)
