"""M6: snooze → proposal → confirmation → the routine actually moves (vault §9).

`learning.py` held every rule and nothing fed it. These tests exercise the
whole loop through `build_application()`: real feedback rows in SQLite, the
real timing learner, real preference records, real vault documents, and a real
trigger whose next firing changes.

The rules being protected here are mostly refusals — never auto-apply, never
treat silence as agreement, never let a rejection be re-asked, and never let
"forget" leave a readable copy behind.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from loop.app import build_application
from loop.core.clock import UTC
from loop.core.errors import InvalidInput
from loop.core.settings import Settings
from loop.runtime.routine_dispatch import ROUTINE_SUBJECT
from loop.runtime.routines import parse_routine
from loop.services.learning import DAY, PreferenceState
from tests.vnext.vault_fixtures import build_minimal_vault

SUBJECT = "routine:morning-weather"

MORNING_WEATHER = """---
schema_version: 1
id: morning-weather
title: Morning weather
trigger:
  kind: local_schedule
  days: [mon, tue, wed, thu, fri, sat, sun]
  at: "07:00"
  timezone: Europe/Berlin
steps:
  - capability: weather.prepare
    arguments:
      location_ref: home
notification:
  mode: each_occurrence
  destination: "4242"
---

Every morning.
"""


@pytest.fixture
def vault(tmp_path: Path):
    return build_minimal_vault(tmp_path / "vault")


@pytest.fixture
def vault_settings(settings: Settings, vault) -> Settings:
    return settings.model_copy(update={"obsidian_vault_path": str(vault.root)})


@pytest.fixture
def app(vault_settings, clock):
    application = build_application(vault_settings, clock=clock)
    routine, problems = parse_routine(
        MORNING_WEATHER, known_capabilities=application.known_capabilities())
    assert problems == []
    application.routines.save(routine)
    application.routine_scheduler.activate("morning-weather",
                                           activation_event_id="evt-activate")
    return application


def epoch(day: int, *, hour: int = 6) -> int:
    """A distinct day, at a plausible snooze hour."""
    return int(dt.datetime(2026, 9, day, hour, tzinfo=UTC).timestamp())


def snooze_five_days(app, *, minutes: int = 30, start_day: int = 1) -> int:
    """The learner's own threshold: five snoozes on distinct days."""
    for index in range(5):
        app.feedback.record_snooze(
            SUBJECT, event_id=f"evt-{index}", shift_minutes=minutes,
            occurred_at=epoch(start_day + index))
    return epoch(start_day + 4)


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def test_feedback_survives_a_restart(vault_settings, clock, app):
    """The learner's window is 28 days; in-memory feedback never reaches it."""
    snooze_five_days(app)

    restarted = build_application(vault_settings, clock=clock)

    assert len(restarted.feedback.log.since(SUBJECT, since=0)) == 5


def test_the_same_event_cannot_be_counted_twice(app):
    """A redelivered update would otherwise be a second, false sample."""
    assert app.feedback.record_snooze(SUBJECT, event_id="e1", shift_minutes=30,
                                      occurred_at=epoch(1)) is True
    assert app.feedback.record_snooze(SUBJECT, event_id="e1", shift_minutes=30,
                                      occurred_at=epoch(1)) is False

    assert len(app.feedback.log.since(SUBJECT, since=0)) == 1


def test_a_snooze_must_move_something_forward(app):
    with pytest.raises(InvalidInput):
        app.feedback.record_snooze(SUBJECT, event_id="e", shift_minutes=0)


# --------------------------------------------------------------------------- #
# Proposing — never applying
# --------------------------------------------------------------------------- #
def test_too_few_snoozes_propose_nothing_and_say_why(app):
    for index in range(3):
        app.feedback.record_snooze(SUBJECT, event_id=f"e{index}",
                                   shift_minutes=30, occurred_at=epoch(1 + index))

    outcome = app.feedback.review(SUBJECT, now=epoch(6))

    assert outcome.proposes is False
    assert "thresholds" in outcome.reason


def test_a_consistent_pattern_produces_a_question_not_a_change(app):
    now = snooze_five_days(app)

    outcome = app.feedback.review(SUBJECT, now=now)

    assert outcome.proposes
    assert outcome.proposal.shift_minutes == 30
    assert "Shall I move it" in outcome.question
    # Proposed, not applied: the routine still fires at its original time.
    assert app.routines.get("morning-weather").trigger["at"] == "07:00"
    record = app.preferences.get(outcome.preference_id)
    assert record.state is PreferenceState.PROPOSED
    assert record.is_hypothesis


