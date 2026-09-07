"""The learning loop, connected: snooze → proposal → confirmation → new time.

`learning.py` already held every rule vault §9 states — the timing learner's
thresholds, hypotheses that lapse, rejections that suppress an equivalent
proposal for thirty days, and a `forget` that removes derivatives rather than
archiving a private copy. What it did not have was anything feeding it. No user
action was ever recorded as `Feedback`, the learner was never run, and a
confirmed preference changed nothing about when a routine actually fires.

This service is that loop, and its shape is dictated by one line of the
contract: *"Propose moving by that median, never auto-apply."* So:

* **Feedback is durable.** The learner's own thresholds are "five snoozes on
  distinct days within 28 days" — a window that spans restarts by definition.
  Feedback held in memory would reset the count every deploy and the learner
  would never fire at all.
* **Confirmation is what moves the schedule**, and it moves the routine's
  trigger, not just a preference row. A confirmed preference that leaves the
  routine firing at the old time has not learned anything; it has recorded an
  opinion.
* **Nonresponse is not feedback.** There is no timeout that treats an ignored
  proposal as agreement. `learning.inferences_from_silence` exists and returns
  nothing on purpose; this service does not work around it.
* **Forgetting reaches the vault.** vault §9 requires removing content-bearing
  derivatives, and the copy Loop wrote into `_mem/loop/preferences` is one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import yaml
from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.errors import InvalidInput, Unavailable
from loop.core.ids import new_id
from loop.runtime.routine_dispatch import ROUTINE_SUBJECT, RoutineScheduler
from loop.services.learning import (
    DAY,
    Feedback,
    FeedbackKind,
    Preference,
    PreferenceStore,
    TimingLearner,
    TimingProposal,
)
from loop.vault.gateway import VaultGateway, WriteMode

logger = logging.getLogger(__name__)

#: Where the owner can read, correct or delete what Loop believes (vault §9).
PREFERENCE_DIR = "_mem/loop/preferences"
HYPOTHESIS_DIR = "_mem/loop/hypotheses"



class FeedbackLog:
    """Durable record of explicit user actions on a subject.

    Its own table rather than a migration revision, matching `RunStore`'s
    pattern: the row is created on first use, so an existing database gains it
    without a schema step that would have to be ordered against every other.
    """

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS preference_feedback ("
                " id TEXT PRIMARY KEY,"
                " subject_ref TEXT NOT NULL,"
                " event_id TEXT NOT NULL,"
                " kind TEXT NOT NULL,"
                " occurred_at INTEGER NOT NULL,"
                " shift_minutes INTEGER,"
                " created_at INTEGER NOT NULL,"
                # One event acts once. A redelivered Telegram update or a
                # replayed job must not count as a second snooze — the learner
                # thresholds are counts, so a duplicate is a false sample.
                " UNIQUE (subject_ref, event_id))"))
            session.commit()

    def record(self, feedback: Feedback) -> bool:
        """Store one action. Returns False when it was already recorded."""
        from sqlalchemy.exc import IntegrityError

        with self._sessions() as session:
            try:
                session.execute(text(
                    "INSERT INTO preference_feedback (id, subject_ref, event_id,"
                    " kind, occurred_at, shift_minutes, created_at)"
                    " VALUES (:id, :subject, :event, :kind, :occurred, :shift,"
                    " :now)"), {
                        "id": new_id(), "subject": feedback.subject_ref,
                        "event": feedback.event_id, "kind": feedback.kind.value,
                        "occurred": feedback.occurred_at,
                        "shift": feedback.shift_minutes,
                        "now": to_micros(self._clock.now())})
                session.commit()
            except IntegrityError:
                session.rollback()
                logger.info("Feedback event %s for %s already recorded",
                            feedback.event_id, feedback.subject_ref)
                return False
        return True

    def since(self, subject_ref: str, *, since: int) -> list[Feedback]:
        with self._sessions() as session:
            rows = session.execute(text(
                "SELECT subject_ref, event_id, kind, occurred_at, shift_minutes"
                " FROM preference_feedback WHERE subject_ref = :subject"
                " AND occurred_at >= :since ORDER BY occurred_at"),
                {"subject": subject_ref, "since": since}).all()
        return [Feedback(subject_ref=row[0], event_id=row[1],
                         kind=FeedbackKind(row[2]), occurred_at=row[3],
                         shift_minutes=row[4]) for row in rows]

    def subjects(self) -> list[str]:
        with self._sessions() as session:
            rows = session.execute(text(
                "SELECT DISTINCT subject_ref FROM preference_feedback"
                " ORDER BY subject_ref")).all()
        return [row[0] for row in rows]

    def forget(self, subject_ref: str) -> int:
        """Delete the evidence itself, not just what was inferred from it.

        Leaving the snoozes behind would let the very next review re-derive the
        preference the owner just asked to forget.
        """
        with self._sessions() as session:
            # Annotated as the leader lease and trigger store do: `execute` is
            # typed as returning `Result`, but a DML statement always returns a
            # `CursorResult`, which is what carries `rowcount`.
            result: CursorResult[Any] = session.execute(  # type: ignore[assignment]
                text("DELETE FROM preference_feedback WHERE subject_ref = :s"),
                {"s": subject_ref})
            session.commit()
        return int(result.rowcount or 0)


@dataclass
class ReviewOutcome:
    """What a learning review concluded about one subject."""

    subject_ref: str
    proposal: TimingProposal | None = None
    preference_id: str | None = None
    question: str = ""
    reason: str = ""
    #: Nothing was proposed and this says exactly why, in the owner's terms.
    suppressed: bool = False

    @property
    def proposes(self) -> bool:
        return self.proposal is not None


@dataclass
class PreferenceJournal:
    """Mirrors learned records into the vault where the owner can edit them.

    vault §9 requires preferences under `_mem/loop/preferences` and hypotheses
    kept *separately* under `_mem/loop/hypotheses` — separately because a
    hypothesis inserted among stated preferences reads as something the owner
    said, and no later correction can tell the two apart again.
    """

    gateway: VaultGateway | None = None

    def path_for(self, record: Preference) -> str:
        directory = HYPOTHESIS_DIR if record.is_hypothesis else PREFERENCE_DIR
        return f"{directory}/{record.key}--{record.scope}.md"

    def write(self, record: Preference) -> str | None:
        """Write or replace the record's document. Returns its path."""
        if self.gateway is None:
            return None
        path = self.path_for(record)
        body = _render(record)
        existing = self.gateway.exists(path)
        operation = self.gateway.make_operation(
            path, body,
            mode=WriteMode.REPLACE if existing else WriteMode.CREATE_NEW,
            expected_hash=self.gateway.hash_of(path) if existing else None)
        self.gateway.apply(self.gateway.begin([operation]))
        return path

    def remove(self, paths: list[str]) -> list[str]:
        """Delete the documents. Forgetting is not archiving (vault §9).

        The vault's never-delete default yields only to an explicit forget
        request, which is exactly what reaches here — so this really unlinks
        rather than moving the file to `_archive`, where its content would
        still be readable and indexable.
        """
        if self.gateway is None:
            return []
        removed: list[str] = []
        for path in paths:
            resolved = self.gateway.resolve(path)
            if resolved.exists():
                resolved.unlink()
                removed.append(path)
        return removed


