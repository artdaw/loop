"""Learning, preferences and the weekly review — P12 to P18."""

from __future__ import annotations

import pytest

from loop.services.learning import (
    DAY,
    Feedback,
    FeedbackKind,
    Preference,
    PreferenceState,
    PreferenceStore,
    TimingLearner,
    inferences_from_silence,
)
from loop.services.review import (
    CanonicalRecord,
    DashboardSnapshot,
    ProgressSource,
    build_review,
    render_review,
)

NOW = 1_780_000_000
SUBJECT = "routine:morning-standup"


def snooze(day_offset: int, shift: int, *, index: int = 0,
           subject: str = SUBJECT) -> Feedback:
    """A snooze `day_offset` days ago, pushed back `shift` minutes."""
    return Feedback(subject_ref=subject, event_id=f"e{day_offset}-{index}",
                    kind=FeedbackKind.SNOOZE,
                    occurred_at=NOW - day_offset * DAY + index * 60,
                    shift_minutes=shift)


@pytest.fixture
def learner() -> TimingLearner:
    return TimingLearner()


@pytest.fixture
def store() -> PreferenceStore:
    return PreferenceStore()


# --------------------------------------------------------------------------- #
# P12 — a real pattern produces one evidence-backed proposal
# --------------------------------------------------------------------------- #
def _consistent() -> list[Feedback]:
    return [snooze(day, shift) for day, shift in
            [(2, 30), (5, 25), (9, 30), (14, 35), (20, 30)]]


def test_p12_five_consistent_snoozes_produce_a_proposal(learner):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    assert proposal is not None


def test_p12_the_proposal_carries_its_sample_count(learner):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)

    assert proposal.sample_count == 5
    assert proposal.distinct_days == 5


def test_p12_the_shift_is_the_median_rounded_to_five_minutes(learner):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)

    assert proposal.median_shift_minutes == 30
    assert proposal.shift_minutes == 30


@pytest.mark.parametrize("shifts,expected", [
    ([21, 22, 22, 23, 22], 20),          # median 22 rounds down
    ([22, 23, 22, 24, 23], 25),          # median 23 rounds up
])
def test_p12_the_shift_is_rounded_to_the_nearest_five_minutes(
        learner, shifts, expected):
    """A proposal of "23 minutes later" reads as false precision."""
    feedback = [snooze(day, shift)
                for day, shift in zip((2, 5, 9, 14, 20), shifts, strict=True)]
    proposal = learner.propose(feedback, subject_ref=SUBJECT, now=NOW)

    assert proposal.shift_minutes == expected


def test_p12_the_evidence_events_are_named(learner):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    assert len(proposal.evidence_event_ids) == 5


def test_p12_nothing_is_applied_automatically(learner, store):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    record = store.propose_from(proposal, preference_id="p1", key=SUBJECT,
                                value={"shift_minutes": 30}, now=NOW)

    assert record.state is PreferenceState.PROPOSED
    assert record.governs is False
    assert store.active(SUBJECT) is None


def test_p12_the_proposal_explains_itself(learner):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    text = proposal.describe()

    assert "5 separate days" in text
    assert "30" in text


def test_p12_a_confirmed_proposal_then_governs(learner, store):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    store.propose_from(proposal, preference_id="p1", key=SUBJECT,
                       value={"shift_minutes": 30}, now=NOW)
    store.confirm("p1", now=NOW)

    assert store.active(SUBJECT).state is PreferenceState.CONFIRMED


# --------------------------------------------------------------------------- #
# P13 — patterns that are not patterns
# --------------------------------------------------------------------------- #
def test_p13_four_snoozes_are_not_enough(learner):
    feedback = [snooze(day, 30) for day in (2, 5, 9, 14)]
    assert learner.propose(feedback, subject_ref=SUBJECT, now=NOW) is None


def test_p13_high_variance_produces_no_proposal(learner):
    """Snoozing to whenever you happen to be free is not a preferred time."""
    feedback = [snooze(day, shift) for day, shift in
                [(2, 10), (5, 90), (9, 20), (14, 120), (20, 45)]]
    assert learner.propose(feedback, subject_ref=SUBJECT, now=NOW) is None