def test_a_proposal_is_written_as_a_hypothesis_not_as_a_preference(app, vault):
    """A hypothesis filed among stated preferences reads as something said."""
    now = snooze_five_days(app)

    app.feedback.review(SUBJECT, now=now)

    hypotheses = list((vault.root / "_mem/loop/hypotheses").glob("*.md"))
    assert len(hypotheses) == 1
    body = hypotheses[0].read_text(encoding="utf-8")
    assert "type: hypothesis" in body
    assert "state: proposed" in body
    assert "inference from observed behaviour, not something you stated" in body
    assert not (vault.root / "_mem/loop/preferences").exists()


def test_nonresponse_is_not_agreement(app):
    """No elapsed time turns an unanswered proposal into a confirmed one."""
    now = snooze_five_days(app)
    outcome = app.feedback.review(SUBJECT, now=now)

    much_later = now + 90 * DAY
    app.feedback.review(SUBJECT, now=much_later)

    assert app.preferences.get(outcome.preference_id).state is \
        PreferenceState.PROPOSED
    assert app.routines.get("morning-weather").trigger["at"] == "07:00"


# --------------------------------------------------------------------------- #
# Confirming actually moves the schedule
# --------------------------------------------------------------------------- #
def test_confirming_moves_the_routine_and_its_live_trigger(app):
    now = snooze_five_days(app)
    outcome = app.feedback.review(SUBJECT, now=now)
    before = app.triggers.for_subject(ROUTINE_SUBJECT, "morning-weather")[0]

    record, applied = app.feedback.confirm(outcome.preference_id, now=now)

    assert record.state is PreferenceState.CONFIRMED
    assert "07:00 to 07:30" in applied
    assert app.routines.get("morning-weather").trigger["at"] == "07:30"

    after = app.triggers.for_subject(ROUTINE_SUBJECT, "morning-weather")
    assert len(after) == 1, "exactly one live schedule"
    assert after[0].id != before.id, "a new trigger, so old firings stay valid"
    assert after[0].definition["at"] == "07:30"
    assert after[0].next_fire_at > before.next_fire_at


def test_confirming_replaces_the_hypothesis_document_with_a_preference(app, vault):
    now = snooze_five_days(app)
    outcome = app.feedback.review(SUBJECT, now=now)

    app.feedback.confirm(outcome.preference_id, now=now)

    assert list((vault.root / "_mem/loop/hypotheses").glob("*.md")) == []
    preferences = list((vault.root / "_mem/loop/preferences").glob("*.md"))
    assert len(preferences) == 1
    assert "state: confirmed" in preferences[0].read_text(encoding="utf-8")


def test_a_confirmed_shift_survives_a_restart(vault_settings, clock, app):
    now = snooze_five_days(app)
    outcome = app.feedback.review(SUBJECT, now=now)
    app.feedback.confirm(outcome.preference_id, now=now)

    restarted = build_application(vault_settings, clock=clock)

    assert restarted.routines.get("morning-weather").trigger["at"] == "07:30"
    live = restarted.triggers.for_subject(ROUTINE_SUBJECT, "morning-weather")
    assert [t.definition["at"] for t in live] == ["07:30"]


def test_a_shift_past_midnight_wraps_rather_than_becoming_an_invalid_time(app):
    app.routine_scheduler.shift_schedule("morning-weather", minutes=-8 * 60)

    assert app.routines.get("morning-weather").trigger["at"] == "23:00"


def test_a_routine_with_no_wall_clock_schedule_cannot_be_shifted(app):
    document = MORNING_WEATHER.replace(
        "  kind: local_schedule\n  days: [mon, tue, wed, thu, fri, sat, sun]\n"
        '  at: "07:00"\n', "  kind: event\n").replace("id: morning-weather",
                                                      "id: on-arrival")
    routine, problems = parse_routine(
        document, known_capabilities=app.known_capabilities())
    assert problems == []
    app.routines.save(routine)

    with pytest.raises(InvalidInput):
        app.routine_scheduler.shift_schedule("on-arrival", minutes=15)


# --------------------------------------------------------------------------- #
# Declining
# --------------------------------------------------------------------------- #
def test_declining_suppresses_the_same_proposal_for_the_policy_window(app):
    now = snooze_five_days(app)
    outcome = app.feedback.review(SUBJECT, now=now)

    app.feedback.reject(outcome.preference_id, proposal=outcome.proposal, now=now)
    again = app.feedback.review(SUBJECT, now=now + 5 * DAY)

    assert again.proposes is False
    assert again.suppressed
    assert "declined" in again.reason
    assert app.routines.get("morning-weather").trigger["at"] == "07:00"


