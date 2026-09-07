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
    #: Optional: when present, each reminder carries opaque button ids so the
    #: delivered message can offer Done / Snooze / Dismiss (T09, T10).
    actions: Any = None
    owner: str = "owner"

    def __call__(self, trigger: Any, decision: str, occurrence_key: str,
                 session: Session | None = None) -> str | None:
        del decision
        if getattr(trigger, "subject_type", "") != "task":
            return None
        task = self.tasks.get(str(trigger.subject_id))
        if task is None or not task.is_open:
            return None
        payload: dict[str, Any] = {"text": task.title, "task_id": task.id}
        if self.actions is not None:
            from loop.services.actions import ActionKind

            # Opaque ids only. Encoding the task and the action into the button
            # would let an edited or replayed callback act on something else.
            payload["actions"] = self.actions.issue(
                kinds=[ActionKind.DONE, ActionKind.SNOOZE, ActionKind.DISMISS],
                subject_ref=f"task:{task.id}", task_id=task.id,
                occurrence_key=occurrence_key, actor=self.owner,
                session=session)

        return self.outbox.enqueue(
            category="reminder", subject_ref=f"task:{task.id}",
            occurrence_key=occurrence_key,
            destination_id=self.destination_id,
            payload=payload, session=session)