def test_p13_repeated_same_day_clicks_count_once(learner):
    """Five taps in one frustrated minute is one bad morning, not a pattern."""
    feedback = [snooze(3, 30, index=i) for i in range(5)]
    assert learner.propose(feedback, subject_ref=SUBJECT, now=NOW) is None


def test_p13_five_distinct_days_among_same_day_repeats_still_qualifies(learner):
    feedback = ([snooze(3, 30, index=i) for i in range(4)]
                + [snooze(day, 30) for day in (6, 10, 15, 21)])
    proposal = learner.propose(feedback, subject_ref=SUBJECT, now=NOW)

    assert proposal is not None
    assert proposal.distinct_days == 5


def test_p13_a_small_median_shift_is_noise(learner):
    feedback = [snooze(day, 5) for day in (2, 5, 9, 14, 20)]
    assert learner.propose(feedback, subject_ref=SUBJECT, now=NOW) is None


def test_p13_evidence_outside_the_window_does_not_count(learner):
    feedback = [snooze(day, 30) for day in (2, 5, 40, 50, 60)]
    assert learner.propose(feedback, subject_ref=SUBJECT, now=NOW) is None


def test_p13_other_feedback_kinds_are_not_snoozes(learner):
    feedback = [Feedback(SUBJECT, f"e{i}", FeedbackKind.DISMISS,
                         NOW - i * DAY, shift_minutes=30) for i in range(6)]
    assert learner.propose(feedback, subject_ref=SUBJECT, now=NOW) is None


def test_p13_another_subjects_snoozes_do_not_contribute(learner):
    feedback = [snooze(day, 30, subject="routine:other")
                for day in (2, 5, 9, 14, 20)]
    assert learner.propose(feedback, subject_ref=SUBJECT, now=NOW) is None


# --------------------------------------------------------------------------- #
# P14 — a rejection is remembered
# --------------------------------------------------------------------------- #
def test_p14_a_rejected_proposal_is_suppressed(learner, store):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    store.propose_from(proposal, preference_id="p1", key=SUBJECT,
                       value={"shift_minutes": 30}, now=NOW)
    store.reject("p1", proposal=proposal, now=NOW)

    later = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW + DAY)
    assert store.suppressed(later, now=NOW + DAY) is True


def test_p14_the_equivalent_proposal_is_not_queued_again(learner, store):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    store.propose_from(proposal, preference_id="p1", key=SUBJECT,
                       value={"shift_minutes": 30}, now=NOW)
    store.reject("p1", proposal=proposal, now=NOW)

    assert store.propose_from(proposal, preference_id="p2", key=SUBJECT,
                              value={"shift_minutes": 30}, now=NOW + DAY) is None


def test_p14_suppression_lasts_thirty_days(learner, store):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    store.propose_from(proposal, preference_id="p1", key=SUBJECT,
                       value={"shift_minutes": 30}, now=NOW)
    store.reject("p1", proposal=proposal, now=NOW)

    assert store.suppressed(proposal, now=NOW + 29 * DAY) is True
    assert store.suppressed(proposal, now=NOW + 31 * DAY) is False


def test_p14_a_different_shift_is_a_different_proposal(learner, store):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    store.propose_from(proposal, preference_id="p1", key=SUBJECT,
                       value={"shift_minutes": 30}, now=NOW)
    store.reject("p1", proposal=proposal, now=NOW)

    different = learner.propose(
        [snooze(day, 60) for day in (2, 5, 9, 14, 20)],
        subject_ref=SUBJECT, now=NOW + DAY)

    assert store.suppressed(different, now=NOW + DAY) is False


def test_p14_the_rejected_record_is_marked_rejected(learner, store):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    store.propose_from(proposal, preference_id="p1", key=SUBJECT,
                       value={"shift_minutes": 30}, now=NOW)
    store.reject("p1", proposal=proposal, now=NOW)

    assert store.get("p1").state is PreferenceState.REJECTED
    assert store.get("p1").governs is False


