"""Connector health, observations and condition logic.

Covers P03, P04, P05, P08, P10, P11, P19.
"""

from __future__ import annotations

import pytest

from loop.connectors.health import (
    ConnectorRegistry,
    Health,
    ProviderResult,
    merge_provider_results,
)
from loop.core.errors import InvalidInput
from loop.services.observations import (
    EdgeState,
    ObservationStore,
    OverrideSet,
    Subscription,
    Truth,
    evaluate,
    wake_for,
)


@pytest.fixture
def connectors(sessions, clock) -> ConnectorRegistry:
    return ConnectorRegistry(sessions=sessions, clock=clock)


@pytest.fixture
def observations(sessions, clock) -> ObservationStore:
    return ObservationStore(sessions=sessions, clock=clock)


# --------------------------------------------------------------------------- #
# P10 — independent degradation
# --------------------------------------------------------------------------- #
def test_p10_a_successful_provider_is_retained_when_another_fails():
    merged = merge_provider_results([
        ProviderResult("google", ok=True, items=["standup"]),
        ProviderResult("outlook", ok=False, error_code="offline"),
    ])

    assert merged.items == ["standup"]
    assert merged.succeeded == ["google"]


def test_p10_the_failing_provider_is_named():
    merged = merge_provider_results([
        ProviderResult("google", ok=True, items=[]),
        ProviderResult("outlook", ok=False, error_code="auth_required"),
    ])

    assert merged.failed == {"outlook": "auth_required"}


def test_p10_a_gap_is_not_an_empty_diary():
    """The failure mode: reporting 'no meetings' because a read failed."""
    merged = merge_provider_results([
        ProviderResult("google", ok=False, error_code="offline"),
    ])

    assert merged.items == []
    assert merged.has_gap is True
    assert "not the same as having nothing scheduled" in merged.describe_gap()


def test_p10_a_complete_read_reports_no_gap():
    merged = merge_provider_results([ProviderResult("google", ok=True, items=[])])
    assert merged.complete is True
    assert merged.describe_gap() == ""


def test_p10_health_is_tracked_per_connector(connectors):
    connectors.record_success("google")
    connectors.record_failure("outlook", error_code="offline")

    assert connectors.get("google").health is Health.READY
    assert connectors.get("outlook").health is Health.OFFLINE


def test_p10_one_failure_does_not_mark_the_others_unhealthy(connectors):
    connectors.record_success("google")
    connectors.record_failure("outlook", error_code="offline")

    assert [s.connector_id for s in connectors.unhealthy()] == ["outlook"]


# --------------------------------------------------------------------------- #
# P11 — one notice per outage episode
# --------------------------------------------------------------------------- #
def test_p11_the_first_failure_warrants_a_notice(connectors):
    connectors.record_failure("outlook", error_code="offline")
    assert connectors.should_notify_outage("outlook") is True


def test_p11_subsequent_failures_do_not_renotify(connectors):
    connectors.record_failure("outlook", error_code="offline")
    connectors.mark_outage_notified("outlook")

    for _ in range(5):
        connectors.record_failure("outlook", error_code="offline")

    assert connectors.should_notify_outage("outlook") is False


def test_p11_recovery_closes_the_episode(connectors):
    connectors.record_failure("outlook", error_code="offline")
    connectors.mark_outage_notified("outlook")
    connectors.record_success("outlook")

    assert connectors.should_notify_outage("outlook") is False
    assert connectors.get("outlook").episode_started_at is None


def test_p11_a_new_outage_after_recovery_notifies_again(connectors):
    connectors.record_failure("outlook", error_code="offline")
    connectors.mark_outage_notified("outlook")
    connectors.record_success("outlook")
    connectors.record_failure("outlook", error_code="offline")

    assert connectors.should_notify_outage("outlook") is True


def test_p11_a_healthy_connector_never_notifies(connectors):
    connectors.record_success("google")
    assert connectors.should_notify_outage("google") is False


def test_consecutive_failures_are_counted(connectors):
    for _ in range(3):
        connectors.record_failure("outlook", error_code="offline")
    assert connectors.get("outlook").consecutive_failures == 3


def test_an_unconfigured_connector_is_not_an_outage(connectors):
    connectors.record_unconfigured("wrike")

    assert connectors.get("wrike").health is Health.UNCONFIGURED
    assert connectors.should_notify_outage("wrike") is False


