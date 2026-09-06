"""Turning a sentence into commitments (runtime §3, T04–T07).

Two distinctions decide everything downstream, and both are easy to get wrong in
the same direction — by producing something confident.

**Task intent versus knowledge capture.** "Remember to call the dentist" is a
thing to do; "remember that production takes six weeks" is a thing that is true.
Filing the second as a task produces a to-do nobody can complete, and filing the
first as a note produces a reminder that never fires. The grammar distinguishes
them reliably enough to act on: an infinitive after "remember" is an action, a
"that"-clause is a fact.

**Saved is not scheduled.** "Remind me later to call" has no time in it. The task
is saved — losing it would be worse — but the plan is `needs_input`, and the
reply must not say a reminder is set. A cheerful "I'll remind you!" for a
reminder that does not exist is the failure this whole path is built to avoid.

One request can be both (T07): "remember this fact and remind me Friday at 10 to
verify it" is a capture *and* a linked task, each with its own truthful result.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class IntentKind(str, Enum):
    TASK = "task"
    CAPTURE = "capture"
    CAPTURE_WITH_TASK = "capture_with_task"
    CLARIFICATION = "clarification"
    UNKNOWN = "unknown"


class PlanStatus(str, Enum):
    READY = "ready"
    NEEDS_INPUT = "needs_input"

    @property
    def is_scheduled(self) -> bool:
        """Only a ready plan may be described as scheduled."""
        return self is PlanStatus.READY


#: Words that mean "at some point", which is not a time.
VAGUE_TIMES = ("later", "sometime", "soon", "at some point", "eventually",
               "in a bit", "one of these days")

_REMEMBER_THAT = re.compile(r"\bremember\s+that\b", re.IGNORECASE)
_REMEMBER_TO = re.compile(r"\bremember\s+to\b", re.IGNORECASE)
_REMIND_ME = re.compile(r"\bremind\s+me\b", re.IGNORECASE)
_NOTE_THAT = re.compile(r"\b(note|save|capture)\s+(that|this)\b", re.IGNORECASE)

#: A concrete clock time, with or without minutes.
_CLOCK = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
                    re.IGNORECASE)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")
_RELATIVE_DAYS = {"today": 0, "tomorrow": 1, "the day after tomorrow": 2}


@dataclass
class TimingHint:
    """What the sentence actually said about when."""

    wall_time: dt.time | None = None
    weekday: str = ""
    day_offset: int | None = None
    vague: str = ""

    @property
    def is_concrete(self) -> bool:
        """A time is concrete only with a clock time *and* a day."""
        return self.wall_time is not None and (
            bool(self.weekday) or self.day_offset is not None)

    @property
    def is_vague(self) -> bool:
        return bool(self.vague)


@dataclass
class Interpretation:
    """What Loop understood, and what it still needs."""

    kind: IntentKind
    plan_status: PlanStatus
    task_title: str = ""
    capture_body: str = ""
    timing: TimingHint = field(default_factory=TimingHint)
    questions: list[str] = field(default_factory=list)

    @property
    def creates_task(self) -> bool:
        return self.kind in (IntentKind.TASK, IntentKind.CAPTURE_WITH_TASK)

    @property
    def creates_capture(self) -> bool:
        return self.kind in (IntentKind.CAPTURE, IntentKind.CAPTURE_WITH_TASK)

    @property
    def may_claim_scheduled(self) -> bool:
        """Whether a reply may say a reminder is set (T04)."""
        return self.creates_task and self.plan_status.is_scheduled

    def acknowledgement(self) -> str:
        """A reply that is true even when something is missing."""
        parts: list[str] = []
        if self.creates_capture:
            parts.append("Saved that.")
        if self.creates_task:
            if self.may_claim_scheduled:
                parts.append(f"I'll remind you: {self.task_title}.")
            else:
                parts.append(f"Saved as a task: {self.task_title}. "
                             f"It has no reminder time yet.")
        parts.extend(self.questions)
        return " ".join(parts) or "I did not understand that."


def parse_timing(text: str, *, now: dt.datetime | None = None) -> TimingHint:
    """Extract a timing hint without inventing one."""
    hint = TimingHint()
    lowered = text.lower()

    for vague in VAGUE_TIMES:
        if re.search(rf"\b{re.escape(vague)}\b", lowered):
            hint.vague = vague
            break

    for name, offset in _RELATIVE_DAYS.items():
        if name in lowered:
            hint.day_offset = offset
            break

    for weekday in _WEEKDAYS:
        if re.search(rf"\b{weekday}\b", lowered):
            hint.weekday = weekday
            break

    match = _CLOCK.search(text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = (match.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            hint.wall_time = dt.time(hour, minute)

    return hint


def _clean(text: str) -> str:
    """Tidy whitespace and drop a dangling conjunction from a split sentence.

    Splitting "remember X and remind me Y" leaves "X and" on the left. The
    trailing conjunction is punctuation of the split, not part of the fact.
    """
    tidied = re.sub(r"\s+", " ", text).strip(" .,;:")
    return re.sub(r"[,;]?\s+(and|then|also)$", "", tidied,
                  flags=re.IGNORECASE).strip(" .,;:")


def interpret(text: str, *, now: dt.datetime | None = None) -> Interpretation:
    """Classify one message (T06, T07)."""
    timing = parse_timing(text, now=now)

    fact_match = _REMEMBER_THAT.search(text) or _NOTE_THAT.search(text)
    action_match = _REMEMBER_TO.search(text) or _REMIND_ME.search(text)

    if fact_match and action_match:
        return _both(text, fact_match, action_match, timing)
    if fact_match:
        body = _clean(text[fact_match.end():])
        # A stated fact is captured exactly as said. Paraphrasing a fact into a
        # tidier sentence is how "six weeks" becomes "about a month".
        return Interpretation(IntentKind.CAPTURE, PlanStatus.READY,
                              capture_body=body)
    if action_match:
        title = _title_from(text, action_match)
        return _task(title, timing)

    return Interpretation(IntentKind.UNKNOWN, PlanStatus.NEEDS_INPUT,
                          questions=["Is that something to do, or something to "
                                     "remember?"])


def _title_from(text: str, match: re.Match[str]) -> str:
    """The action, with the timing words removed but nothing else changed."""
    remainder = text[match.end():]
    remainder = re.sub(r"\b(?:on\s+)?(?:" + "|".join(_WEEKDAYS) + r")\b", "",
                       remainder, flags=re.IGNORECASE)
    for vague in VAGUE_TIMES:
        remainder = re.sub(rf"\b{re.escape(vague)}\b", "", remainder,
                           flags=re.IGNORECASE)
    for name in _RELATIVE_DAYS:
        remainder = re.sub(rf"\b{re.escape(name)}\b", "", remainder,
                           flags=re.IGNORECASE)
    remainder = _CLOCK.sub("", remainder)
    return _clean(remainder.replace(" to ", " ", 1)) or _clean(text)


def _task(title: str, timing: TimingHint) -> Interpretation:
    """A task, ready only when the time is actually known (T04)."""
    if timing.is_concrete:
        return Interpretation(IntentKind.TASK, PlanStatus.READY,
                              task_title=title, timing=timing)

    question = _timing_question(timing)
    return Interpretation(IntentKind.TASK, PlanStatus.NEEDS_INPUT,
                          task_title=title, timing=timing,
                          questions=[question])


def _timing_question(timing: TimingHint) -> str:
    """Exactly one question, naming the part that is missing."""
    if timing.wall_time is not None and not timing.weekday \
            and timing.day_offset is None:
        return f"Which day, at {timing.wall_time:%H:%M}?"
    if timing.wall_time is None and (timing.weekday or timing.day_offset
                                     is not None):
        return "What time that day?"
    return "When should I remind you?"


def _both(text: str, fact: re.Match[str], action: re.Match[str],
          timing: TimingHint) -> Interpretation:
    """A capture and a linked task from one sentence (T07)."""
    if fact.start() < action.start():
        body = _clean(text[fact.end():action.start()])
        title = _title_from(text, action)
    else:
        body = _clean(text[fact.end():])
        title = _title_from(text[:fact.start()], action)

    result = Interpretation(IntentKind.CAPTURE_WITH_TASK,
                            PlanStatus.READY if timing.is_concrete
                            else PlanStatus.NEEDS_INPUT,
                            task_title=title, capture_body=body, timing=timing)
    if not timing.is_concrete:
        result.questions.append(_timing_question(timing))
    return result


def resolve_clarification(pending: Interpretation, answer: str, *,
                          now: dt.datetime | None = None) -> Interpretation:
    """Apply an answer to the waiting task (T05).

    The answer completes the *existing* task rather than creating a second one.
    Treating "tomorrow at 10" as a new request is how one dentist appointment
    becomes two entries, and the duplicate outlives the correction.
    """
    if pending.plan_status is not PlanStatus.NEEDS_INPUT:
        return pending

    supplied = parse_timing(answer, now=now)
    merged = TimingHint(
        wall_time=supplied.wall_time or pending.timing.wall_time,
        weekday=supplied.weekday or pending.timing.weekday,
        day_offset=(supplied.day_offset if supplied.day_offset is not None
                    else pending.timing.day_offset),
        vague="")

    if not merged.is_concrete:
        return Interpretation(pending.kind, PlanStatus.NEEDS_INPUT,
                              task_title=pending.task_title,
                              capture_body=pending.capture_body, timing=merged,
                              questions=[_timing_question(merged)])

    return Interpretation(pending.kind, PlanStatus.READY,
                          task_title=pending.task_title,
                          capture_body=pending.capture_body, timing=merged)