def _render(record: Preference) -> str:
    """A record document: frontmatter fields from vault §9, then the evidence."""
    frontmatter = {
        "type": "hypothesis" if record.is_hypothesis else "preference",
        "id": record.id,
        "key": record.key,
        "value": record.value,
        "scope": record.scope,
        "state": record.state.value,
        "evidence_event_ids": list(record.evidence_event_ids),
        "observed_from": record.observed_from,
        "observed_to": record.observed_to,
        "sample_count": record.sample_count,
        "created": record.created_at,
        "updated": record.updated_at,
        "review_after": record.review_after,
        "supersedes": record.supersedes,
    }
    rendered = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    body = record.rationale.strip() or "No rationale was recorded."
    limitation = (
        "This is an inference from observed behaviour, not something you "
        "stated. Correct or delete this file and Loop will stop acting on it."
        if record.is_hypothesis else
        "Recorded from your own instruction. Edit or delete this file to change it.")
    return f"---\n{rendered}---\n\n{body}\n\n_{limitation}_\n"


class FeedbackService:
    """Records explicit feedback, proposes changes, and applies confirmed ones."""

    def __init__(self, *, log: FeedbackLog, preferences: PreferenceStore,
                 scheduler: RoutineScheduler | None = None,
                 journal: PreferenceJournal | None = None,
                 learner: TimingLearner | None = None,
                 clock: Clock | None = None) -> None:
        self.log = log
        self.preferences = preferences
        self.scheduler = scheduler
        self.journal = journal or PreferenceJournal()
        self.learner = learner or TimingLearner()
        self._clock = clock or SystemClock()

    def _now(self) -> int:
        return int(self._clock.now().timestamp())

    # ------------------------------------------------------------------ #
    # Recording what the owner actually did
    # ------------------------------------------------------------------ #
    def record_snooze(self, subject_ref: str, *, event_id: str,
                      shift_minutes: int, occurred_at: int | None = None) -> bool:
        if shift_minutes <= 0:
            raise InvalidInput("A snooze moves a notification later.")
        return self.log.record(Feedback(
            subject_ref=subject_ref, event_id=event_id,
            kind=FeedbackKind.SNOOZE, occurred_at=occurred_at or self._now(),
            shift_minutes=shift_minutes))

    def record_action(self, subject_ref: str, *, event_id: str,
                      kind: FeedbackKind, occurred_at: int | None = None) -> bool:
        return self.log.record(Feedback(
            subject_ref=subject_ref, event_id=event_id, kind=kind,
            occurred_at=occurred_at or self._now()))

    # ------------------------------------------------------------------ #
    # Reviewing
    # ------------------------------------------------------------------ #
    def review(self, subject_ref: str, *, now: int | None = None) -> ReviewOutcome:
        """Run the learner over stored feedback and propose, or explain why not.

        A proposal is stored as a `proposed` preference — a *hypothesis*, kept
        apart from anything the owner stated — and asked as a question. It
        never governs until the owner confirms it.
        """
        moment = now if now is not None else self._now()

        if self.preferences.is_forgotten(subject_ref):
            return ReviewOutcome(
                subject_ref=subject_ref, suppressed=True,
                reason="this subject was forgotten at the owner's request")

        # A bounded read, not a rule: `TimingLearner.propose` applies the
        # 28-day window itself, and that is where the rule is tested. This
        # only avoids loading years of history for the learner to discard —
        # and it takes the bound from the learner rather than restating it,
        # so the two cannot drift apart.
        window = self.log.since(
            subject_ref, since=moment - self.learner.window_days * DAY)
        proposal = self.learner.propose(window, subject_ref=subject_ref, now=moment)
        if proposal is None:
            return ReviewOutcome(
                subject_ref=subject_ref,
                reason="the observed snoozes do not meet the learner's "
                       "thresholds for a consistent shift")

        if self.preferences.suppressed(proposal, now=moment):
            return ReviewOutcome(
                subject_ref=subject_ref, suppressed=True,
                reason="an equivalent proposal was declined recently")

        record = self.preferences.propose_from(
            proposal, preference_id=new_id(), key=subject_ref,
            value=proposal.shift_minutes, now=moment)
        if record is None:
            # `propose_from` applies the same suppression and forgetting rules
            # again. Reaching here means state changed between the checks
            # above and this call; the honest report is that nothing is being
            # proposed, not a crash.
            return ReviewOutcome(
                subject_ref=subject_ref, suppressed=True,
                reason="the proposal was suppressed as it was being recorded")

        self.journal.write(record)
        return ReviewOutcome(subject_ref=subject_ref, proposal=proposal,
                             preference_id=record.id,
                             question=proposal.describe())

    def review_all(self, *, now: int | None = None) -> list[ReviewOutcome]:
        return [self.review(subject, now=now) for subject in self.log.subjects()]

    # ------------------------------------------------------------------ #
    # Deciding
    # ------------------------------------------------------------------ #
    def confirm(self, preference_id: str, *, now: int | None = None
                ) -> tuple[Preference, str]:
        """Accept a proposal, and actually move the schedule it was about.

        Returns the confirmed record and a human-readable description of what
        changed. A confirmation that updated only the preference row would
        leave the routine firing at the old time — an opinion recorded, not a
        behaviour learned.
        """
        moment = now if now is not None else self._now()
        record = self.preferences.get(preference_id)
        if record is None:
            raise Unavailable(
                f"No proposal {preference_id!r} is on offer.",
                details={"preference_id": preference_id})

        # Captured *before* the state changes. `PreferenceStore` mutates the
        # record in place and hands back the same object, so asking for the
        # old document's path afterwards would return the new one's — and the
        # hypothesis file would survive alongside the preference as a second,
        # stale copy of the same belief.
        previous_path = self.journal.path_for(record)

        confirmed = self.preferences.confirm(preference_id, now=moment)
        self.journal.remove([previous_path])
        self.journal.write(confirmed)

        applied = "recorded; no routine is bound to this subject"
        if self.scheduler is not None and isinstance(confirmed.value, int):
            applied = self._reschedule(confirmed.key, confirmed.value)
        return confirmed, applied

    def reject(self, preference_id: str, *, proposal: TimingProposal,
               now: int | None = None) -> Preference:
        """Decline a proposal, suppressing equivalents for the policy window."""
        moment = now if now is not None else self._now()
        existing = self.preferences.get(preference_id)
        previous_path = (self.journal.path_for(existing) if existing is not None
                         else None)

        record = self.preferences.reject(preference_id, proposal=proposal,
                                         now=moment)
        # Same in-place mutation as `confirm`: the path has to be taken before
        # the state moves, or a declined hypothesis keeps its document.
        self.journal.remove([p for p in (previous_path,) if p])
        return record

    def forget(self, subject_ref: str, *, now: int | None = None
               ) -> dict[str, Any]:
        """Remove the belief, its vault copy, and the evidence behind it."""
        moment = now if now is not None else self._now()
        records = [r for r in self.preferences.all() if r.key == subject_ref]
        paths = [self.journal.path_for(record) for record in records]

        result = self.preferences.forget(subject_ref, now=moment)
        result["removed_documents"] = self.journal.remove(paths)
        result["removed_feedback"] = self.log.forget(subject_ref)
        return result

    # ------------------------------------------------------------------ #
    # Explicit statements
    # ------------------------------------------------------------------ #
    def record_explicit(self, key: str, value: Any, *, event_id: str,
                        scope: str = "global", rationale: str = "",
                        now: int | None = None) -> Preference:
        """An authenticated instruction applies immediately, within its scope."""
        moment = now if now is not None else self._now()
        del rationale        # the store writes "stated by the owner" itself
        record = self.preferences.record_explicit(
            preference_id=new_id(), key=key, value=value, scope=scope,
            event_id=event_id, now=moment)
        self.journal.write(record)
        return record

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _reschedule(self, subject_ref: str, shift_minutes: int) -> str:
        """Move the routine named by this subject, if there is one."""
        assert self.scheduler is not None
        slug = subject_ref.split(":", 1)[1] if subject_ref.startswith(
            f"{ROUTINE_SUBJECT}:") else subject_ref
        routine = self.scheduler.routines.get(slug)
        if routine is None:
            return f"no routine named {slug!r}; the preference is recorded only"
        return self.scheduler.shift_schedule(slug, minutes=shift_minutes)


@dataclass
class LearningReport:
    """A digest of one review pass, for `status` and the CLI."""

    proposals: list[ReviewOutcome] = field(default_factory=list)
    quiet: list[ReviewOutcome] = field(default_factory=list)

    @property
    def has_questions(self) -> bool:
        return bool(self.proposals)


def summarise(outcomes: list[ReviewOutcome]) -> LearningReport:
    report = LearningReport()
    for outcome in outcomes:
        (report.proposals if outcome.proposes else report.quiet).append(outcome)
    return report
