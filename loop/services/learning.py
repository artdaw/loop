"""Preferences, hypotheses and the timing learner (vault §9, runtime §8).

Learning here means noticing patterns in what the user explicitly did, and then
*asking*. It never means retraining, and it never means acting on an inference
as though it were a stated fact.

Three kinds of record, deliberately kept apart:

* **Explicit** — the user said it. Applies immediately within its scope.
* **Hypothesis** — Loop inferred it. Stored separately, never written into the
  profile or goals, never presented as fact, and expiring on its own.
* **Confirmed** — the user accepted a proposal. Only this promotes an inference
  into something that governs behaviour.

The load-bearing rule is that **nonresponse is not feedback** (P15). Ten ignored
messages mean ten things Loop does not know: not dislike, not consent, not
completion, not that they were seen. Every threshold below counts explicit
actions only, because silence is the most abundant signal available and the
least informative one.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

DAY = 86_400

#: Initial timing-learner heuristics (vault §9, interfaces learning policy).
#: Adjustable, and explicitly not calibrated psychological certainty.
TIMING_MIN_SAMPLES = 5
TIMING_WINDOW_DAYS = 28
MIN_SHIFT_MINUTES = 15
MAX_IQR_MINUTES = 30
REJECTION_COOLDOWN_DAYS = 30
HYPOTHESIS_TTL_DAYS = 30
PROPOSAL_ROUNDING_MINUTES = 5


class FeedbackKind(str, Enum):
    """Explicit actions only. There is no "ignored" kind, by design (P15)."""

    ACCEPT = "accept"
    REJECT = "reject"
    SNOOZE = "snooze"
    DISMISS = "dismiss"
    CORRECT = "correct"


class PreferenceState(str, Enum):
    EXPLICIT = "explicit"
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"

    @property
    def governs(self) -> bool:
        """Whether a record in this state actually decides behaviour."""
        return self in (PreferenceState.EXPLICIT, PreferenceState.CONFIRMED)


@dataclass(frozen=True)
class Feedback:
    """One explicit user action on one subject."""

    subject_ref: str
    event_id: str
    kind: FeedbackKind
    occurred_at: int
    #: For a snooze: how far the user pushed it, in minutes.
    shift_minutes: int | None = None

    @property
    def day(self) -> int:
        """The local day index, for the distinct-day requirement."""
        return self.occurred_at // DAY


@dataclass
class Preference:
    """A preference or hypothesis record (vault §9)."""

    id: str
    key: str
    value: Any
    scope: str = "global"
    state: PreferenceState = PreferenceState.PROPOSED
    evidence_event_ids: tuple[str, ...] = ()
    observed_from: int | None = None
    observed_to: int | None = None
    sample_count: int = 0
    created_at: int = 0
    updated_at: int = 0
    review_after: int | None = None
    supersedes: str | None = None
    rationale: str = ""
    #: Where this record's content has been copied — briefs, indexes, digests.
    derivative_refs: tuple[str, ...] = ()

    @property
    def is_hypothesis(self) -> bool:
        return self.state is PreferenceState.PROPOSED

    @property
    def governs(self) -> bool:
        return self.state.governs

    def expired(self, now: int) -> bool:
        """A hypothesis with no fresh supporting evidence lapses (vault §9)."""
        if not self.is_hypothesis or self.observed_to is None:
            return False
        return now - self.observed_to > HYPOTHESIS_TTL_DAYS * DAY


@dataclass
class TimingProposal:
    """A proposed schedule shift. Always a proposal — never auto-applied."""

    subject_ref: str
    shift_minutes: int
    sample_count: int
    distinct_days: int
    median_shift_minutes: float
    iqr_minutes: float
    evidence_event_ids: tuple[str, ...]
    observed_from: int
    observed_to: int

    @property
    def equivalence_key(self) -> tuple[str, int]:
        """Two proposals are "equivalent" when they ask for the same change.

        A rejection suppresses the equivalent proposal, not every future one:
        the user said no to moving this reminder 30 minutes later, which is not
        the same as saying no to ever discussing its timing again (P14).
        """
        return (self.subject_ref, self.shift_minutes)

    def describe(self) -> str:
        return (f"You have snoozed this by about {self.median_shift_minutes:.0f} "
                f"minutes on {self.distinct_days} separate days "
                f"({self.sample_count} times). Shall I move it "
                f"{self.shift_minutes} minutes later?")


def _round_to(value: float, step: int) -> int:
    return int(round(value / step) * step)


def _iqr(values: list[float]) -> float:
    """Interquartile range, defined for small samples without interpolation."""
    if len(values) < 2:
        return 0.0
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    lower = ordered[:midpoint]
    upper = ordered[midpoint + 1:] if len(ordered) % 2 else ordered[midpoint:]
    if not lower or not upper:
        return 0.0
    return statistics.median(upper) - statistics.median(lower)


class TimingLearner:
    """Proposes schedule shifts from explicit snoozes (P12, P13).

    Every threshold exists to stop one specific false positive:

    * **Distinct days**, not events — five taps in one frustrated minute is one
      bad morning, not a pattern.
    * **Minimum shift** — a four-minute median is noise dressed as a signal.
    * **Maximum IQR** — a wide spread means the user snoozes to whenever they
      happen to be free, and no single new time would have helped.
    """

    def __init__(self, *, min_samples: int = TIMING_MIN_SAMPLES,
                 window_days: int = TIMING_WINDOW_DAYS,
                 min_shift_minutes: int = MIN_SHIFT_MINUTES,
                 max_iqr_minutes: int = MAX_IQR_MINUTES) -> None:
        self.min_samples = min_samples
        self.window_days = window_days
        self.min_shift_minutes = min_shift_minutes
        self.max_iqr_minutes = max_iqr_minutes

    def propose(self, feedback: list[Feedback], *, subject_ref: str,
                now: int) -> TimingProposal | None:
        """Return a proposal only when every threshold is met."""
        window_start = now - self.window_days * DAY
        snoozes = [f for f in feedback
                   if f.subject_ref == subject_ref
                   and f.kind is FeedbackKind.SNOOZE
                   and f.shift_minutes is not None
                   and window_start <= f.occurred_at <= now]

        if len(snoozes) < self.min_samples:
            return None

        # One sample per day: repeated same-day clicks are one observation.
        by_day: dict[int, Feedback] = {}
        for item in sorted(snoozes, key=lambda f: f.occurred_at):
            by_day.setdefault(item.day, item)
        if len(by_day) < self.min_samples:
            return None

        chosen = [by_day[day] for day in sorted(by_day)]
        shifts = [float(f.shift_minutes) for f in chosen
                  if f.shift_minutes is not None]

        median = statistics.median(shifts)
        spread = _iqr(shifts)

        if median < self.min_shift_minutes:
            return None
        if spread > self.max_iqr_minutes:
            return None

        return TimingProposal(
            subject_ref=subject_ref,
            shift_minutes=_round_to(median, PROPOSAL_ROUNDING_MINUTES),
            sample_count=len(chosen), distinct_days=len(by_day),
            median_shift_minutes=median, iqr_minutes=spread,
            evidence_event_ids=tuple(f.event_id for f in chosen),
            observed_from=chosen[0].occurred_at,
            observed_to=chosen[-1].occurred_at)


@dataclass
class Rejection:
    equivalence_key: tuple[str, int]
    rejected_at: int

    def suppresses(self, proposal: TimingProposal, *, now: int,
                   cooldown_days: int = REJECTION_COOLDOWN_DAYS) -> bool:
        if self.equivalence_key != proposal.equivalence_key:
            return False
        return now - self.rejected_at < cooldown_days * DAY


class PreferenceStore:
    """Preferences, hypotheses and the proposal lifecycle."""

    def __init__(self) -> None:
        self._records: dict[str, Preference] = {}
        self._rejections: list[Rejection] = []
        self._queued_proposals: list[TimingProposal] = []
        self._forgotten_keys: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------ #
    # Records
    # ------------------------------------------------------------------ #
    def put(self, preference: Preference) -> Preference:
        self._records[preference.id] = preference
        return preference

    def get(self, preference_id: str) -> Preference | None:
        return self._records.get(preference_id)

    def all(self) -> list[Preference]:
        return sorted(self._records.values(), key=lambda p: p.id)

    def active(self, key: str, *, scope: str = "global",
               now: int | None = None) -> Preference | None:
        """The record that actually governs this key, if any.

        Hypotheses are excluded: an inference never decides behaviour on its
        own, and an expired one does not linger.
        """
        candidates = [p for p in self._records.values()
                      if p.key == key and p.scope == scope and p.governs]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.updated_at)

    def hypotheses(self, *, now: int) -> list[Preference]:
        return [p for p in self.all() if p.is_hypothesis and not p.expired(now)]

    # ------------------------------------------------------------------ #
    # Explicit statements win (P16)
    # ------------------------------------------------------------------ #
    def record_explicit(self, *, preference_id: str, key: str, value: Any,
                        scope: str = "global", now: int,
                        event_id: str = "") -> Preference:
        """Record an authenticated statement, retiring anything it contradicts.

        An explicit preference does not merely outrank a conflicting hypothesis;
        it invalidates it. Leaving the inference alive would let it resurface as
        a proposal the user has already answered.
        """
        explicit = Preference(
            id=preference_id, key=key, value=value, scope=scope,
            state=PreferenceState.EXPLICIT,
            evidence_event_ids=(event_id,) if event_id else (),
            created_at=now, updated_at=now, sample_count=1,
            rationale="stated by the owner")

        for record in list(self._records.values()):
            if record.key != key or record.scope != scope:
                continue
            if record.id == preference_id:
                continue
            if record.value == value:
                continue
            record.state = PreferenceState.SUPERSEDED
            record.updated_at = now
            record.supersedes = None

        self._queued_proposals = [
            p for p in self._queued_proposals if p.subject_ref != key]
        return self.put(explicit)

    def propose_from(self, proposal: TimingProposal, *, preference_id: str,
                     key: str, value: Any, now: int,
                     scope: str = "global") -> Preference | None:
        """Queue a proposal as a hypothesis, unless a rejection suppresses it."""
        if self.suppressed(proposal, now=now):
            return None
        if (key, scope) in self._forgotten_keys:
            return None

        record = Preference(
            id=preference_id, key=key, value=value, scope=scope,
            state=PreferenceState.PROPOSED,
            evidence_event_ids=proposal.evidence_event_ids,
            observed_from=proposal.observed_from,
            observed_to=proposal.observed_to,
            sample_count=proposal.sample_count, created_at=now, updated_at=now,
            review_after=now + HYPOTHESIS_TTL_DAYS * DAY,
            rationale=proposal.describe())
        self._queued_proposals.append(proposal)
        return self.put(record)

    def confirm(self, preference_id: str, *, now: int) -> Preference:
        record = self._require(preference_id)
        record.state = PreferenceState.CONFIRMED
        record.updated_at = now
        self._queued_proposals = [
            p for p in self._queued_proposals if p.subject_ref != record.key]
        return record

    def reject(self, preference_id: str, *, proposal: TimingProposal,
               now: int) -> Preference:
        """Record a rejection and start its cooldown (P14)."""
        record = self._require(preference_id)
        record.state = PreferenceState.REJECTED
        record.updated_at = now
        self._rejections.append(Rejection(proposal.equivalence_key, now))
        self._queued_proposals = [
            p for p in self._queued_proposals
            if p.equivalence_key != proposal.equivalence_key]
        return record

    def suppressed(self, proposal: TimingProposal, *, now: int) -> bool:
        return any(r.suppresses(proposal, now=now) for r in self._rejections)

    def queued_proposals(self) -> list[TimingProposal]:
        return list(self._queued_proposals)

    def _require(self, preference_id: str) -> Preference:
        record = self._records.get(preference_id)
        if record is None:
            raise KeyError(f"no preference {preference_id!r}")
        return record

    # ------------------------------------------------------------------ #
    # Forgetting (P17)
    # ------------------------------------------------------------------ #
    def forget(self, key: str, *, scope: str = "global",
               now: int) -> dict[str, Any]:
        """Remove a learned record and everything carrying its content.

        Archiving a private copy is not forgetting. The canonical record, the
        derivatives that quote it and any queued proposal that would reintroduce
        it all have to go, or the next digest simply says it again.
        """
        removed: list[str] = []
        derivatives: list[str] = []

        for record in list(self._records.values()):
            if record.key != key or record.scope != scope:
                continue
            derivatives.extend(record.derivative_refs)
            removed.append(record.id)
            del self._records[record.id]

        before = len(self._queued_proposals)
        self._queued_proposals = [
            p for p in self._queued_proposals if p.subject_ref != key]

        self._forgotten_keys.add((key, scope))
        return {
            "removed_records": removed,
            "removed_derivatives": sorted(set(derivatives)),
            "invalidated_proposals": before - len(self._queued_proposals),
            "forgotten_at": now,
        }

    def is_forgotten(self, key: str, *, scope: str = "global") -> bool:
        return (key, scope) in self._forgotten_keys


def inferences_from_silence(_messages: list[Any]) -> list[Preference]:
    """Deliberately returns nothing, and is deliberately named (P15).

    Ten unanswered messages support no conclusion. Not dislike, not consent,
    not completion, and not that they were seen — "seen" needs a real
    acknowledgement from the transport, never the absence of a reply.
    """
    return []
