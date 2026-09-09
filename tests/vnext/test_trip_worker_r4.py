"""A scheduled trip check actually runs (TR15–TR19 — R4).

`TripMonitorScheduler` turned an approved plan into durable `trip.check` jobs
and nothing consumed them. Monitoring scheduled work that never executed, and
because nothing ever failed, nothing ever said so.

The theme running through these is that "nothing changed" is a *conclusion*,
not a default. An outage, a withdrawn approval and a genuinely quiet check all
end with no notification, and only one of them means the trip is fine.
"""

from __future__ import annotations

import datetime as dt

import pytest

from loop.app import build_application
from loop.capabilities.travel.monitor import (
    ChangeKind,
    Check,
    QuoteBasis,
    plan_monitoring,
)
from loop.core.clock import UTC
from loop.runtime.outbox import FakeTransport
from loop.runtime.trip_monitor import TRIP_JOB_KIND
from loop.runtime.trip_worker import CheckOutcome, TripCheckSource

TRIP = "trip-lisbon"
DEPARTURE = dt.datetime(2026, 9, 20, 9, 0)
TRAVEL_DAYS = [dt.date(2026, 9, 20), dt.date(2026, 9, 21)]


@pytest.fixture
def sends() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def app(settings, clock, sends):
    return build_application(settings, clock=clock,
                             transports={"telegram": sends})


def a_plan(checks: list[str] | None = None, *, option_id: str = "opt-1",
           revision_id: str = "rev-1"):
    return plan_monitoring(
        trip_id=TRIP, revision_id=revision_id, option_id=option_id,
        requested_checks=checks, departure_local=DEPARTURE,
        travel_days=TRAVEL_DAYS, timezone="Europe/Lisbon",
        now_utc=dt.datetime(2026, 9, 5, 7, 0, tzinfo=UTC),
        owner_timezone="Europe/Berlin")


def queue_a_check(app, checks: list[str] | None = None, **kw) -> None:
    """Activate monitoring and let a checkpoint fire."""
    app.trip_monitor.activate(a_plan(checks, **kw),
                              activation_event_id="evt-approved")
    first = min(t.next_fire_at for t in app.trip_monitor.pending_checks(TRIP))
    app.clock.set(dt.datetime.fromtimestamp(first / 1_000_000, UTC))
    app.service.tick()


class Delayed(TripCheckSource):
    name = "fixture-schedule"

    def read(self, *, trip_id, option_id, check):
        del trip_id, option_id
        if check is not Check.SCHEDULE:
            return None
        return {"before_minutes": 40, "after_minutes": 95}


class Unchanged(TripCheckSource):
    name = "fixture-schedule"

    def read(self, *, trip_id, option_id, check):
        del trip_id, option_id, check
        return {"before_minutes": 40, "after_minutes": 42}


class Broken(TripCheckSource):
    name = "fixture-broken"

    def read(self, *, trip_id, option_id, check):
        del trip_id, option_id, check
        return None

    def unavailable_reason(self, check):
        return f"the {check.value} provider returned an error"


# --------------------------------------------------------------------------- #
# TR15 — a queued check is actually executed
# --------------------------------------------------------------------------- #
def test_a_queued_check_is_claimed_and_produces_a_result(app):
    queue_a_check(app, ["schedule"])
    app.trip_worker.sources = {"schedule": Unchanged()}

    result = app.trip_worker.run_one()

    assert result is not None
    assert result.trip_id == TRIP
    assert result.checks_run == ["schedule"]
    assert result.outcome == CheckOutcome.NO_CHANGE
    persisted = app.jobs.get(result.job_id)
    assert persisted is not None
    assert persisted.result is not None
    assert persisted.result["outcome"] == CheckOutcome.NO_CHANGE
    assert persisted.result["checks_run"] == ["schedule"]
    assert app.jobs.claim("w", kinds=[TRIP_JOB_KIND]) is None


def test_the_check_survives_a_restart_and_runs_in_the_new_process(settings,
                                                                  clock, sends):
    first = build_application(settings, clock=clock,
                              transports={"telegram": sends})
    queue_a_check(first, ["schedule"])

    restarted = build_application(settings, clock=clock,
                                  transports={"telegram": sends})
    restarted.trip_worker.sources = {"schedule": Unchanged()}
    result = restarted.trip_worker.run_one()

    assert result is not None
    assert result.checks_run == ["schedule"]
    persisted = restarted.jobs.get(result.job_id)
    assert persisted is not None and persisted.result == result.as_record()


# --------------------------------------------------------------------------- #
# TR16 — a material change notifies once; a no-change check calls no model
# --------------------------------------------------------------------------- #
def test_a_material_change_produces_one_notice_with_before_and_after(app, sends):
    queue_a_check(app, ["schedule"])
    app.trip_worker.sources = {"schedule": Delayed()}

    result = app.trip_worker.run_one()

    assert result.outcome == CheckOutcome.SUCCESS
    assert result.material
    assert result.notified, "a material change produced no notice"
    app.service.tick()
    assert len(sends.sent) == 1
    payload = sends.sent[0]["payload"]
    assert payload["before"] == "40" and payload["after"] == "95"