# --------------------------------------------------------------------------- #
# P03 / P04 — freshness
# --------------------------------------------------------------------------- #
def test_p03_a_fresh_observation_resolves_to_its_value(observations):
    observations.record(key="rain_probability", subject_ref="home", value=60,
                        ttl_seconds=3600)

    truth, value = observations.resolve("rain_probability", "home")
    assert truth is Truth.TRUE
    assert value == 60


def test_p04_a_stale_observation_resolves_unknown(observations, clock):
    """Stale weather cannot satisfy a predicate."""
    observations.record(key="rain_probability", subject_ref="home", value=60,
                        ttl_seconds=60)
    clock.advance(hours=2)

    truth, value = observations.resolve("rain_probability", "home")
    assert truth is Truth.UNKNOWN
    assert value is None


def test_p04_a_missing_observation_is_unknown_not_false(observations):
    truth, _ = observations.resolve("rain_probability", "home")
    assert truth is Truth.UNKNOWN
    assert truth is not Truth.FALSE


def test_p04_an_unknown_fact_makes_the_predicate_unknown():
    result = evaluate({"fact": "rain", "op": "gte", "value": 50},
                      facts={}, missing={"rain"})
    assert result is Truth.UNKNOWN


def test_p04_unknown_does_not_fire():
    assert Truth.UNKNOWN.may_fire is False


def test_p04_a_stale_fact_cannot_produce_confident_advice(observations, clock):
    observations.record(key="rain", subject_ref="home", value=60, ttl_seconds=60)
    clock.advance(hours=3)
    truth, _ = observations.resolve("rain", "home")

    decision = evaluate({"fact": "rain", "op": "gte", "value": 50},
                        facts={}, missing={"rain"} if truth is Truth.UNKNOWN
                        else set())
    assert decision is Truth.UNKNOWN


def test_the_freshest_value_wins(observations, clock):
    observations.record(key="temp", subject_ref="home", value=10, ttl_seconds=3600)
    clock.advance(minutes=5)
    observations.record(key="temp", subject_ref="home", value=14, ttl_seconds=3600)

    _, value = observations.resolve("temp", "home")
    assert value == 14


def test_an_invalid_confidence_is_rejected(observations):
    with pytest.raises(InvalidInput):
        observations.record(key="x", subject_ref="s", value=1,
                            confidence="certain")


def test_expiring_an_observation_makes_it_unknown(observations):
    recorded = observations.record(key="x", subject_ref="s", value=1,
                                   ttl_seconds=3600)
    observations.expire_now(recorded.id)

    assert observations.resolve("x", "s")[0] is Truth.UNKNOWN


# --------------------------------------------------------------------------- #
# Three-valued logic
# --------------------------------------------------------------------------- #
def test_a_true_leaf_is_true():
    assert evaluate({"fact": "rain", "op": "gte", "value": 50},
                    facts={"rain": 60}) is Truth.TRUE


def test_a_false_leaf_is_false():
    assert evaluate({"fact": "rain", "op": "gte", "value": 50},
                    facts={"rain": 10}) is Truth.FALSE


def test_one_false_settles_a_conjunction():
    predicate = {"all": [{"fact": "a", "op": "eq", "value": 1},
                         {"fact": "b", "op": "eq", "value": 2}]}
    assert evaluate(predicate, facts={"a": 9}, missing={"b"}) is Truth.FALSE


def test_an_unknown_in_a_conjunction_is_unknown():
    predicate = {"all": [{"fact": "a", "op": "eq", "value": 1},
                         {"fact": "b", "op": "eq", "value": 2}]}
    assert evaluate(predicate, facts={"a": 1}, missing={"b"}) is Truth.UNKNOWN


def test_one_true_settles_a_disjunction():
    predicate = {"any": [{"fact": "a", "op": "eq", "value": 1},
                         {"fact": "b", "op": "eq", "value": 2}]}
    assert evaluate(predicate, facts={"a": 1}, missing={"b"}) is Truth.TRUE


def test_the_negation_of_unknown_is_unknown():
    predicate = {"not": {"fact": "a", "op": "eq", "value": 1}}
    assert evaluate(predicate, facts={}, missing={"a"}) is Truth.UNKNOWN


