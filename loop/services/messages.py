"""Free text in, truthful outcome out (T04–T07).

`interpret.py` shipped complete and connected to nothing. It could tell a
reminder from a fact, spot a vague time, phrase the one question worth asking
and resolve an answer onto the *existing* task — and no code path ever called
it, so every interface only understood explicit commands.

This is the service that calls it, and the whole of its job is to keep two
promises the interpreter can only state:

* **Nothing is claimed that did not happen.** "Remind me later to call" has no
  time in it. The task is saved, the question is asked, and the reply does not
  say a reminder is set — `Interpretation.may_claim_scheduled` decides that,
  not the wording of a template.
* **An answer completes the waiting task rather than making a second one.**
  Treating "tomorrow at 10" as a fresh request is how one dentist appointment
  becomes two entries, and the duplicate outlives the correction.

**Pending clarifications are durable.** The gap between "remind me later" and
the answer is human-shaped — minutes, or the next morning — so it certainly
outlives a deploy. Holding it in memory would silently turn the answer into a
second task, which is precisely the failure the resolution logic exists to
prevent.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.ids import new_id
from loop.runtime.interpret import (
    IntentKind,
    Interpretation,
    PlanStatus,
    TimingHint,
    interpret,
    resolve_clarification,
)
from loop.runtime.triggers import resolve_local_time
from loop.services.reminders import ReminderService

logger = logging.getLogger(__name__)


@dataclass
class MessageOutcome:
    """What one message actually produced."""

    reply: str
    interpretation: Interpretation
    task_id: str = ""
    capture_path: str = ""
    scheduled_for: dt.datetime | None = None
    awaiting_answer: bool = False
    questions: list[str] = field(default_factory=list)

    @property
    def created_anything(self) -> bool:
        return bool(self.task_id or self.capture_path)


def _to_instant(timing: TimingHint, *, now: dt.datetime, timezone: str
                ) -> dt.datetime | None:
    """Turn a concrete hint into an instant, or None if it is not concrete.

    Never guesses the missing half. A wall time with no day, or a day with no
    wall time, stays unresolved and becomes a question — picking "today" for
    the first or "09:00" for the second would schedule something the owner did
    not ask for, at a time they never said.
    """
    if not timing.is_concrete or timing.wall_time is None:
        return None

    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)

    if timing.day_offset is not None:
        local_date = local_now.date() + dt.timedelta(days=timing.day_offset)
    else:
        weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday",
                    "saturday", "sunday")
        wanted = weekdays.index(timing.weekday)
        ahead = (wanted - local_now.weekday()) % 7
        # "Friday at 10" said on a Friday afternoon means *next* Friday, not a
        # time that has already passed today.
        if ahead == 0 and local_now.time() >= timing.wall_time:
            ahead = 7
        local_date = local_now.date() + dt.timedelta(days=ahead)

    instant, _rule = resolve_local_time(local_date, timing.wall_time, timezone)
    return instant


class PendingClarifications:
    """Questions waiting on an answer, per actor. Durable by necessity."""

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS pending_clarifications ("
                " id TEXT PRIMARY KEY,"
                " actor TEXT NOT NULL UNIQUE,"
                " task_id TEXT NOT NULL,"
                " interpretation_json TEXT NOT NULL,"
                " created_at INTEGER NOT NULL)"))
            session.commit()

    def put(self, actor: str, *, task_id: str,
            interpretation: Interpretation) -> None:
        payload = {
            "kind": interpretation.kind.value,
            "task_title": interpretation.task_title,
            "capture_body": interpretation.capture_body,
            "timing": {
                "wall_time": (interpretation.timing.wall_time.isoformat()
                              if interpretation.timing.wall_time else None),
                "weekday": interpretation.timing.weekday,
                "day_offset": interpretation.timing.day_offset,
            },
            "questions": list(interpretation.questions),
        }
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO pending_clarifications"
                " (id, actor, task_id, interpretation_json, created_at)"
                " VALUES (:id, :actor, :task, :payload, :now)"
                " ON CONFLICT(actor) DO UPDATE SET"
                " task_id = excluded.task_id,"
                " interpretation_json = excluded.interpretation_json,"
                " created_at = excluded.created_at"), {
                    "id": new_id(), "actor": actor, "task": task_id,
                    "payload": json.dumps(payload, sort_keys=True),
                    "now": to_micros(self._clock.now())})
            session.commit()

    def get(self, actor: str) -> tuple[str, Interpretation] | None:
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT task_id, interpretation_json FROM"
                " pending_clarifications WHERE actor = :actor"),
                {"actor": actor}).first()
        if row is None:
            return None
        payload: dict[str, Any] = json.loads(row[1])
        timing = payload.get("timing") or {}
        wall = timing.get("wall_time")
        return row[0], Interpretation(
            kind=IntentKind(payload["kind"]),
            plan_status=PlanStatus.NEEDS_INPUT,
            task_title=payload.get("task_title", ""),
            capture_body=payload.get("capture_body", ""),
            timing=TimingHint(
                wall_time=dt.time.fromisoformat(wall) if wall else None,
                weekday=timing.get("weekday", ""),
                day_offset=timing.get("day_offset")),
            questions=list(payload.get("questions") or []))

    def clear(self, actor: str) -> None:
        with self._sessions() as session:
            session.execute(
                text("DELETE FROM pending_clarifications WHERE actor = :actor"),
                {"actor": actor})
            session.commit()


class MessageService:
    """Routes one free-text message to tasks, captures and questions."""

    def __init__(self, *, tasks: Any, reminders: ReminderService,
                 pending: PendingClarifications, knowledge: Any = None,
                 clock: Clock | None = None,
                 timezone: str = "UTC") -> None:
        self.tasks = tasks
        self.reminders = reminders
        self.pending = pending
        self.knowledge = knowledge
        self._clock = clock or SystemClock()
        self.timezone = timezone

    def handle(self, message: str, *, actor: str = "owner") -> MessageOutcome:
        """Interpret one message, act on it, and reply only to what happened."""
        if not message.strip():
            return MessageOutcome(reply="There was nothing in that message.",
                                  interpretation=Interpretation(
                                      IntentKind.UNKNOWN, PlanStatus.NEEDS_INPUT))

        understood = interpret(message, now=self._clock.now())
        waiting = self.pending.get(actor)

        # An open question does not swallow the next thing the owner says.
        # "Remember that production takes six weeks" is a fact, not an answer
        # to "when should I remind you?" — and treating it as one would both
        # lose the fact and resolve the question with nonsense. Only a message
        # that is *not* an explicit new request is read as the answer, which
        # is exactly what `interpret` reports as UNKNOWN ("tomorrow at 10",
        # "sometime soon"). The question stays open either way.
        if waiting is not None and understood.kind is IntentKind.UNKNOWN:
            return self._answer(waiting, message, actor=actor)

        return self._act(understood, message, actor=actor)

    # ------------------------------------------------------------------ #
    # Answering an outstanding question (T05)
    # ------------------------------------------------------------------ #
    def _answer(self, waiting: tuple[str, Interpretation], answer: str, *,
                actor: str) -> MessageOutcome:
        task_id, pending = waiting
        resolved = resolve_clarification(pending, answer, now=self._clock.now())

        if resolved.plan_status is not PlanStatus.READY:
            # Still not a time. The question stays open on the same task
            # rather than the answer becoming a second one.
            return MessageOutcome(
                reply=" ".join(resolved.questions) or "I still need a time.",
                interpretation=resolved, task_id=task_id,
                awaiting_answer=True, questions=list(resolved.questions))

        instant = _to_instant(resolved.timing, now=self._clock.now(),
                              timezone=self.timezone)
        if instant is None:
            return MessageOutcome(
                reply="I still need a day and a time for that.",
                interpretation=resolved, task_id=task_id,
                awaiting_answer=True)

        self.reminders.attach(task_id, instant=instant,
                              timezone=self.timezone)
        self.pending.clear(actor)
        local = instant.astimezone(ZoneInfo(self.timezone))
        return MessageOutcome(
            reply=f"I'll remind you at {local:%Y-%m-%d %H:%M} — "
                  f"{resolved.task_title}.",
            interpretation=resolved, task_id=task_id,
            scheduled_for=instant)

    # ------------------------------------------------------------------ #
    # Acting on a fresh message (T04, T06, T07)
    # ------------------------------------------------------------------ #
    def _act(self, understood: Interpretation, original: str, *,
             actor: str) -> MessageOutcome:
        capture_path = ""
        task_id = ""
        scheduled: dt.datetime | None = None

        if understood.creates_capture and self.knowledge is not None:
            # The fact is preserved exactly as written, not as interpreted.
            result = self.knowledge.capture(understood.capture_body or original,
                                            origin=actor)
            capture_path = result.path

        if understood.creates_task:
            instant = _to_instant(understood.timing, now=self._clock.now(),
                                  timezone=self.timezone)
            if instant is not None:
                reminder = self.reminders.schedule(
                    understood.task_title, instant=instant,
                    timezone=self.timezone, original_local=original)
                task_id, scheduled = reminder.task.id, instant
            else:
                task = self.tasks.create(understood.task_title, owner=actor)
                task_id = task.id
                if understood.questions:
                    self.pending.put(actor, task_id=task_id,
                                     interpretation=understood)

        return MessageOutcome(
            reply=self._reply(understood, capture_path=capture_path,
                              scheduled=scheduled),
            interpretation=understood, task_id=task_id,
            capture_path=capture_path, scheduled_for=scheduled,
            awaiting_answer=bool(understood.questions and scheduled is None),
            questions=list(understood.questions))

    def _reply(self, understood: Interpretation, *, capture_path: str,
               scheduled: dt.datetime | None) -> str:
        """Say what happened. Each half reports its own outcome (T07)."""
        parts: list[str] = []
        if capture_path:
            parts.append(f"Saved to your vault: {capture_path}.")
        elif understood.creates_capture:
            parts.append("Noted, but no vault is configured, so it is not "
                         "saved yet.")

        if understood.creates_task:
            if scheduled is not None:
                local = scheduled.astimezone(ZoneInfo(self.timezone))
                parts.append(f"I'll remind you at {local:%Y-%m-%d %H:%M} — "
                             f"{understood.task_title}.")
            else:
                parts.append(f"Saved as a task: {understood.task_title}. "
                             "It has no reminder time yet.")
        parts.extend(understood.questions)
        return " ".join(parts) or "I did not understand that."
