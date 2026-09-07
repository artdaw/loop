"""Condition routines fire on the edge, and remember it (P05).

`observations.py` shipped complete and unreachable: a routine with
`trigger.kind: condition` could be written, validated and activated while never
running. These tests run the whole path through `build_application()`.

The load-bearing test is the restart one. `EdgeState` is an in-memory
dataclass, so a process that forgot it would see `unknown → true` on its next
sweep, call that an edge, and announce the same condition again — every deploy,
invisibly to any test that never restarts.
"""

from __future__ import annotations

import pytest

from loop.app import build_application
from loop.runtime.routines import parse_routine
from loop.services.observations import Truth

SUBJECT = "home"

DOOR_ROUTINE = """---
schema_version: 1
id: door-left-open
title: Door left open
trigger:
  kind: condition
  subject_ref: home
  predicate:
    all:
      - fact: door
        op: eq
        value: open
steps:
  - capability: reminder.schedule
    arguments:
      query: close the door
notification:
  mode: each_occurrence
  destination: "4242"
---

Tell me when the door is left open.
"""


@pytest.fixture
def app(settings, clock):
    application = build_application(settings, clock=clock)
    routine, problems = parse_routine(
        DOOR_ROUTINE, known_capabilities=application.known_capabilities())
    assert problems == [], problems
    application.routines.save(routine)
    application.routine_scheduler.activate("door-left-open",
                                           activation_event_id="evt-activate")
    return application


def observe(app, value: str, *, ttl_seconds: int = 3600) -> None:
    app.observations.record(key="door", subject_ref=SUBJECT, value=value,
                            ttl_seconds=ttl_seconds)


# --------------------------------------------------------------------------- #
# Firing on the edge
# --------------------------------------------------------------------------- #
def test_a_condition_with_no_observation_is_unknown_not_false(app):
    """Not knowing is not knowing it is shut."""
    outcomes = app.conditions.sweep()

    assert len(outcomes) == 1
    assert outcomes[0].truth is Truth.UNKNOWN
    assert outcomes[0].missing == ["door"]
    assert not outcomes[0].fired


def test_becoming_true_fires_once(app):
    observe(app, "open")

    outcomes = app.conditions.sweep()

    assert outcomes[0].truth is Truth.TRUE
    assert outcomes[0].fired
    assert app.service.pending_summary()["jobs"] == 1


def test_staying_true_does_not_fire_again(app):
    """P05's actual sentence: repeated updates, one notification."""
    observe(app, "open")
    app.conditions.sweep()

    for _ in range(5):
        observe(app, "open")
        outcome = app.conditions.sweep()[0]
        assert outcome.truth is Truth.TRUE
        assert not outcome.fired
        assert "already announced" in outcome.reason

    assert app.service.pending_summary()["jobs"] == 1


def test_going_false_rearms_it(app):
    observe(app, "open")
    app.conditions.sweep()
    observe(app, "shut")
    app.conditions.sweep()

    observe(app, "open")
    outcome = app.conditions.sweep()[0]

    assert outcome.fired
    assert app.service.pending_summary()["jobs"] == 2


def test_lapsing_into_unknown_also_rearms_it(app, clock):
    """A sensor that goes quiet and comes back is worth announcing again."""
    observe(app, "open", ttl_seconds=60)
    app.conditions.sweep()

    clock.advance(minutes=5)                 # the observation goes stale
    stale = app.conditions.sweep()[0]
    assert stale.truth is Truth.UNKNOWN

    observe(app, "open")
    assert app.conditions.sweep()[0].fired


def test_a_stale_observation_is_not_treated_as_current(app, clock):
    observe(app, "open", ttl_seconds=60)
    app.conditions.sweep()
    clock.advance(minutes=5)

    outcome = app.conditions.sweep()[0]

    assert outcome.truth is Truth.UNKNOWN
    assert outcome.missing == ["door"]


# --------------------------------------------------------------------------- #
# The edge is durable
# --------------------------------------------------------------------------- #
def test_a_restart_does_not_re_announce_a_condition_still_true(settings, clock,
                                                               app):
    """The failure this prevents repeats on every deploy, silently."""
    observe(app, "open")
    assert app.conditions.sweep()[0].fired
    queued = app.service.pending_summary()["jobs"]

    restarted = build_application(settings, clock=clock)
    outcome = restarted.conditions.sweep()[0]

    assert outcome.truth is Truth.TRUE
    assert not outcome.fired, "a restart re-announced a condition already true"
    assert restarted.service.pending_summary()["jobs"] == queued


def test_a_restart_still_notices_a_genuine_new_edge(settings, clock, app):
    """Durability must not freeze the edge, only remember it."""
    observe(app, "open")
    app.conditions.sweep()
    observe(app, "shut")
    app.conditions.sweep()

    restarted = build_application(settings, clock=clock)
    observe(restarted, "open")

    assert restarted.conditions.sweep()[0].fired


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
def test_a_paused_routine_is_not_evaluated(app):
    observe(app, "open")
    app.routine_scheduler.pause("door-left-open")

    assert app.conditions.sweep() == []
    assert app.service.pending_summary()["jobs"] == 0


def test_a_clock_routine_is_not_swept_as_a_condition(settings, clock):
    """Each dispatcher owns its own trigger kind."""
    application = build_application(settings, clock=clock)
    document = DOOR_ROUTINE.replace(
        "  kind: condition\n  subject_ref: home\n"
        "  predicate:\n    all:\n      - fact: door\n        op: eq\n"
        "        value: open\n",
        "  kind: local_schedule\n  days: [mon]\n  at: \"07:00\"\n"
        "  timezone: Europe/Berlin\n")
    routine, problems = parse_routine(
        document, known_capabilities=application.known_capabilities())
    assert problems == [], problems
    application.routines.save(routine)
    application.routine_scheduler.activate("door-left-open",
                                           activation_event_id="evt")

    assert application.conditions.sweep() == []


def test_the_sweep_reaches_no_model(app):
    observe(app, "open")

    app.conditions.sweep()

    assert app.model_gateway.audit.cloud_calls == 0
    assert app.model_gateway.audit.local_calls == 0
