"""Task Specialist — deadlines, capture, and end-of-day summaries.

Tracks tasks and deadlines, captures tasks mentioned in chat ("remind me to
call Jean tomorrow") into local SQLite, and (Phase 2) syncs with Wrike once an
API key is available.

Phase 1 scope:
    - Capture free-text tasks from chat into the MemoryStore.
    - List tasks due today / overdue for the morning briefing.
    - Compose an end-of-day summary of open items.

Wrike read/create sync is Phase 2; bidirectional sync is Phase 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from config.settings import Settings, get_settings


@dataclass
class Task:
    """A tracked task/todo item."""

    id: str
    title: str
    due: date | None = None
    source: str = "chat"  # chat | wrike
    done: bool = False


class TaskSpecialist:
    """Focused sub-agent for tasks and deadlines."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # TODO(phase1): accept the MemoryStore; TODO(phase2): accept Wrike connector.

    def capture_from_text(self, text: str) -> Task:
        """Parse a natural-language task from chat and persist it."""
        # TODO(phase1): extract title + due date via LLMRouter; save to SQLite.
        raise NotImplementedError("TaskSpecialist.capture_from_text is a Phase 1 stub.")

    def due_today(self) -> list[Task]:
        """Return tasks due today or overdue."""
        # TODO(phase1): query MemoryStore for due<=today and not done.
        raise NotImplementedError("TaskSpecialist.due_today is a Phase 1 stub.")

    def end_of_day_summary(self) -> str:
        """Compose the end-of-day summary of open items."""
        # TODO(phase1): assemble open tasks into a chat-ready summary.
        raise NotImplementedError("TaskSpecialist.end_of_day_summary is a Phase 1 stub.")
