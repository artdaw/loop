"""Autonomy gate — how far Loop may act on its own.

Loop has two gates, and keeping them apart is deliberate:

===================  ==========================================  ==============
Gate                 Question                                    Failure mode
===================  ==========================================  ==============
``LLMRouter``        *Which model may see this data?*            fails **closed**
``AutonomyGate``     *May Loop do this without asking me?*       fails to **asking**
===================  ==========================================  ==============

They are independent axes. A voice note is private *and* safe to file
automatically; an email to a colleague is not private but must never go out
unattended. Using one as a proxy for the other would make both harder to reason
about, so:

**Invariant:** autonomy MUST NOT influence backend choice, and privacy metadata
MUST NOT influence whether an action may run. No code path may consult one gate
to answer the other's question.

Levels are ordered, so a ceiling is expressible as ``min(level, ceiling)``:

    OBSERVE (0)  record only; never surface proactively
    SUGGEST (1)  surface a suggestion; prepare nothing
    APPROVE (2)  prepare the action, wait for explicit approval  [default]
    ACT     (3)  execute autonomously, then log it

Resolution order for a level, first hit wins:

    1. ``Preference["autonomy.<action>"]``  — per-action runtime override
    2. ``Preference["autonomy.default"]``   — global runtime default
    3. ``settings.default_autonomy_level``  — config default

The resolved level is then capped (see :meth:`AutonomyGate.ceiling_for`).
Unparseable values fall through to the next tier with a warning rather than
raising: a corrupt preference must not brick the assistant.

Usage::

    gate = AutonomyGate()
    decision = gate.decide(ActionType.WRIKE_WRITE)
    if decision.requires_approval:
        ...ask the user...
    gate.record(ActionType.WRIKE_WRITE, level=decision.level,
                executed=True, approved=True, detail="task 42")
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any

from config.settings import Settings, get_settings
from core.exceptions import ApprovalRequiredError

logger = logging.getLogger(__name__)


class AutonomyLevel(enum.IntEnum):
    """How far Loop may go for a given action. Ordered, so ``min()`` caps."""

    OBSERVE = 0
    SUGGEST = 1
    APPROVE = 2
    ACT = 3

    @property
    def label(self) -> str:
        return self.name.lower()


class ActionType(str, enum.Enum):
    """The kinds of action the gate governs."""

    EMAIL_SEND = "email_send"
    TASK_CREATE = "task_create"
    WRIKE_WRITE = "wrike_write"
    NOTE_WRITE = "note_write"
    CALENDAR_WRITE = "calendar_write"
    NOTIFY = "notify"


#: Fallback used when both the preference and the setting are unparseable.
DEFAULT_LEVEL = AutonomyLevel.APPROVE

_PREFERENCE_PREFIX = "autonomy."
_DEFAULT_PREFERENCE_KEY = "autonomy.default"


@dataclass(frozen=True)
class AutonomyDecision:
    """The gate's answer for one action."""

    action: ActionType
    level: AutonomyLevel
    allowed: bool             # may proceed at all (level >= SUGGEST)
    requires_approval: bool   # must not execute without an explicit approval
    reason: str
    capped: bool = False      # True when a ceiling lowered the configured level


def parse_level(value: Any) -> AutonomyLevel | None:
    """Parse a level from a string, int, or AutonomyLevel. ``None`` if invalid."""
    if isinstance(value, AutonomyLevel):
        return value
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        return None
    if isinstance(value, int):
        try:
            return AutonomyLevel(value)
        except ValueError:
            return None
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        for level in AutonomyLevel:
            if level.name.lower() == text:
                return level
        if text.isdigit():
            try:
                return AutonomyLevel(int(text))
            except ValueError:
                return None
    return None


