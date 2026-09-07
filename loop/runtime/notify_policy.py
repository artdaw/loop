"""Notification policy: who gets interrupted, and when (runtime §8).

The Notification Manager decides delivery — not each agent. Letting every
specialist decide for itself is how an assistant becomes noise: each individual
message is defensible, and the total is not.

The central distinction is **authority**, not urgency:

* A reply, an explicitly requested reminder, an approval result, or an activated
  routine's own scheduled output is **non-discretionary**. It goes at the time
  the user asked for, quiet hours included, because the user set that time (P07).
* Everything an agent thought of by itself is **discretionary**. It defers to a
  digest during quiet hours, competes against a daily cap, and expires rather
  than accumulating (P06).

An LLM may suggest phrasing or argue relevance. It cannot promote a discretionary
suggestion into a reminder — only the executor assigns a category, from the
authority the candidate actually came with. Otherwise "this is important" becomes
a way to bypass the cap.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import text

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.ids import new_id

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


class Category(str, Enum):
    """Only the executor assigns these (runtime §8)."""

    REPLY = "reply"
    REMINDER = "reminder"
    REQUESTED_ROUTINE = "requested_routine"
    DISCRETIONARY = "discretionary"
    DIGEST = "digest"
    APPROVAL = "approval"
    HEALTH = "health"

    @property
    def is_discretionary(self) -> bool:
        return self is Category.DISCRETIONARY


#: Categories carrying explicit user authority for their timing.
NON_DISCRETIONARY = frozenset({
    Category.REPLY, Category.REMINDER, Category.APPROVAL,
    Category.REQUESTED_ROUTINE,
})

#: Onboarding defaults (runtime §8).
QUIET_START_HOUR = 22
QUIET_END_HOUR = 7
MAX_DISCRETIONARY_PER_DAY = 3
PER_SUBJECT_COOLDOWN_SECONDS = 6 * 3600
DIGEST_HOURS = (8, 18)


class Decision(str, Enum):
    SEND = "send"
    DEFER_TO_DIGEST = "defer_to_digest"
    SUPPRESS = "suppress"
    EXPIRE = "expire"


@dataclass
class Candidate:
    """A proposed notification, before policy has ruled on it."""

    category: Category
    subject_ref: str
    occurrence_key: str
    why_now: str = ""
    scope: str = ""
    not_before: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    project_ref: str | None = None
    created_at: dt.datetime | None = None
    #: Did the user choose this delivery *moment*, or only the trigger?
    #: A reminder they set for 23:00 is a time they picked; a warning
    #: subscription is a trigger they picked, and the alert arrives whenever
    #: the weather decides. Only the former overrides quiet hours (WF20).
    timing_chosen_by_user: bool = True

    @property
    def is_discretionary(self) -> bool:
        return self.category.is_discretionary


@dataclass
class PolicyOutcome:
    """What policy decided, and why — the reason is user-visible."""

    decision: Decision
    reason: str
    deliver_at: dt.datetime | None = None

    @property
    def sends(self) -> bool:
        return self.decision is Decision.SEND


@dataclass
class NotificationPolicy:
    """Quiet hours, caps and digests for one owner."""

    timezone: str = "Europe/Berlin"
    quiet_start_hour: int = QUIET_START_HOUR
    quiet_end_hour: int = QUIET_END_HOUR
    max_discretionary_per_day: int = MAX_DISCRETIONARY_PER_DAY
    per_subject_cooldown_seconds: int = PER_SUBJECT_COOLDOWN_SECONDS

    def is_quiet(self, moment: dt.datetime) -> bool:
        local = moment.astimezone(ZoneInfo(self.timezone))
        hour = local.hour
        if self.quiet_start_hour > self.quiet_end_hour:
            # The window wraps midnight, which is the normal case.
            return hour >= self.quiet_start_hour or hour < self.quiet_end_hour
        return self.quiet_start_hour <= hour < self.quiet_end_hour

    def next_digest_after(self, moment: dt.datetime) -> dt.datetime:
        """The next digest slot at or after ``moment``."""
        zone = ZoneInfo(self.timezone)
        local = moment.astimezone(zone)
        for hour in DIGEST_HOURS:
            slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
            if slot > local:
                return slot.astimezone(dt.UTC)
        tomorrow = (local + dt.timedelta(days=1)).replace(
            hour=DIGEST_HOURS[0], minute=0, second=0, microsecond=0)
        return tomorrow.astimezone(dt.UTC)


class NotificationManager:
    """Applies policy to candidates. Agents submit; this decides."""

    def __init__(self, *, policy: NotificationPolicy | None = None,
                 clock: Clock | None = None,
                 sessions: sessionmaker[Session] | None = None) -> None:
        self.policy = policy or NotificationPolicy()
        self._clock = clock or SystemClock()
        #: Sent discretionary messages, for the daily cap and subject cooldown.
        self._sent: list[tuple[dt.datetime, str]] = []
        self._suppressed_scopes: dict[str, dt.datetime] = {}
        # Optional write-through/read-through persistence. Without it this is
        # exactly the in-memory object every existing test already constructs.
        # With it, a restart mid-day does not forget how many discretionary
        # messages already went out — an unremembered count would silently
        # reopen the daily cap the moment the process restarts.
        self._sessions = sessions
        if self._sessions is not None:
            self._ensure_table()
            self._load_recent()

    # ------------------------------------------------------------------ #
    # Persistence (optional)
    # ------------------------------------------------------------------ #
    def _ensure_table(self) -> None:
        assert self._sessions is not None
        with self._sessions() as session:
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    subject_ref TEXT NOT NULL,
                    occurrence_key TEXT NOT NULL DEFAULT '',
                    local_date TEXT NOT NULL,
                    sent_at BIGINT NOT NULL
                )"""))
            session.commit()

    def _load_recent(self) -> None:
        """Load enough history to cover the cooldown and daily-cap windows.

        48 hours comfortably covers the default 6-hour subject cooldown and a
        same-day cap check across any timezone offset, with margin.
        """
        assert self._sessions is not None
        cutoff = to_micros(self._clock.now()) - 48 * 3600 * 1_000_000
        with self._sessions() as session:
            rows = session.execute(text(
                "SELECT sent_at, subject_ref FROM notification_deliveries "
                "WHERE sent_at >= :cutoff"), {"cutoff": cutoff}).all()
        for sent_at, subject_ref in rows:
            self._sent.append((dt.datetime.fromtimestamp(
                sent_at / 1_000_000, tz=dt.UTC), subject_ref))

    def _persist_sent(self, candidate: Candidate, sent_at: dt.datetime) -> None:
        if self._sessions is None:
            return
        local_date = sent_at.astimezone(
            ZoneInfo(self.policy.timezone)).date().isoformat()
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO notification_deliveries (id, category, "
                "subject_ref, occurrence_key, local_date, sent_at) VALUES "
                "(:id, :category, :subject_ref, :occurrence_key, :local_date, "
                ":sent_at)"),
                {"id": new_id(), "category": candidate.category.value,
                 "subject_ref": candidate.subject_ref,
                 "occurrence_key": candidate.occurrence_key,
                 "local_date": local_date, "sent_at": to_micros(sent_at)})
            session.commit()

    # ------------------------------------------------------------------ #
    # Scoped suppression
    # ------------------------------------------------------------------ #
    def suppress_scope(self, scope: str, *, until: dt.datetime) -> None:
        """Suppress one scope, e.g. a declared away window (P08)."""
        self._suppressed_scopes[scope] = until

    def _scope_suppressed(self, candidate: Candidate,
                          now: dt.datetime) -> bool:
        for scope, until in self._suppressed_scopes.items():
            if now >= until:
                continue
            if candidate.scope == scope or candidate.scope.startswith(scope + "."):
                return True
        return False

    # ------------------------------------------------------------------ #
    # The decision
    # ------------------------------------------------------------------ #
    def decide(self, candidate: Candidate) -> PolicyOutcome:
        """Rule on one candidate."""
        now = self._clock.now()

        if candidate.expires_at is not None and now >= candidate.expires_at:
            return PolicyOutcome(Decision.EXPIRE,
                                 "the candidate expired before it could be sent")

        if self._scope_suppressed(candidate, now):
            return PolicyOutcome(Decision.SUPPRESS,
                                 f"the {candidate.scope!r} scope is suppressed")

        if candidate.category in NON_DISCRETIONARY:
            if candidate.timing_chosen_by_user or not self.policy.is_quiet(now):
                # The user set this time, or it is not quiet hours anyway.
                # Silencing a requested reminder would be the failure here.
                return PolicyOutcome(
                    Decision.SEND,
                    f"{candidate.category.value} carries explicit authority",
                    deliver_at=candidate.not_before or now)
            # Authorised to be delivered, but not authorised to wake anyone:
            # the user chose the trigger, not the hour.
            return PolicyOutcome(
                Decision.DEFER_TO_DIGEST,
                "quiet hours; this subscription has no quiet-hour exception",
                deliver_at=self.policy.next_digest_after(now))

        if self.policy.is_quiet(now):
            return PolicyOutcome(
                Decision.DEFER_TO_DIGEST, "quiet hours",
                deliver_at=self.policy.next_digest_after(now))

        if self._over_daily_cap(now):
            return PolicyOutcome(
                Decision.DEFER_TO_DIGEST,
                f"more than {self.policy.max_discretionary_per_day} "
                "discretionary messages today",
                deliver_at=self.policy.next_digest_after(now))

        if self._in_subject_cooldown(candidate, now):
            return PolicyOutcome(
                Decision.DEFER_TO_DIGEST,
                f"already messaged about {candidate.subject_ref} recently",
                deliver_at=self.policy.next_digest_after(now))

        return PolicyOutcome(Decision.SEND, candidate.why_now or "within policy",
                             deliver_at=now)

    def record_sent(self, candidate: Candidate) -> None:
        """Count a delivered discretionary message against the cap."""
        if candidate.is_discretionary:
            sent_at = self._clock.now()
            self._sent.append((sent_at, candidate.subject_ref))
            self._persist_sent(candidate, sent_at)

    def _over_daily_cap(self, now: dt.datetime) -> bool:
        today = now.astimezone(ZoneInfo(self.policy.timezone)).date()
        count = sum(1 for sent_at, _ in self._sent
                    if sent_at.astimezone(ZoneInfo(self.policy.timezone)).date()
                    == today)
        return count >= self.policy.max_discretionary_per_day

    def _in_subject_cooldown(self, candidate: Candidate,
                             now: dt.datetime) -> bool:
        window = dt.timedelta(seconds=self.policy.per_subject_cooldown_seconds)
        return any(subject == candidate.subject_ref and now - sent_at < window
                   for sent_at, subject in self._sent)

    # ------------------------------------------------------------------ #
    # Competition between discretionary candidates
    # ------------------------------------------------------------------ #
    def rank(self, candidates: list[Candidate], *,
             current_project: str | None = None) -> list[Candidate]:
        """Order competing discretionary candidates (runtime §8).

        Latest useful delivery time first, then explicit project relevance, then
        oldest creation time. Deterministic so the same queue always produces
        the same order — a model reordering by "importance" would be a way to
        jump the cap.
        """
        far_future = dt.datetime.max.replace(tzinfo=dt.UTC)
        epoch = dt.datetime.min.replace(tzinfo=dt.UTC)

        def key(candidate: Candidate) -> tuple:
            return (
                candidate.expires_at or far_future,
                0 if (current_project and candidate.project_ref == current_project)
                else 1,
                candidate.created_at or epoch,
            )

        return sorted(candidates, key=key)

    # ------------------------------------------------------------------ #
    # Occurrence supersession (P09)
    # ------------------------------------------------------------------ #
    @staticmethod
    def supersede(pending: list[Candidate], *, subject_ref: str,
                  new_occurrence_key: str) -> tuple[list[Candidate], list[Candidate]]:
        """Replace pending candidates for a subject with a new occurrence.

        When a meeting moves, the reminder for the old time is not merely late —
        it is *wrong*, and sending it would tell the user to prepare for
        something that is no longer happening. Returns (kept, cancelled).
        """
        cancelled = [c for c in pending
                     if c.subject_ref == subject_ref
                     and c.occurrence_key != new_occurrence_key]
        kept = [c for c in pending if c not in cancelled]
        return kept, cancelled


def assign_category(*, requested_by_user: bool, is_reply: bool = False,
                    is_approval: bool = False,
                    from_activated_routine: bool = False) -> Category:
    """Derive a category from actual authority, never from a model's claim.

    An activated routine's *own* scheduled output is requested; other
    suggestions it happens to produce stay discretionary, or activating one
    routine would exempt everything downstream of it from the cap.
    """
    if is_reply:
        return Category.REPLY
    if is_approval:
        return Category.APPROVAL
    if requested_by_user:
        return Category.REMINDER
    if from_activated_routine:
        return Category.REQUESTED_ROUTINE
    return Category.DISCRETIONARY