# --------------------------------------------------------------------------- #
# P15 — silence means nothing
# --------------------------------------------------------------------------- #
def test_p15_ignored_messages_produce_no_preference():
    assert inferences_from_silence([f"message-{i}" for i in range(10)]) == []


def test_p15_there_is_no_feedback_kind_for_being_ignored():
    """Silence cannot be recorded as feedback, so it cannot become evidence."""
    assert "ignore" not in {kind.value for kind in FeedbackKind}
    assert "seen" not in {kind.value for kind in FeedbackKind}


def test_p15_the_learner_never_sees_nonresponse(learner):
    """Ten unanswered notifications leave the timing learner with no samples."""
    assert learner.propose([], subject_ref=SUBJECT, now=NOW) is None


def test_p15_silence_does_not_confirm_a_pending_proposal(learner, store):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    store.propose_from(proposal, preference_id="p1", key=SUBJECT,
                       value={"shift_minutes": 30}, now=NOW)

    assert store.get("p1").state is PreferenceState.PROPOSED
    assert store.active(SUBJECT) is None


def test_p15_a_hypothesis_expires_rather_than_hardening(learner, store):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    store.propose_from(proposal, preference_id="p1", key=SUBJECT,
                       value={"shift_minutes": 30}, now=NOW)

    later = NOW + 45 * DAY
    assert store.get("p1").expired(later) is True
    assert store.hypotheses(now=later) == []


# --------------------------------------------------------------------------- #
# P16 — an explicit statement wins
# --------------------------------------------------------------------------- #
def test_p16_an_explicit_preference_governs(store):
    store.record_explicit(preference_id="x1", key="reminder_time",
                          value="09:00", now=NOW, event_id="e1")
    assert store.active("reminder_time").value == "09:00"


def test_p16_it_supersedes_a_conflicting_hypothesis(store):
    store.put(Preference(id="h1", key="reminder_time", value="10:30",
                         state=PreferenceState.PROPOSED, updated_at=NOW))
    store.record_explicit(preference_id="x1", key="reminder_time",
                          value="09:00", now=NOW + 60, event_id="e1")

    assert store.get("h1").state is PreferenceState.SUPERSEDED


def test_p16_the_superseded_hypothesis_no_longer_governs(store):
    store.put(Preference(id="h1", key="reminder_time", value="10:30",
                         state=PreferenceState.CONFIRMED, updated_at=NOW))
    store.record_explicit(preference_id="x1", key="reminder_time",
                          value="09:00", now=NOW + 60)

    assert store.active("reminder_time").value == "09:00"
    assert store.get("h1").governs is False


def test_p16_an_agreeing_record_is_not_superseded(store):
    store.put(Preference(id="h1", key="reminder_time", value="09:00",
                         state=PreferenceState.CONFIRMED, updated_at=NOW))
    store.record_explicit(preference_id="x1", key="reminder_time",
                          value="09:00", now=NOW + 60)

    assert store.get("h1").state is PreferenceState.CONFIRMED


def test_p16_a_different_scope_is_untouched(store):
    store.put(Preference(id="h1", key="reminder_time", value="10:30",
                         scope="project:loop", state=PreferenceState.CONFIRMED,
                         updated_at=NOW))
    store.record_explicit(preference_id="x1", key="reminder_time",
                          value="09:00", now=NOW + 60)

    assert store.get("h1").state is PreferenceState.CONFIRMED


def test_p16_the_explicit_statement_invalidates_queued_proposals(learner, store):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    store.propose_from(proposal, preference_id="p1", key=SUBJECT,
                       value={"shift_minutes": 30}, now=NOW)
    store.record_explicit(preference_id="x1", key=SUBJECT, value="08:15",
                          now=NOW + 60)

    assert store.queued_proposals() == []


# --------------------------------------------------------------------------- #
# P17 — forgetting
# --------------------------------------------------------------------------- #
def test_p17_the_canonical_record_is_removed(store):
    store.record_explicit(preference_id="x1", key="commute_mode", value="bike",
                          now=NOW)
    store.forget("commute_mode", now=NOW + 60)

    assert store.active("commute_mode") is None
    assert store.get("x1") is None


