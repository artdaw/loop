"""Daily briefing — what today looks like.

Backs ``loop briefing`` and the 08:00 morning message. Split the same way as
:mod:`specialists.review`: :meth:`DailyBriefing.collect` gathers data,
:meth:`DailyBriefing.compose` renders it, so the content is testable without a
terminal or a delivery channel.

Tasks and follow-ups come from local SQLite and always work. Meetings need a
calendar connector, and the Gmail/Outlook calendar clients are still Phase 1
stubs — so an absent or failing calendar is reported as a *state* ("calendars
not connected") rather than silently rendering an empty agenda. A briefing that
quietly omits your meetings is worse than one that admits it cannot see them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: How many flagged threads to surface before saying "and N more".
MAX_FOLLOW_UPS = 5
#: How many other open tasks to list.
MAX_OTHER_TASKS = 5


@dataclass
class Agenda:
    """Everything the briefing knows about today."""

    day: date
    meetings: list[Any] = field(default_factory=list)
    calendar_connected: bool = False
    calendar_error: str = ""
    due_today: list[Any] = field(default_factory=list)
    overdue: list[Any] = field(default_factory=list)
    follow_ups: list[Any] = field(default_factory=list)

    @property
    def is_quiet(self) -> bool:
        return not (self.meetings or self.due_today or self.overdue or self.follow_ups)


class DailyBriefing:
    """Builds the morning briefing."""

    def __init__(self, settings: Settings | None = None,
                 memory: Any | None = None,
                 calendar: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._memory = memory
        # Optional CalendarSpecialist. Absent ⇒ the meetings section reports
        # that calendars are not connected.
        self._calendar = calendar

    def _get_memory(self) -> Any:
        if self._memory is None:
            from core.memory import MemoryStore

            self._memory = MemoryStore(self.settings)
            self._memory.bootstrap()
        return self._memory

    # ------------------------------------------------------------------ #
    # Collection
    # ------------------------------------------------------------------ #
    def collect(self, day: date | None = None) -> Agenda:
        """Gather today's meetings, tasks, and flagged threads."""
        today = day or date.today()
        memory = self._get_memory()

        agenda = Agenda(day=today)

        try:
            agenda.due_today = memory.get_due_today(today=today)
            agenda.overdue = memory.get_overdue(today=today)
        except Exception as exc:  # noqa: BLE001 - a briefing must still render
            logger.exception("Could not read tasks for the briefing")
            agenda.calendar_error = agenda.calendar_error or ""
            logger.debug("task read failed: %s", exc)

        try:
            agenda.follow_ups = memory.list_open_follow_ups()
        except Exception:  # noqa: BLE001
            logger.exception("Could not read follow-ups for the briefing")

        agenda.meetings, agenda.calendar_connected, agenda.calendar_error = (
            self._collect_meetings()
        )
        return agenda

    def _collect_meetings(self) -> tuple[list[Any], bool, str]:
        """Fetch today's events. Returns (events, connected, error)."""
        if self._calendar is None:
            return [], False, ""

        try:
            events = asyncio.run(self._calendar.get_all_events_today())
        except NotImplementedError:
            # The Phase 1 calendar connectors are still stubs.
            return [], False, "calendar connectors not implemented yet"
        except Exception as exc:  # noqa: BLE001 - report, never crash the briefing
            logger.exception("Calendar lookup failed during the briefing")
            return [], False, str(exc)

        ordered = sorted(events, key=lambda event: getattr(event, "start", None)
                         or datetime.max)
        return ordered, True, ""

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    def compose(self, agenda: Agenda | None = None) -> str:
        """Render the briefing as a chat/terminal-friendly message."""
        agenda = agenda or self.collect()
        lines = [f"☀️ *Good morning* — {agenda.day.strftime('%A, %d %B %Y')}", ""]

        lines.extend(self._meetings_block(agenda))
        lines.extend(self._follow_ups_block(agenda))
        lines.extend(self._tasks_block(agenda))

        if agenda.is_quiet and agenda.calendar_connected:
            lines.append("Nothing scheduled and nothing outstanding. Enjoy it. 🎉")

        return "\n".join(lines).strip()

    @staticmethod
    def _meetings_block(agenda: Agenda) -> list[str]:
        lines = ["📅 *Meetings*"]
        if not agenda.calendar_connected:
            reason = agenda.calendar_error or (
                "set GMAIL_CREDENTIALS_PATH or OUTLOOK_CLIENT_ID in .env"
            )
            lines.append(f"   Calendars not connected — {reason}")
        elif not agenda.meetings:
            lines.append("   Nothing in the diary today.")
        else:
            for event in agenda.meetings:
                start = getattr(event, "start", None)
                when = start.strftime("%H:%M") if isinstance(start, datetime) else "??:??"
                # Personal events are masked: the briefing may go to a work channel.
                title = ("(private)" if getattr(event, "is_personal", False)
                         else getattr(event, "title", "(untitled)"))
                location = getattr(event, "location", "") or ""
                suffix = f" — {location}" if location else ""
                lines.append(f"   {when}  {title}{suffix}")
        lines.append("")
        return lines

    @staticmethod
    def _follow_ups_block(agenda: Agenda) -> list[str]:
        if not agenda.follow_ups:
            return []
        lines = [f"✉️ *Waiting on a reply* ({len(agenda.follow_ups)})"]
        for item in agenda.follow_ups[:MAX_FOLLOW_UPS]:
            subject = getattr(item, "subject", "") or "(no subject)"
            sender = getattr(item, "sender", "") or ""
            who = f" — {sender}" if sender else ""
            score = getattr(item, "triage_score", 0) or 0
            priority = " ‼️" if score >= 15 else ""
            lines.append(f"   • [{item.id}] {subject}{who}{priority}")
        remaining = len(agenda.follow_ups) - MAX_FOLLOW_UPS
        if remaining > 0:
            lines.append(f"   …and {remaining} more")
        lines.append("")
        return lines

    @staticmethod
    def _tasks_block(agenda: Agenda) -> list[str]:
        lines: list[str] = []
        if agenda.overdue:
            lines.append(f"⚠️ *Overdue* ({len(agenda.overdue)})")
            for task in agenda.overdue:
                due = task.due_date.isoformat() if task.due_date else "no date"
                lines.append(f"   • {task.description}  (due {due})")
            lines.append("")

        if agenda.due_today:
            lines.append(f"📌 *Due today* ({len(agenda.due_today)})")
            for task in agenda.due_today:
                flag = " ‼️" if getattr(task, "priority", "") == "high" else ""
                lines.append(f"   • {task.description}{flag}")
            lines.append("")
        return lines