def test_incomparable_types_are_unknown_not_false():
    assert evaluate({"fact": "a", "op": "lt", "value": 5},
                    facts={"a": "text"}) is Truth.UNKNOWN


def test_an_unknown_operator_is_rejected():
    with pytest.raises(InvalidInput):
        evaluate({"fact": "a", "op": "eval", "value": 1}, facts={"a": 1})


# --------------------------------------------------------------------------- #
# P05 — edge triggering
# --------------------------------------------------------------------------- #
def test_p05_a_transition_into_true_fires():
    assert EdgeState().should_fire(Truth.TRUE) is True


def test_p05_a_persistently_true_condition_does_not_refire():
    edge = EdgeState()
    edge.should_fire(Truth.TRUE)

    assert [edge.should_fire(Truth.TRUE) for _ in range(5)] == [False] * 5


def test_p05_going_false_rearms_the_trigger():
    edge = EdgeState()
    edge.should_fire(Truth.TRUE)
    edge.should_fire(Truth.FALSE)

    assert edge.should_fire(Truth.TRUE) is True


def test_p05_going_unknown_also_rearms():
    edge = EdgeState()
    edge.should_fire(Truth.TRUE)
    edge.should_fire(Truth.UNKNOWN)

    assert edge.should_fire(Truth.TRUE) is True


def test_p05_an_explicit_occurrence_boundary_rearms():
    edge = EdgeState()
    edge.should_fire(Truth.TRUE)
    edge.rearm()

    assert edge.should_fire(Truth.TRUE) is True


def test_p05_unknown_never_fires():
    assert EdgeState().should_fire(Truth.UNKNOWN) is False


# --------------------------------------------------------------------------- #
# P08 — scoped expiring overrides
# --------------------------------------------------------------------------- #
def test_p08_an_override_suppresses_its_own_scope(clock):
    overrides = OverrideSet(clock=clock)
    overrides.add("commute", reason="working from home", ttl_seconds=12 * 3600)

    assert overrides.suppresses("commute") is not None


def test_p08_an_override_does_not_suppress_unrelated_scopes(clock):
    overrides = OverrideSet(clock=clock)
    overrides.add("commute", reason="working from home", ttl_seconds=12 * 3600)

    assert overrides.suppresses("medication") is None


def test_p08_a_nested_scope_is_covered(clock):
    overrides = OverrideSet(clock=clock)
    overrides.add("commute", reason="wfh", ttl_seconds=3600)

    assert overrides.suppresses("commute.weather") is not None


def test_p08_the_override_expires_on_its_own(clock):
    overrides = OverrideSet(clock=clock)
    overrides.add("commute", reason="wfh", ttl_seconds=3600)
    clock.advance(hours=2)

    assert overrides.suppresses("commute") is None
    assert overrides.active() == []


def test_p08_normal_policy_resumes_the_next_day(clock):
    overrides = OverrideSet(clock=clock)
    overrides.add("commute", reason="wfh", ttl_seconds=12 * 3600)
    clock.advance(days=1)

    assert overrides.suppresses("commute") is None


# --------------------------------------------------------------------------- #
# P19 — no unnecessary wake-ups
# --------------------------------------------------------------------------- #
def test_p19_an_event_with_no_subscription_wakes_nothing():
    subscriptions = [Subscription(id="s1", event_kind="task.changed")]
    assert wake_for(subscriptions, event_kind="vault.changed", subject={}) == []


def test_p19_only_matching_subscriptions_wake():
    subscriptions = [
        Subscription(id="s1", event_kind="task.changed"),
        Subscription(id="s2", event_kind="vault.changed"),
    ]
    woken = wake_for(subscriptions, event_kind="vault.changed", subject={})

    assert [s.id for s in woken] == ["s2"]


def test_p19_subject_filters_narrow_further():
    subscriptions = [Subscription(id="s1", event_kind="task.changed",
                                  subject_filters={"project": "loop"})]

    assert wake_for(subscriptions, event_kind="task.changed",
                    subject={"project": "other"}) == []
    assert wake_for(subscriptions, event_kind="task.changed",
                    subject={"project": "loop"})


def test_p19_a_disabled_subscription_does_not_wake():
    subscriptions = [Subscription(id="s1", event_kind="task.changed",
                                  enabled=False)]
    assert wake_for(subscriptions, event_kind="task.changed", subject={}) == []