def test_p17_content_bearing_derivatives_are_named(store):
    store.put(Preference(id="x1", key="commute_mode", value="bike",
                         state=PreferenceState.EXPLICIT, updated_at=NOW,
                         derivative_refs=("4-journal/2026-09-01.md",
                                          "index:preferences")))
    result = store.forget("commute_mode", now=NOW + 60)

    assert result["removed_derivatives"] == ["4-journal/2026-09-01.md",
                                             "index:preferences"]


def test_p17_queued_proposals_are_invalidated(learner, store):
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    store.propose_from(proposal, preference_id="p1", key=SUBJECT,
                       value={"shift_minutes": 30}, now=NOW)
    result = store.forget(SUBJECT, now=NOW + 60)

    assert result["invalidated_proposals"] == 1
    assert store.queued_proposals() == []


def test_p17_a_forgotten_key_does_not_come_back_as_a_new_proposal(learner, store):
    store.forget(SUBJECT, now=NOW)
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW + DAY)

    assert store.propose_from(proposal, preference_id="p9", key=SUBJECT,
                              value={"shift_minutes": 30},
                              now=NOW + DAY) is None


def test_p17_forgetting_is_recorded_not_merely_archived(store):
    store.record_explicit(preference_id="x1", key="commute_mode", value="bike",
                          now=NOW)
    store.forget("commute_mode", now=NOW + 60)

    assert store.is_forgotten("commute_mode") is True
    assert all(p.key != "commute_mode" for p in store.all())


def test_p17_an_unrelated_preference_survives(store):
    store.record_explicit(preference_id="x1", key="commute_mode", value="bike",
                          now=NOW)
    store.record_explicit(preference_id="x2", key="reminder_time", value="09:00",
                          now=NOW)
    store.forget("commute_mode", now=NOW + 60)

    assert store.active("reminder_time").value == "09:00"


# --------------------------------------------------------------------------- #
# P18 — the weekly review uses canonical records
# --------------------------------------------------------------------------- #
def _records() -> list[CanonicalRecord]:
    return [CanonicalRecord(ref="project:loop", title="Loop",
                            updated_at=NOW - 2 * DAY, progress=0.4),
            CanonicalRecord(ref="project:garden", title="Garden",
                            updated_at=NOW - 40 * DAY)]


def test_p18_canonical_progress_is_used():
    review = build_review(_records(), now=NOW)
    line = review.line_for("project:loop")

    assert line.progress == 0.4
    assert line.source is ProgressSource.CANONICAL


def test_p18_a_stale_dashboard_does_not_supply_progress():
    """The number looks right and silently misstates a week of work."""
    review = build_review(
        _records(), now=NOW,
        dashboards=[DashboardSnapshot("project:garden", progress=0.9,
                                      computed_at=NOW - 30 * DAY)])
    line = review.line_for("project:garden")

    assert line.progress is None
    assert line.source is ProgressSource.UNKNOWN


def test_p18_the_stale_dashboard_is_reported_as_a_caveat():
    review = build_review(
        _records(), now=NOW,
        dashboards=[DashboardSnapshot("project:garden", progress=0.9,
                                      computed_at=NOW - 30 * DAY)])

    assert any("stale" in caveat for caveat in review.caveats)


def test_p18_missing_progress_is_stated_not_invented():
    review = build_review(_records(), now=NOW)
    rendered = render_review(review)

    assert any("progress not recorded" in line for line in rendered)
    assert not any("90%" in line for line in rendered)


def test_p18_inactivity_uses_the_thirty_day_general_threshold():
    review = build_review(_records(), now=NOW)
    assert review.inactive == ["project:garden"]


def test_p18_a_recently_updated_project_is_not_inactive():
    review = build_review(_records(), now=NOW)
    assert "project:loop" not in review.inactive


def test_p18_the_existing_fourteen_day_routine_keeps_its_own_scope():
    """Two thresholds coexist on purpose; merging them rescopes a user routine."""
    from loop.services.review import EXISTING_ROUTINE_STALENESS_DAYS, GENERAL_STALENESS_DAYS

    assert EXISTING_ROUTINE_STALENESS_DAYS == 14
    assert GENERAL_STALENESS_DAYS == 30

    narrow = build_review(_records(), now=NOW,
                          staleness_days=EXISTING_ROUTINE_STALENESS_DAYS)
    assert narrow.inactive == ["project:garden"]


