"""Triggers, DST resolution, occurrences and catch-up (runtime §6–§7).

Covers acceptance D07, D08, D09, D10 and the exactly-once occurrence guarantee
behind D01.
"""

from __future__ import annotations

import datetime as dt

import pytest

from loop.core.clock import UTC, to_micros
from loop.runtime.triggers import (
    CATCH_UP_WINDOW_SECONDS,
    Trigger,
    TriggerService,
    occurrence_key_for_schedule,
    resolve_local_time,
)

BERLIN = "Europe/Berlin"


@pytest.fixture
def triggers(sessions, clock) -> TriggerService:
    return TriggerService(sessions=sessions, clock=clock)


# --------------------------------------------------------------------------- #
# D07 — spring gap
# --------------------------------------------------------------------------- #
def test_d07_nonexistent_local_time_shifts_forward_by_the_gap():
    """02:30 on 2026-03-29 Europe/Berlin does not exist; it becomes 03:30."""
    instant, resolution = resolve_local_time(
        dt.date(2026, 3, 29), dt.time(2, 30), BERLIN)

    assert resolution == "gap_shifted"
    assert instant == dt.datetime(2026, 3, 29, 1, 30, tzinfo=UTC)


def test_d07_the_shifted_time_reads_as_0330_local():
    from zoneinfo import ZoneInfo

    instant, _ = resolve_local_time(dt.date(2026, 3, 29), dt.time(2, 30), BERLIN)
    local = instant.astimezone(ZoneInfo(BERLIN))

    assert (local.hour, local.minute) == (3, 30)


def test_d07_the_gap_policy_is_recorded_not_silent():
    _, resolution = resolve_local_time(dt.date(2026, 3, 29), dt.time(2, 30), BERLIN)
    assert resolution == "gap_shifted"