def test_the_same_change_seen_twice_notifies_once(app, sends):
    queue_a_check(app, ["schedule"])
    app.trip_worker.sources = {"schedule": Delayed()}
    app.trip_worker.run_one()
    app.service.tick()

    # A second checkpoint reporting the identical change.
    later = min((t.next_fire_at for t in app.trip_monitor.pending_checks(TRIP)),
                default=None)
    if later:
        app.clock.set(dt.datetime.fromtimestamp(later / 1_000_000, UTC))
        app.service.tick()
        app.trip_worker.run_one()
        app.service.tick()

    assert len(sends.sent) == 1, "the same change was reported twice"


def test_a_no_change_check_invokes_no_model(app):
    """Travel §7. A model here would spend budget forever to conclude nothing."""
    queue_a_check(app, ["schedule"])
    app.trip_worker.sources = {"schedule": Unchanged()}

    result = app.trip_worker.run_one()

    assert result.outcome == CheckOutcome.NO_CHANGE
    assert app.model_gateway.audit.cloud_calls == 0
    assert app.model_gateway.audit.local_calls == 0


# --------------------------------------------------------------------------- #
# An unavailable source is never silence
# --------------------------------------------------------------------------- #
def test_a_check_with_no_configured_provider_reports_unavailable(app, sends):
    """The failure this outcome exists to prevent: nobody looked, and the
    owner heard nothing and concluded the trip was fine."""
    queue_a_check(app, ["cost"])
    app.trip_worker.sources = {}

    result = app.trip_worker.run_one()

    assert result.outcome == CheckOutcome.FAILED
    assert "cost" in result.checks_unavailable
    assert "account" in result.checks_unavailable["cost"]
    assert sends.sent == []


def test_some_sources_answering_is_partial_not_success(app):
    queue_a_check(app, ["schedule", "cost"])
    app.trip_worker.sources = {"schedule": Unchanged(), "cost": Broken()}

    result = app.trip_worker.run_one()

    assert result.outcome == CheckOutcome.PARTIAL
    assert result.checks_run == ["schedule"]
    assert "cost" in result.checks_unavailable


# --------------------------------------------------------------------------- #
# TR17 — a quote whose basis changed is not a price change
# --------------------------------------------------------------------------- #
def test_a_quote_on_a_different_basis_is_not_reported_as_a_price_change(app,
                                                                       sends):
    class DifferentBasis(TripCheckSource):
        def read(self, *, trip_id, option_id, check):
            del trip_id, option_id
            if check is not Check.COST:
                return None
            return {
                "before_minor": 20000, "after_minor": 32000, "currency": "EUR",
                "before_basis": QuoteBasis("2026-09-20", "2026-09-22", 2),
                # Three travellers and different dates: not the same thing.
                "after_basis": QuoteBasis("2026-09-21", "2026-09-24", 3),
            }

    queue_a_check(app, ["cost"])
    app.trip_worker.sources = {"cost": DifferentBasis()}

    result = app.trip_worker.run_one()

    assert not result.material, "an incomparable quote was reported as a change"
    kinds = {change.kind for change in result.changes}
    assert ChangeKind.COST not in kinds
    app.service.tick()
    assert sends.sent == []


# --------------------------------------------------------------------------- #
# TR19 — withdrawn approval stops the check
# --------------------------------------------------------------------------- #
def test_a_check_queued_before_cancellation_does_not_run(app, sends):
    queue_a_check(app, ["schedule"])
    app.trip_worker.sources = {"schedule": Delayed()}
    app.trip_monitor.deactivate(TRIP)

    result = app.trip_worker.run_one()

    assert result.outcome == CheckOutcome.NO_CHANGE
    assert "monitoring was stopped" in result.reason
    assert result.checks_run == []
    app.service.tick()
    assert sends.sent == []


def test_a_check_for_a_superseded_revision_does_not_run(app, sends):
    """Selecting a new revision must not leave the old one still reporting."""
    queue_a_check(app, ["schedule"])
    app.trip_worker.sources = {"schedule": Delayed()}
    # The owner selects a different option; monitoring is re-approved for it.
    app.trip_monitor.activate(a_plan(["schedule"], option_id="opt-2"),
                              activation_event_id="evt-reselected")

    result = app.trip_worker.run_one()

    assert result.outcome == CheckOutcome.NO_CHANGE
    assert "different revision or option" in result.reason
    assert sends.sent == []


def test_a_check_removed_from_scope_is_not_run(app):
    """The scope narrowed after the job was queued."""
    queue_a_check(app, ["schedule", "cost"])
    app.trip_monitor.deactivate(TRIP)
    app.trip_monitor.activate(a_plan(["schedule"]),
                              activation_event_id="evt-narrowed")
    app.trip_worker.sources = {"schedule": Unchanged(), "cost": Unchanged()}

    result = app.trip_worker.run_one()

    assert result.checks_run == ["schedule"]
    assert "cost" not in result.checks_run


def test_a_failing_source_fails_the_job_rather_than_claiming_success(app):
    class Raises(TripCheckSource):
        def read(self, *, trip_id, option_id, check):
            raise RuntimeError("provider exploded")

    queue_a_check(app, ["schedule"])
    app.trip_worker.sources = {"schedule": Raises()}

    result = app.trip_worker.run_one()

    assert result.outcome == CheckOutcome.FAILED
    assert "provider exploded" in result.reason