def test_p18_rendering_is_deterministic():
    review = build_review(_records(), now=NOW)
    assert render_review(review) == render_review(review)


def test_p18_a_fresh_dashboard_still_does_not_override_the_record():
    review = build_review(
        [CanonicalRecord(ref="project:loop", title="Loop",
                         updated_at=NOW - DAY, progress=0.4)],
        now=NOW,
        dashboards=[DashboardSnapshot("project:loop", progress=0.95,
                                      computed_at=NOW - 60)])

    assert review.line_for("project:loop").progress == 0.4


# --------------------------------------------------------------------------- #
# M3 — the preference store survives a restart
# --------------------------------------------------------------------------- #
def test_m3_an_explicit_preference_survives_a_fresh_store(sessions):
    first = PreferenceStore(sessions=sessions)
    first.record_explicit(preference_id="x1", key="reminder_time",
                          value="09:00", now=NOW)

    second = PreferenceStore(sessions=sessions)
    assert second.active("reminder_time").value == "09:00"


def test_m3_a_confirmed_hypothesis_survives_a_restart(sessions, learner):
    first = PreferenceStore(sessions=sessions)
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    first.propose_from(proposal, preference_id="p1", key=SUBJECT,
                       value={"shift_minutes": 30}, now=NOW)
    first.confirm("p1", now=NOW)

    second = PreferenceStore(sessions=sessions)
    assert second.active(SUBJECT).state is PreferenceState.CONFIRMED


def test_m3_a_rejection_and_its_cooldown_survive_a_restart(sessions, learner):
    """The whole point of P14: a restart must not un-suppress a rejection."""
    first = PreferenceStore(sessions=sessions)
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW)
    first.propose_from(proposal, preference_id="p1", key=SUBJECT,
                       value={"shift_minutes": 30}, now=NOW)
    first.reject("p1", proposal=proposal, now=NOW)

    second = PreferenceStore(sessions=sessions)
    assert second.suppressed(proposal, now=NOW + DAY) is True


def test_m3_a_superseded_hypothesis_stays_superseded_after_restart(sessions):
    first = PreferenceStore(sessions=sessions)
    first.put(Preference(id="h1", key="reminder_time", value="10:30",
                         state=PreferenceState.CONFIRMED, updated_at=NOW))
    first.record_explicit(preference_id="x1", key="reminder_time",
                          value="09:00", now=NOW + 60)

    second = PreferenceStore(sessions=sessions)
    assert second.get("h1").state is PreferenceState.SUPERSEDED
    assert second.active("reminder_time").value == "09:00"


def test_m3_forgetting_survives_a_restart(sessions):
    """Forgetting must not come back after a restart re-hydrates the store."""
    first = PreferenceStore(sessions=sessions)
    first.record_explicit(preference_id="x1", key="commute_mode", value="bike",
                          now=NOW)
    first.forget("commute_mode", now=NOW + 60)

    second = PreferenceStore(sessions=sessions)
    assert second.active("commute_mode") is None
    assert second.get("x1") is None
    assert second.is_forgotten("commute_mode") is True


def test_m3_a_forgotten_key_still_refuses_new_proposals_after_restart(
        sessions, learner):
    first = PreferenceStore(sessions=sessions)
    first.forget(SUBJECT, now=NOW)

    second = PreferenceStore(sessions=sessions)
    proposal = learner.propose(_consistent(), subject_ref=SUBJECT, now=NOW + DAY)
    assert second.propose_from(proposal, preference_id="p9", key=SUBJECT,
                               value={"shift_minutes": 30},
                               now=NOW + DAY) is None


def test_m3_a_store_with_no_sessions_still_works_exactly_as_before():
    store = PreferenceStore()
    store.record_explicit(preference_id="x1", key="reminder_time",
                          value="09:00", now=NOW)
    assert store.active("reminder_time").value == "09:00"