def test_a_normal_day_needs_no_adjustment():
    instant, resolution = resolve_local_time(
        dt.date(2026, 9, 5), dt.time(9, 0), BERLIN)

    assert resolution == "normal"
    assert instant == dt.datetime(2026, 9, 5, 7, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# D08 — autumn fold
# --------------------------------------------------------------------------- #
def test_d08_ambiguous_local_time_uses_the_first_occurrence():
    """02:30 happens twice on 2026-10-25; fire once, at fold=0 / 00:30Z."""
    instant, resolution = resolve_local_time(
        dt.date(2026, 10, 25), dt.time(2, 30), BERLIN)

    assert resolution == "fold_first"
    assert instant == dt.datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_d08_the_repeated_hour_yields_one_occurrence_key():
    """Both wall-clock 02:30s map to the same key, so only one firing survives."""
    key = occurrence_key_for_schedule(dt.date(2026, 10, 25), dt.time(2, 30),
                                      BERLIN, "fold_first")
    same = occurrence_key_for_schedule(dt.date(2026, 10, 25), dt.time(2, 30),
                                       BERLIN, "fold_first")
    assert key == same


def test_d08_a_second_claim_for_the_same_occurrence_is_refused(triggers, clock):
    trigger = triggers.create_local_schedule(
        subject_type="routine", subject_id="r1", days=["sun"], at="02:30",
        timezone=BERLIN)
    stored = Trigger(id=trigger.id, subject_type="routine", subject_id="r1",
                     kind="local_schedule", definition=trigger.definition,
                     enabled=True, revision=1)
    key = occurrence_key_for_schedule(dt.date(2026, 10, 25), dt.time(2, 30),
                                      BERLIN, "fold_first")
    nominal = dt.datetime(2026, 10, 25, 0, 30, tzinfo=UTC)

    first = triggers.claim_occurrence(stored, key, nominal_at=nominal)
    second = triggers.claim_occurrence(stored, key, nominal_at=nominal)

    assert first is not None
    assert second is None


# --------------------------------------------------------------------------- #
# Schedule computation
# --------------------------------------------------------------------------- #
def test_next_occurrence_is_strictly_after_now(triggers, clock):
    occurrence = triggers.next_occurrence_after(
        clock.now(), days=["mon", "tue", "wed", "thu", "fri"],
        wall_time=dt.time(7, 30), timezone=BERLIN)

    assert occurrence.effective_at > clock.now()


def test_weekend_days_are_skipped_for_a_weekday_schedule(triggers):
    """From Saturday, the next weekday occurrence is Monday."""
    saturday = dt.datetime(2026, 9, 5, 12, 0, tzinfo=UTC)  # 2026-09-05 is a Sat
    occurrence = triggers.next_occurrence_after(
        saturday, days=["mon", "tue", "wed", "thu", "fri"],
        wall_time=dt.time(7, 30), timezone=BERLIN)

    from zoneinfo import ZoneInfo
    assert occurrence.effective_at.astimezone(ZoneInfo(BERLIN)).weekday() == 0


def test_schedules_are_computed_by_wall_clock_not_by_adding_24_hours(triggers):
    """Across the spring change the UTC gap between occurrences is 23 hours."""
    before = dt.datetime(2026, 3, 27, 12, 0, tzinfo=UTC)  # Friday
    first = triggers.next_occurrence_after(
        before, days=["sat", "sun"], wall_time=dt.time(9, 0),
        timezone=BERLIN)
    second = triggers.next_occurrence_after(
        first.effective_at, days=["sat", "sun"], wall_time=dt.time(9, 0),
        timezone=BERLIN)

    delta = (second.effective_at - first.effective_at).total_seconds()
    assert delta == 23 * 3600


def test_an_invalid_day_is_rejected(triggers):
    from loop.core.errors import InvalidInput

    with pytest.raises(InvalidInput):
        triggers.create_local_schedule(subject_type="routine", subject_id="r1",
                                       days=["funday"], at="07:30",
                                       timezone=BERLIN)


def test_an_invalid_time_is_rejected(triggers):
    from loop.core.errors import InvalidInput

    with pytest.raises(InvalidInput):
        triggers.create_local_schedule(subject_type="routine", subject_id="r1",
                                       days=["mon"], at="7 o'clock",
                                       timezone=BERLIN)


# --------------------------------------------------------------------------- #
# Storage and due selection
# --------------------------------------------------------------------------- #
def test_a_one_shot_trigger_stores_its_instant(triggers, clock):
    when = clock.now() + dt.timedelta(days=1)
    trigger = triggers.create_at(subject_type="task", subject_id="t1",
                                 instant=when, timezone=BERLIN)

    assert trigger.next_fire_at == to_micros(when)


def test_future_triggers_are_not_due(triggers, clock):
    triggers.create_at(subject_type="task", subject_id="t1",
                       instant=clock.now() + dt.timedelta(hours=1),
                       timezone=BERLIN)
    assert triggers.due_triggers() == []


def test_a_trigger_becomes_due_once_time_passes(triggers, clock):
    triggers.create_at(subject_type="task", subject_id="t1",
                       instant=clock.now() + dt.timedelta(hours=1),
                       timezone=BERLIN)
    clock.advance(hours=2)

    due = triggers.due_triggers()
    assert len(due) == 1
    assert due[0].subject_id == "t1"


# --------------------------------------------------------------------------- #
# D09 / D10 — catch-up
# --------------------------------------------------------------------------- #
def _one_shot(clock, *, late_hours: float, catch_up: bool = True) -> Trigger:
    fire_at = clock.now() - dt.timedelta(hours=late_hours)
    return Trigger(id="t", subject_type="task", subject_id="s", kind="at",
                   definition={"catch_up": catch_up}, enabled=True, revision=1,
                   next_fire_at=to_micros(fire_at))


def test_d09_an_explicit_reminder_two_hours_late_still_fires(triggers, clock):
    decision = triggers.catch_up_decision(_one_shot(clock, late_hours=2))
    assert decision == "fire_delayed"


def test_d09_the_delayed_firing_is_labelled_not_silent(triggers, clock):
    """"delayed" is a distinct outcome so the reply can say so."""
    assert triggers.catch_up_decision(_one_shot(clock, late_hours=5)) == "fire_delayed"


def test_d09_beyond_the_24_hour_window_it_becomes_digest_material(triggers, clock):
    late = CATCH_UP_WINDOW_SECONDS / 3600 + 1
    assert triggers.catch_up_decision(_one_shot(clock, late_hours=late)) == "digest"


def test_a_trigger_with_catch_up_disabled_is_skipped(triggers, clock):
    decision = triggers.catch_up_decision(
        _one_shot(clock, late_hours=2, catch_up=False))
    assert decision == "skip"


def test_a_trigger_that_is_not_late_simply_fires(triggers, clock):
    future = Trigger(id="t", subject_type="task", subject_id="s", kind="at",
                     definition={}, enabled=True, revision=1,
                     next_fire_at=to_micros(clock.now() + dt.timedelta(hours=1)))
    assert triggers.catch_up_decision(future) == "fire"


def test_d10_a_stale_routine_morning_is_skipped(triggers, clock):
    """Runtime §7: do not send a forecast for a departure already passed."""
    stale = Trigger(id="t", subject_type="routine", subject_id="r",
                    kind="local_schedule", definition={"catch_up": "skip"},
                    enabled=True, revision=1,
                    next_fire_at=to_micros(clock.now() - dt.timedelta(hours=5)))
    assert triggers.catch_up_decision(stale) == "skip"


def test_d10_a_routine_within_its_grace_window_still_runs(triggers, clock):
    recent = Trigger(id="t", subject_type="routine", subject_id="r",
                     kind="local_schedule", definition={"catch_up": "skip"},
                     enabled=True, revision=1,
                     next_fire_at=to_micros(clock.now() - dt.timedelta(minutes=10)))
    assert triggers.catch_up_decision(recent) == "fire"


def test_d10_five_missed_mornings_do_not_replay_a_backlog(triggers, sessions, clock):
    """Each stale morning is skipped, so no backlog of forecasts is delivered."""
    decisions = []
    for days_ago in range(1, 6):
        stale = Trigger(id=f"t{days_ago}", subject_type="routine",
                        subject_id="r", kind="local_schedule",
                        definition={"catch_up": "skip"}, enabled=True,
                        revision=1,
                        next_fire_at=to_micros(
                            clock.now() - dt.timedelta(days=days_ago)))
        decisions.append(triggers.catch_up_decision(stale))

    assert decisions == ["skip"] * 5


# --------------------------------------------------------------------------- #
# Revision is part of occurrence identity
# --------------------------------------------------------------------------- #
def test_a_new_revision_may_fire_the_same_occurrence_key(triggers, clock):
    """Editing a routine must not be blocked by its old firings."""
    created = triggers.create_local_schedule(
        subject_type="routine", subject_id="r", days=["mon"], at="07:30",
        timezone=BERLIN)
    trigger_v1 = Trigger(id=created.id, subject_type="routine", subject_id="r",
                         kind="local_schedule", definition={}, enabled=True,
                         revision=1)
    trigger_v2 = Trigger(id=created.id, subject_type="routine", subject_id="r",
                         kind="local_schedule", definition={}, enabled=True,
                         revision=2)
    key = "2026-09-07T07:30@Europe/Berlin#0"
    nominal = clock.now()

    assert triggers.claim_occurrence(trigger_v1, key, nominal_at=nominal)
    assert triggers.claim_occurrence(trigger_v2, key, nominal_at=nominal)
