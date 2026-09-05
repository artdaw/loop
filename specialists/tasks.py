"""Task Specialist — deadlines, chat capture, and end-of-day summaries.

Captures tasks mentioned in chat ("remind me to call Jean tomorrow") into local
SQLite via the local LLM, lists tasks due today / overdue, and composes an
end-of-day summary. Wrike sync arrives in a later phase.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from config.settings import Settings, get_settings
from core.llm_router import LLMRouter
from core.memory import MemoryStore, Task


@dataclass
class ParsedTask:
    """The structured result of parsing a chat message into a task."""

    description: str
    due_date: date | None = None
    priority: str = "normal"  # low | normal | high


class TaskSpecialist:
    """Focused sub-agent for tasks and deadlines."""

    def __init__(self, settings: Settings | None = None,
                 router: LLMRouter | None = None,
                 memory: MemoryStore | None = None) -> None:
        self.settings = settings or get_settings()
        self.router = router or LLMRouter(self.settings)
        self.memory = memory or MemoryStore(self.settings)
        self.memory.bootstrap()

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #
    def parse_task_from_message(self, message_text: str) -> ParsedTask:
        """Extract task description, due date, and priority via the local LLM."""
        today = date.today()
        prompt = (
            "Extract a single actionable task from the message below. "
            "Return STRICT JSON with keys: "
            "description (string, the task, imperative voice), "
            "due_date (string 'YYYY-MM-DD' or null if none mentioned), "
            "priority (one of 'low', 'normal', 'high').\n"
            f"Today's date is {today.isoformat()}. Resolve relative dates like "
            "'tomorrow' or 'next Monday' against it.\n\n"
            f"Message: {message_text}\n\nJSON:"
        )
        raw = self.router.route(prompt, {"source": "work"})
        parsed = self._extract_json(raw)

        if not parsed:
            return ParsedTask(
                description=message_text.strip(),
                due_date=self._heuristic_due_date(message_text),
                priority=self._heuristic_priority(message_text),
            )

        due = self._coerce_date(parsed.get("due_date"))
        priority = str(parsed.get("priority") or "normal").lower()
        if priority not in {"low", "normal", "high"}:
            priority = "normal"
        return ParsedTask(
            description=str(parsed.get("description") or message_text).strip(),
            due_date=due,
            priority=priority,
        )

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def save_task(self, task: ParsedTask, *, source: str = "chat") -> Task:
        """Persist a parsed task to SQLite and return the stored row."""
        return self.memory.add_task(
            task.description,
            due_date=task.due_date,
            priority=task.priority,
            source=source,
        )

    def capture_from_text(self, text: str, *, source: str = "chat") -> Task:
        """Convenience: parse a chat message and save the resulting task."""
        parsed = self.parse_task_from_message(text)
        return self.save_task(parsed, source=source)

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def get_due_today(self) -> list[Task]:
        """Return tasks due today."""
        return self.memory.get_due_today()

    def get_overdue(self) -> list[Task]:
        """Return overdue tasks."""
        return self.memory.get_overdue()

    def list_open(self) -> list[Task]:
        """Return all open tasks."""
        return self.memory.list_open_tasks()

    # ------------------------------------------------------------------ #
    # Summaries
    # ------------------------------------------------------------------ #
    def end_of_day_summary(self) -> str:
        """Compose an end-of-day summary: open, overdue, and completed today."""
        due_today = self.get_due_today()
        overdue = self.get_overdue()
        completed = self.memory.get_completed_today()
        open_tasks = [t for t in self.list_open() if t not in due_today]

        lines = ["🌙 *End-of-day summary*", ""]

        if overdue:
            lines.append(f"⚠️ *Overdue ({len(overdue)})*")
            lines.extend(f"  • {self._fmt(t)}" for t in overdue)
            lines.append("")

        if due_today:
            lines.append(f"📌 *Due today, still open ({len(due_today)})*")
            lines.extend(f"  • {self._fmt(t)}" for t in due_today)
            lines.append("")

        if open_tasks:
            lines.append(f"📋 *Other open tasks ({len(open_tasks)})*")
            lines.extend(f"  • {self._fmt(t)}" for t in open_tasks[:10])
            lines.append("")

        if completed:
            lines.append(f"✅ *Completed today ({len(completed)})*")
            lines.extend(f"  • {t.description}" for t in completed)
            lines.append("")

        if not (overdue or due_today or open_tasks or completed):
            lines.append("Nothing tracked today — inbox zero for tasks. 🎉")

        return "\n".join(lines).strip()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fmt(task: Task) -> str:
        bits = [task.description]
        if task.due_date:
            bits.append(f"(due {task.due_date.isoformat()})")
        if task.priority == "high":
            bits.append("‼️")
        return " ".join(bits)

    @staticmethod
    def _extract_json(raw: str) -> dict | None:
        if not raw:
            return None
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _coerce_date(value) -> date | None:
        if not value or str(value).lower() in {"null", "none", ""}:
            return None
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    @staticmethod
    def _heuristic_due_date(text: str) -> date | None:
        lowered = text.lower()
        today = date.today()
        if "today" in lowered:
            return today
        if "tomorrow" in lowered:
            return today + timedelta(days=1)
        return None

    @staticmethod
    def _heuristic_priority(text: str) -> str:
        lowered = text.lower()
        if any(w in lowered for w in ("urgent", "asap", "important", "critical")):
            return "high"
        return "normal"
