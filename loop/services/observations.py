"""Observations, expiry and three-valued condition logic (runtime §4, §7).

Observations are **temporary context**, not permanent knowledge. Each key
declares a maximum freshness, and a value past it evaluates ``unknown`` rather
than continuing to look true. That distinction is the whole module: a stale
forecast must not satisfy "it will rain", because acting on it produces confident
advice about a world that has moved on (P04).

Three-valued logic throughout:

* ``true`` may fire,
* ``false`` does not,
* ``unknown`` records the missing fact and fires nothing.

Collapsing ``unknown`` into ``false`` would silently turn "we could not check" into
"we checked and it is fine" — the same class of error as reporting an unreadable
calendar as an empty one.

**Edge triggering** completes it. A condition that stays true does not re-fire on
every evaluation (P05); it fires on the transition into true and rearms only when
it goes false or unknown, or at an explicitly configured occurrence boundary.
Otherwise a persistently true condition becomes an unlimited notification source.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.errors import InvalidInput
from loop.core.ids import new_id
from loop.core.privacy import PrivacyLabel

logger = logging.getLogger(__name__)

#: How confident we are in a value's origin (runtime §4).
CONFIDENCE_LEVELS = ("stated", "observed", "inferred")

#: Operators a condition predicate may use. No eval, no code strings.
OPERATORS = ("eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in", "exists")


class Truth(str, Enum):
    """Three-valued result of evaluating a predicate."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"

    @property
    def may_fire(self) -> bool:
        return self is Truth.TRUE


@dataclass
class Observation:
    """One time-bounded fact about the world."""

    id: str
    key: str
    subject_ref: str
    value: Any
    valid_from: int
    expires_at: int | None = None
    confidence: str = "observed"
    privacy: PrivacyLabel = field(
        default_factory=PrivacyLabel.for_unlabelled_import)
    supersedes: str | None = None
    evidence: list[str] = field(default_factory=list)

    def is_fresh(self, now: int) -> bool:
        return self.expires_at is None or now < self.expires_at