class AutonomyGate:
    """Resolves, enforces, and audits autonomy levels per action type."""

    def __init__(self, settings: Settings | None = None,
                 memory: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._memory = memory

    # ------------------------------------------------------------------ #
    # Collaborators
    # ------------------------------------------------------------------ #
    def _get_memory(self) -> Any:
        """Lazily build a MemoryStore so constructing a gate touches no disk."""
        if self._memory is None:
            from core.memory import MemoryStore

            self._memory = MemoryStore(self.settings)
            self._memory.bootstrap()
        return self._memory

    # ------------------------------------------------------------------ #
    # Level resolution
    # ------------------------------------------------------------------ #
    def level_for(self, action: ActionType) -> AutonomyLevel:
        """Return the configured level for ``action``, before any ceiling."""
        memory = self._get_memory()

        raw = memory.get_preference(f"{_PREFERENCE_PREFIX}{action.value}", "")
        level = parse_level(raw)
        if level is not None:
            return level
        if raw:
            logger.warning("Ignoring unparseable autonomy preference for %s: %r",
                           action.value, raw)

        raw_default = memory.get_preference(_DEFAULT_PREFERENCE_KEY, "")
        level = parse_level(raw_default)
        if level is not None:
            return level
        if raw_default:
            logger.warning("Ignoring unparseable autonomy.default: %r", raw_default)

        level = parse_level(self.settings.default_autonomy_level)
        if level is not None:
            return level
        logger.warning("Ignoring unparseable default_autonomy_level: %r",
                       self.settings.default_autonomy_level)
        return DEFAULT_LEVEL

    def ceiling_for(self, action: ActionType) -> AutonomyLevel:
        """Return the hard ceiling for ``action``.

        Only outbound email has one today. It exists so that reaching full
        autonomy for sending mail takes a deliberate ``.env`` change rather than
        a click in the dashboard — Loop's standing rule is that it never sends
        email unattended unless the user has explicitly opted in.
        """
        if action is ActionType.EMAIL_SEND:
            return parse_level(self.settings.max_autonomy_email_send) or DEFAULT_LEVEL
        return AutonomyLevel.ACT

    def set_level(self, action: ActionType | None,
                  level: AutonomyLevel | str) -> None:
        """Persist a level. ``action=None`` writes the global default."""
        parsed = parse_level(level)
        if parsed is None:
            raise ValueError(f"Not a valid autonomy level: {level!r}")
        key = _DEFAULT_PREFERENCE_KEY if action is None else (
            f"{_PREFERENCE_PREFIX}{action.value}"
        )
        self._get_memory().set_preference(key, parsed.label)

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #
    def decide(self, action: ActionType, *, detail: str = "") -> AutonomyDecision:
        """Return the gate's decision for ``action`` without acting on it."""
        configured = self.level_for(action)
        ceiling = self.ceiling_for(action)
        capped = configured > ceiling
        level = min(configured, ceiling)

        allowed = level >= AutonomyLevel.SUGGEST
        requires_approval = allowed and level <= AutonomyLevel.APPROVE

        if capped:
            reason = (
                f"level {configured.label} lowered to {level.label} by the "
                f"{action.value} ceiling"
            )
        elif not allowed:
            reason = f"{action.value} is set to {level.label}; Loop will only record it"
        elif requires_approval:
            reason = f"{action.value} is set to {level.label}; explicit approval required"
        else:
            reason = f"{action.value} is set to {level.label}; Loop may act autonomously"

        if detail:
            reason = f"{reason} ({detail})"

        return AutonomyDecision(
            action=action,
            level=level,
            allowed=allowed,
            requires_approval=requires_approval,
            reason=reason,
            capped=capped,
        )

    def guard(self, action: ActionType, *, approved: bool = False,
              detail: str = "") -> AutonomyDecision:
        """Raise :class:`ApprovalRequiredError` unless ``action`` may run now.

        For callers that cannot present an approval prompt. Callers that can
        should use :meth:`decide` and branch on ``requires_approval``.
        """
        decision = self.decide(action, detail=detail)
        may_run = decision.allowed and (approved or not decision.requires_approval)

        self.record(action, level=decision.level, executed=may_run,
                    approved=approved, detail=detail)

        if not may_run:
            raise ApprovalRequiredError(decision.reason)
        return decision

    def levels_table(self) -> dict[ActionType, AutonomyDecision]:
        """Return the current decision for every action (for CLI/dashboard)."""
        return {action: self.decide(action) for action in ActionType}

    # ------------------------------------------------------------------ #
    # Audit
    # ------------------------------------------------------------------ #
    def record(self, action: ActionType, *, level: AutonomyLevel,
               executed: bool, approved: bool = False, detail: str = "") -> None:
        """Append an audit row.

        Records *that* an action happened and under what authority — never its
        content. ``detail`` is a short label (a task id, a filename).
        """
        try:
            self._get_memory().log_autonomy_action(
                action=action.value,
                level=level.label,
                executed=executed,
                approved=approved,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - auditing must never break the action
            logger.exception("Failed to write autonomy audit row for %s", action.value)

    def recent_audit(self, *, limit: int = 50) -> list:
        """Return the most recent audit rows, newest first."""
        return self._get_memory().recent_autonomy_audit(limit=limit)