def test_declining_removes_the_hypothesis_document(app, vault):
    now = snooze_five_days(app)
    outcome = app.feedback.review(SUBJECT, now=now)
    assert list((vault.root / "_mem/loop/hypotheses").glob("*.md"))

    app.feedback.reject(outcome.preference_id, proposal=outcome.proposal, now=now)

    assert list((vault.root / "_mem/loop/hypotheses").glob("*.md")) == []


def test_a_decline_is_not_permanent_silence_on_the_subject(app):
    """The owner said no to *this* change, not to ever discussing timing (P14)."""
    now = snooze_five_days(app)
    outcome = app.feedback.review(SUBJECT, now=now)
    app.feedback.reject(outcome.preference_id, proposal=outcome.proposal, now=now)

    later = now + 40 * DAY
    for index in range(5):
        app.feedback.record_snooze(SUBJECT, event_id=f"later-{index}",
                                   shift_minutes=30,
                                   occurred_at=later + index * DAY)

    assert app.feedback.review(SUBJECT, now=later + 5 * DAY).proposes


# --------------------------------------------------------------------------- #
# Explicit statements and forgetting
# --------------------------------------------------------------------------- #
def test_an_explicit_statement_is_recorded_as_stated_not_inferred(app, vault):
    record = app.feedback.record_explicit(
        "notify.quiet_weekends", True, event_id="evt-said", now=epoch(1))

    assert record.state is PreferenceState.EXPLICIT
    document = (vault.root / "_mem/loop/preferences"
                / "notify.quiet_weekends--global.md").read_text(encoding="utf-8")
    assert "type: preference" in document
    assert "Recorded from your own instruction" in document


def test_forgetting_removes_the_record_its_document_and_its_evidence(app, vault):
    """Archiving a private copy is not forgetting (vault §9, P17)."""
    now = snooze_five_days(app)
    app.feedback.review(SUBJECT, now=now)
    assert list((vault.root / "_mem/loop/hypotheses").glob("*.md"))

    result = app.feedback.forget(SUBJECT, now=now)

    assert result["removed_records"]
    assert result["removed_documents"]
    assert result["removed_feedback"] == 5
    assert list((vault.root / "_mem/loop/hypotheses").glob("*.md")) == []
    assert app.feedback.log.since(SUBJECT, since=0) == []


def test_forgetting_stops_the_same_belief_being_re_derived(app):
    """Leaving the snoozes behind would let the next review recreate it."""
    now = snooze_five_days(app)
    app.feedback.review(SUBJECT, now=now)
    app.feedback.forget(SUBJECT, now=now)

    again = app.feedback.review(SUBJECT, now=now)

    assert again.proposes is False
    assert again.suppressed
    assert "forgotten" in again.reason


def test_forgetting_survives_a_restart(vault_settings, clock, app):
    now = snooze_five_days(app)
    app.feedback.review(SUBJECT, now=now)
    app.feedback.forget(SUBJECT, now=now)

    restarted = build_application(vault_settings, clock=clock)

    assert restarted.preferences.is_forgotten(SUBJECT)
    assert restarted.feedback.log.since(SUBJECT, since=0) == []


def test_reviewing_every_subject_reports_questions_and_quiet_ones(app):
    now = snooze_five_days(app)
    app.feedback.record_snooze("routine:other", event_id="o1", shift_minutes=10,
                               occurred_at=now)

    from loop.services.feedback import summarise
    report = summarise(app.feedback.review_all(now=now))

    assert report.has_questions
    assert [o.subject_ref for o in report.proposals] == [SUBJECT]
    assert [o.subject_ref for o in report.quiet] == ["routine:other"]


def test_snoozes_older_than_the_window_do_not_count(app):
    """The learner's rule is five snoozes *within 28 days* (vault §9).

    Without the window, a habit the owner had months ago and has since dropped
    would still be proposed as though it were current — and every subject
    would eventually accumulate enough history to trigger something.
    """
    now = epoch(20, hour=6) + 60 * DAY
    for index in range(4):
        app.feedback.record_snooze(SUBJECT, event_id=f"old-{index}",
                                   shift_minutes=30,
                                   occurred_at=now - (40 + index) * DAY)
    for index in range(2):
        app.feedback.record_snooze(SUBJECT, event_id=f"recent-{index}",
                                   shift_minutes=30,
                                   occurred_at=now - (1 + index) * DAY)

    outcome = app.feedback.review(SUBJECT, now=now)

    # Six snoozes in the log, but only two inside the window.
    assert len(app.feedback.log.since(SUBJECT, since=0)) == 6
    assert len(app.feedback.log.since(SUBJECT, since=now - 28 * DAY)) == 2
    assert outcome.proposes is False
    assert "thresholds" in outcome.reason