class ObservationStore:
    """Stores observations and answers questions about them."""

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS observations ("
                "id VARCHAR(36) PRIMARY KEY, key VARCHAR(128) NOT NULL, "
                "subject_ref VARCHAR(128) NOT NULL, value_json TEXT NOT NULL, "
                "evidence_json TEXT NOT NULL DEFAULT '[]', "
                "valid_from BIGINT NOT NULL, expires_at BIGINT, "
                "confidence VARCHAR(16) NOT NULL, privacy TEXT NOT NULL, "
                "supersedes VARCHAR(36))"))
            session.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_observations_key "
                "ON observations (key, subject_ref, expires_at)"))
            session.commit()

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    def record(self, *, key: str, subject_ref: str, value: Any,
               ttl_seconds: int | None = None, confidence: str = "observed",
               privacy: PrivacyLabel | None = None,
               evidence: list[str] | None = None,
               supersedes: str | None = None) -> Observation:
        """Store an observation with an explicit freshness bound."""
        if confidence not in CONFIDENCE_LEVELS:
            raise InvalidInput(f"confidence must be one of {CONFIDENCE_LEVELS}")

        now = to_micros(self._clock.now())
        observation = Observation(
            id=new_id(), key=key, subject_ref=subject_ref, value=value,
            valid_from=now,
            expires_at=now + ttl_seconds * 1_000_000 if ttl_seconds else None,
            confidence=confidence,
            privacy=privacy or PrivacyLabel.for_unlabelled_import(),
            supersedes=supersedes, evidence=list(evidence or []))

        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO observations (id, key, subject_ref, value_json, "
                "evidence_json, valid_from, expires_at, confidence, privacy, "
                "supersedes) VALUES (:id, :key, :subject, :value, :evidence, "
                ":valid_from, :expires, :confidence, :privacy, :supersedes)"),
                {"id": observation.id, "key": key, "subject": subject_ref,
                 "value": json.dumps(value), "evidence": json.dumps(observation.evidence),
                 "valid_from": now, "expires": observation.expires_at,
                 "confidence": confidence,
                 "privacy": json.dumps(observation.privacy.to_json()),
                 "supersedes": supersedes})
            session.commit()
        return observation

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def current(self, key: str, subject_ref: str) -> Observation | None:
        """The freshest non-expired value, or ``None``.

        ``None`` means *unknown*, not *false*. Callers must not treat an absent
        observation as a negative answer.
        """
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT id, key, subject_ref, value_json, evidence_json, "
                "valid_from, expires_at, confidence, privacy, supersedes "
                "FROM observations WHERE key = :key AND subject_ref = :subject "
                "AND (expires_at IS NULL OR expires_at > :now) "
                # `rowid` breaks the tie: two observations recorded in the
                # same microsecond otherwise come back in arbitrary order, and
                # the *older* value winning is how a sensor update silently
                # fails to take effect. Insertion order is the intuitive
                # meaning of "freshest" when the timestamps are equal.
                "ORDER BY valid_from DESC, rowid DESC LIMIT 1"),
                {"key": key, "subject": subject_ref, "now": now}).first()
        if row is None:
            return None
        return Observation(
            id=row[0], key=row[1], subject_ref=row[2], value=json.loads(row[3]),
            evidence=json.loads(row[4]), valid_from=row[5], expires_at=row[6],
            confidence=row[7],
            privacy=PrivacyLabel.from_json(json.loads(row[8])),
            supersedes=row[9])

    def history(self, key: str, subject_ref: str) -> list[Observation]:
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            rows = session.execute(text(
                "SELECT id FROM observations WHERE key = :key "
                "AND subject_ref = :subject ORDER BY valid_from DESC"),
                {"key": key, "subject": subject_ref}).all()
        result = []
        for row in rows:
            with self._sessions() as session:
                found = session.execute(text(
                    "SELECT id, key, subject_ref, value_json, evidence_json, "
                    "valid_from, expires_at, confidence, privacy, supersedes "
                    "FROM observations WHERE id = :id"), {"id": row[0]}).first()
            if found:
                result.append(Observation(
                    id=found[0], key=found[1], subject_ref=found[2],
                    value=json.loads(found[3]), evidence=json.loads(found[4]),
                    valid_from=found[5], expires_at=found[6], confidence=found[7],
                    privacy=PrivacyLabel.from_json(json.loads(found[8])),
                    supersedes=found[9]))
        _ = now
        return result

    def resolve(self, key: str, subject_ref: str) -> tuple[Truth, Any]:
        """Read a fact for predicate evaluation.

        Returns ``UNKNOWN`` when absent or stale, never a default value.
        """
        observation = self.current(key, subject_ref)
        if observation is None:
            return Truth.UNKNOWN, None
        return Truth.TRUE, observation.value

    def expire_now(self, observation_id: str) -> None:
        """Force-expire an observation, e.g. when its source is forgotten."""
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE observations SET expires_at = :now WHERE id = :id"),
                {"now": now, "id": observation_id})
            session.commit()


# --------------------------------------------------------------------------- #
# Predicates
# --------------------------------------------------------------------------- #
def evaluate(predicate: dict[str, Any], facts: dict[str, Any], *,
             missing: set[str] | None = None) -> Truth:
    """Evaluate a typed predicate AST with three-valued logic.

    ``facts`` holds known values; ``missing`` names keys that are absent or
    stale. A leaf on a missing key is ``unknown`` — and ``unknown`` propagates,
    except where the other operand already decides the result.
    """
    missing = missing or set()

    if "all" in predicate:
        results = [evaluate(p, facts, missing=missing) for p in predicate["all"]]
        if any(r is Truth.FALSE for r in results):
            return Truth.FALSE      # one false settles a conjunction
        if any(r is Truth.UNKNOWN for r in results):
            return Truth.UNKNOWN
        return Truth.TRUE

    if "any" in predicate:
        results = [evaluate(p, facts, missing=missing) for p in predicate["any"]]
        if any(r is Truth.TRUE for r in results):
            return Truth.TRUE       # one true settles a disjunction
        if any(r is Truth.UNKNOWN for r in results):
            return Truth.UNKNOWN
        return Truth.FALSE

    if "not" in predicate:
        inner = evaluate(predicate["not"], facts, missing=missing)
        if inner is Truth.UNKNOWN:
            return Truth.UNKNOWN    # the negation of unknown is still unknown
        return Truth.FALSE if inner is Truth.TRUE else Truth.TRUE

    fact = predicate.get("fact")
    operator = predicate.get("op")
    expected = predicate.get("value")

    if operator not in OPERATORS:
        raise InvalidInput(f"unknown operator {operator!r}")
    if fact in missing or fact not in facts:
        return Truth.UNKNOWN

    actual = facts[fact]
    try:
        if operator == "exists":
            return Truth.TRUE if actual is not None else Truth.FALSE
        if operator == "eq":
            return Truth.TRUE if actual == expected else Truth.FALSE
        if operator == "ne":
            return Truth.TRUE if actual != expected else Truth.FALSE
        if operator == "lt":
            return Truth.TRUE if actual < expected else Truth.FALSE
        if operator == "lte":
            return Truth.TRUE if actual <= expected else Truth.FALSE
        if operator == "gt":
            return Truth.TRUE if actual > expected else Truth.FALSE
        if operator == "gte":
            return Truth.TRUE if actual >= expected else Truth.FALSE
        if operator == "in":
            return Truth.TRUE if actual in (expected or []) else Truth.FALSE
        if operator == "not_in":
            return Truth.TRUE if actual not in (expected or []) else Truth.FALSE
    except TypeError:
        # Comparing incompatible types is a missing fact, not a false one.
        return Truth.UNKNOWN
    return Truth.UNKNOWN


