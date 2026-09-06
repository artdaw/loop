"""Routine documents, activation and scope changes (runtime §7, vault §4).

A routine document is a **proposal** until an authenticated event activates it.
``enabled: true`` inside the file is the author's stated intent, not permission —
otherwise anyone who can write to the vault could schedule background work that
sends messages (V28, P21).

Three states, kept distinct because they mean different things to the user:

* **proposed** — saved, visible, doing nothing. This is where a routine lands
  when something is missing.
* **active** — activated by an authenticated request, with the authority
  recorded and the next wake-up visible.
* **invalid** — the document does not validate; the last valid version stays in
  effect rather than the routine silently stopping.

Two rules about what Loop must *not* invent:

* A missing location is a **question**, not something to guess. A timezone is
  not a location: ``Europe/Berlin`` covers hundreds of places with different
  weather, and inferring one would produce confident advice about the wrong
  city (P02).
* A request naming a capability Loop does not have becomes a visible inactive
  proposal that names the missing adapter. It never produces fake live data
  (P20).

**Broadening scope needs fresh authority.** Editing a routine's schedule or
wording can take effect after validation; adding a recipient or a tool cannot,
because that is a permission change wearing the clothes of a content edit (P21).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import yaml

from loop.core.errors import ApprovalRequired, InvalidInput
from loop.core.ids import content_hash, new_id

logger = logging.getLogger(__name__)

VALID_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
VALID_TRIGGER_KINDS = ("at", "local_schedule", "event", "condition")
VALID_MODES = ("each_occurrence", "digest", "once")

#: Fields whose widening is a permission change, not a content edit (P21).
AUTHORITY_FIELDS = ("destination", "tools", "effects")


class RoutineStatus(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    PAUSED = "paused"
    INVALID = "invalid"

    @property
    def runs(self) -> bool:
        return self is RoutineStatus.ACTIVE


@dataclass
class MissingRequirement:
    """Something the routine needs before it can run."""

    kind: str            # input | capability | permission
    name: str
    question: str = ""

    @property
    def is_question(self) -> bool:
        return bool(self.question)


@dataclass
class Routine:
    """A parsed routine document."""

    id: str
    slug: str
    title: str
    trigger: dict[str, Any]
    steps: list[dict[str, Any]] = field(default_factory=list)
    notification: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    privacy: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    status: RoutineStatus = RoutineStatus.PROPOSED
    activation_event_id: str | None = None
    source_hash: str = ""
    missing: list[MissingRequirement] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status is RoutineStatus.ACTIVE

    @property
    def blocking_questions(self) -> list[str]:
        return [m.question for m in self.missing if m.is_question]

    @property
    def declared_tools(self) -> set[str]:
        tools: set[str] = set()
        for step in self.steps:
            capability = step.get("capability")
            if capability:
                tools.add(str(capability))
        return tools

    @property
    def destination(self) -> str:
        return str(self.notification.get("destination") or "")


def parse_routine(document: str, *, known_capabilities: set[str],
                  provided_inputs: dict[str, Any] | None = None
                  ) -> tuple[Routine | None, list[str]]:
    """Parse and validate a routine document.

    Returns ``(routine, problems)``. A routine with unmet requirements still
    comes back — saved and visible — because losing the user's request because
    one detail was missing is worse than holding it (P02, P20).
    """
    problems: list[str] = []
    if not document.startswith("---"):
        return None, ["routine document requires YAML frontmatter"]

    parts = document.split("---", 2)
    if len(parts) < 3:
        return None, ["malformed frontmatter block"]

    try:
        front = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        return None, [f"invalid YAML ({exc.__class__.__name__})"]
    if not isinstance(front, dict):
        return None, ["frontmatter must be a mapping"]

    if front.get("schema_version") != 1:
        problems.append("schema_version must be 1")

    slug = str(front.get("id") or "")
    if not slug:
        problems.append("routine requires an id")

    trigger = front.get("trigger") or {}
    if not isinstance(trigger, dict) or trigger.get("kind") not in VALID_TRIGGER_KINDS:
        problems.append(f"trigger.kind must be one of {VALID_TRIGGER_KINDS}")
    elif trigger["kind"] == "local_schedule":
        bad_days = [d for d in (trigger.get("days") or []) if d not in VALID_DAYS]
        if bad_days:
            problems.append(f"unknown days: {bad_days}")
        if not trigger.get("at"):
            problems.append("local_schedule requires an `at` time")
        if not trigger.get("timezone"):
            problems.append("local_schedule requires a timezone")

    steps = front.get("steps") or []
    if not isinstance(steps, list) or not steps:
        problems.append("routine requires at least one step")
        steps = []

    notification = front.get("notification") or {}
    mode = notification.get("mode")
    if mode and mode not in VALID_MODES:
        problems.append(f"notification.mode must be one of {VALID_MODES}")

    if problems:
        return None, problems

    routine = Routine(
        id=new_id(), slug=slug, title=str(front.get("title") or slug),
        trigger=dict(trigger), steps=[dict(s) for s in steps],
        notification=dict(notification), limits=dict(front.get("limits") or {}),
        privacy=dict(front.get("privacy") or {}), rationale=parts[2].strip(),
        source_hash=content_hash(document))

    routine.missing = _find_missing(routine, known_capabilities,
                                    provided_inputs or {})
    return routine, []


def _find_missing(routine: Routine, known_capabilities: set[str],
                  provided: dict[str, Any]) -> list[MissingRequirement]:
    """Identify what stops this routine running, without guessing any of it."""
    missing: list[MissingRequirement] = []

    for capability in sorted(routine.declared_tools):
        if capability not in known_capabilities:
            missing.append(MissingRequirement(
                kind="capability", name=capability,
                question=""))          # not a question: no answer would help

    for step in routine.steps:
        for key, value in (step.get("arguments") or {}).items():
            if value == "$required" and key not in provided:
                missing.append(MissingRequirement(
                    kind="input", name=key,
                    question=_question_for(key)))
    return missing


def _question_for(key: str) -> str:
    """One focused question per missing input.

    Explicitly *not* derived from the timezone or any other proxy: a location
    guessed from `Europe/Berlin` would produce confident advice about the wrong
    city, which is worse than asking (P02).
    """
    questions = {
        "location_ref": "Which location should I use for this?",
        "location": "Which city or place should I use?",
        "departure_time": "What time do you usually leave?",
    }
    return questions.get(key, f"What should I use for {key}?")


class RoutineService:
    """Activation, pausing and scope-change review."""

    def __init__(self) -> None:
        self._routines: dict[str, Routine] = {}

    # ------------------------------------------------------------------ #
    # Saving
    # ------------------------------------------------------------------ #
    def save(self, routine: Routine) -> Routine:
        """Store a routine as a proposal. Saving never activates."""
        routine.status = (RoutineStatus.PROPOSED if routine.missing
                          else routine.status)
        self._routines[routine.slug] = routine
        return routine

    def get(self, slug: str) -> Routine | None:
        return self._routines.get(slug)

    def list_routines(self) -> list[Routine]:
        return list(self._routines.values())

    # ------------------------------------------------------------------ #
    # Activation
    # ------------------------------------------------------------------ #
    def activate(self, slug: str, *, activation_event_id: str) -> Routine:
        """Activate a routine under an authenticated request (P01).

        An authenticated "tell me every morning…" *is* authority to create and
        activate that scoped routine; it does not need a second approval click.
        But it must be an event, not a line in a file.
        """
        routine = self._require(slug)
        if not activation_event_id:
            raise ApprovalRequired(
                "Activating a routine requires an authenticated request.",
                details={"slug": slug})
        if routine.missing:
            raise InvalidInput(
                "This routine is missing required information and stays inactive.",
                details={"slug": slug,
                         "missing": [m.name for m in routine.missing],
                         "questions": routine.blocking_questions})

        routine.status = RoutineStatus.ACTIVE
        routine.activation_event_id = activation_event_id
        return routine

    def pause(self, slug: str) -> Routine:
        routine = self._require(slug)
        routine.status = RoutineStatus.PAUSED
        return routine

    def resume(self, slug: str) -> Routine:
        """Resume with the previously approved scope, unchanged."""
        routine = self._require(slug)
        if routine.activation_event_id is None:
            raise ApprovalRequired(
                "This routine was never activated; activate it instead.",
                details={"slug": slug})
        routine.status = RoutineStatus.ACTIVE
        return routine

    def _require(self, slug: str) -> Routine:
        routine = self._routines.get(slug)
        if routine is None:
            raise InvalidInput(f"No routine {slug!r}")
        return routine

    # ------------------------------------------------------------------ #
    # Edits (P21)
    # ------------------------------------------------------------------ #
    def review_edit(self, slug: str, updated: Routine
                    ) -> tuple[bool, list[str]]:
        """Decide whether an edit may apply directly.

        Returns ``(applies_directly, broadened)``. Behaviour changes take effect
        after validation; anything that *widens* destination, tools or effects
        is a permission change and stays a proposal until authorised — the old
        scope remains in force meanwhile.
        """
        current = self._require(slug)
        broadened: list[str] = []

        if updated.destination and updated.destination != current.destination:
            broadened.append(f"destination: {current.destination or 'none'} "
                             f"→ {updated.destination}")

        new_tools = updated.declared_tools - current.declared_tools
        if new_tools:
            broadened.append(f"tools: +{sorted(new_tools)}")

        current_effects = set(current.notification.get("effects") or [])
        new_effects = set(updated.notification.get("effects") or []) - current_effects
        if new_effects:
            broadened.append(f"effects: +{sorted(new_effects)}")

        return (not broadened), broadened

    def apply_edit(self, slug: str, updated: Routine, *,
                   authority_event_id: str | None = None) -> Routine:
        """Apply an edit, refusing a broadened scope without fresh authority."""
        applies, broadened = self.review_edit(slug, updated)
        if not applies and not authority_event_id:
            raise ApprovalRequired(
                "This edit broadens what the routine may do and needs approval.",
                details={"slug": slug, "broadened": broadened})

        current = self._require(slug)
        updated.id = current.id
        updated.status = current.status
        updated.activation_event_id = (authority_event_id
                                       or current.activation_event_id)
        self._routines[slug] = updated
        return updated

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #
    def describe(self, slug: str) -> dict[str, Any]:
        """A user-facing summary, honest about why a routine is not running."""
        routine = self._require(slug)
        return {
            "slug": routine.slug,
            "title": routine.title,
            "status": routine.status.value,
            "active": routine.is_active,
            "activation_event_id": routine.activation_event_id,
            "missing": [{"kind": m.kind, "name": m.name, "question": m.question}
                        for m in routine.missing],
            "questions": routine.blocking_questions,
        }