@dataclass
class EdgeState:
    """Tracks a condition's last truth value so it fires on transitions only."""

    last: Truth = Truth.UNKNOWN
    fired_for_current_true: bool = False

    def should_fire(self, current: Truth) -> bool:
        """True only on a transition *into* true (P05)."""
        if current is not Truth.TRUE:
            # Going false or unknown rearms the trigger.
            self.last = current
            self.fired_for_current_true = False
            return False

        if self.last is Truth.TRUE and self.fired_for_current_true:
            self.last = current
            return False

        self.last = current
        self.fired_for_current_true = True
        return True

    def rearm(self) -> None:
        """Explicitly allow the next true to fire (an occurrence boundary)."""
        self.fired_for_current_true = False


# --------------------------------------------------------------------------- #
# Scoped overrides (P08)
# --------------------------------------------------------------------------- #
@dataclass
class ScopedOverride:
    """A temporary, scoped suppression such as "working from home today".

    Scoped deliberately: an override for the commute must not silence unrelated
    reminders, and it must expire on its own so tomorrow behaves normally.
    """

    scope: str
    reason: str
    expires_at: int

    def covers(self, candidate_scope: str, *, now: int) -> bool:
        if now >= self.expires_at:
            return False
        return candidate_scope == self.scope or candidate_scope.startswith(
            self.scope + ".")


class OverrideSet:
    """Active scoped overrides."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._overrides: list[ScopedOverride] = []

    def add(self, scope: str, *, reason: str, ttl_seconds: int) -> ScopedOverride:
        override = ScopedOverride(
            scope=scope, reason=reason,
            expires_at=to_micros(self._clock.now()) + ttl_seconds * 1_000_000)
        self._overrides.append(override)
        return override

    def suppresses(self, scope: str) -> ScopedOverride | None:
        now = to_micros(self._clock.now())
        for override in self._overrides:
            if override.covers(scope, now=now):
                return override
        return None

    def active(self) -> list[ScopedOverride]:
        now = to_micros(self._clock.now())
        return [o for o in self._overrides if now < o.expires_at]


# --------------------------------------------------------------------------- #
# Subscriptions (P19)
# --------------------------------------------------------------------------- #
@dataclass
class Subscription:
    """A narrow interest in a kind of event."""

    id: str
    event_kind: str
    subject_filters: dict[str, Any] = field(default_factory=dict)
    handler: str = ""
    enabled: bool = True

    def matches(self, event_kind: str, subject: dict[str, Any]) -> bool:
        if not self.enabled or event_kind != self.event_kind:
            return False
        return all(subject.get(key) == value
                   for key, value in self.subject_filters.items())


def wake_for(subscriptions: list[Subscription], *, event_kind: str,
             subject: dict[str, Any]) -> list[Subscription]:
    """Which subscriptions an event should wake.

    An event matching nothing wakes nothing and costs no model call (P19).
    Waking every role "just in case" is how an idle system starts spending.
    """
    return [s for s in subscriptions if s.matches(event_kind, subject)]
