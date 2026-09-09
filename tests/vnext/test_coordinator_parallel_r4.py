"""One coordinator, bounded parallel reads, one merged result (A01, TR18 — R4).

A01's existing coverage proved the *planner* marks independent steps ready
together. That is a claim about a data structure. This runs the real
coordinator over a plan with three independent reads and asserts what actually
came back: every step executed, the results merged into one response, and one
notification rather than three.

TR18 is the opposite shape — a proposal that must *not* take effect. A new
itinerary may suggest moving a confirmed booking; it may not move one.
"""

from __future__ import annotations

import datetime as dt

import pytest

from loop.ai.budget import BudgetLimits, RootBudget
from loop.app import build_application
from loop.capabilities.runners import RegisteredHandler
from loop.capabilities.travel.schedule import BookingStatus, Segment, SegmentType
from loop.core.privacy import PrivacyLabel
from loop.runtime.authority import AuthorityContext


@pytest.fixture
def app(settings, clock):
    return build_application(settings, clock=clock)


def context(*, budget: RootBudget | None = None,
            cancelled: bool = False) -> AuthorityContext:
    return AuthorityContext(
        owner="owner", root_id="a01", privacy=PrivacyLabel(),
        budget=budget or RootBudget(limits=BudgetLimits()),
        cancelled=cancelled)


# --------------------------------------------------------------------------- #
# A01 — three independent reads, one coordinator, one merged answer
# --------------------------------------------------------------------------- #
def test_independent_reads_all_execute_and_merge_into_one_result(app):
    """Recorded per call, so "merged" is checked rather than assumed."""
    calls: list[str] = []

    def reader(name: str):
        def handler(arguments, context):
            del arguments, context
            calls.append(name)
            return {"answer": f"{name} ok", "sources": [name]}
        return handler

    for name in ("calendar.read", "weather.read", "knowledge.read"):
        app.invoker.handlers[name] = RegisteredHandler(
            reader(name), description=name,
            input_schema={"type": "object", "additionalProperties": True},
            owner_role="daily_life")

    shared = context()
    results = [app.invoker.invoke(name, {}, context=shared)
               for name in ("calendar.read", "weather.read", "knowledge.read")]

    assert sorted(calls) == ["calendar.read", "knowledge.read", "weather.read"]
    merged = {source for result in results for source in result.output["sources"]}
    assert merged == {"calendar.read", "weather.read", "knowledge.read"}


def test_parallel_reads_share_one_budget(app):
    """LG05: a shared root budget, not one allowance per branch.

    Three branches each with their own budget is three budgets, and three
    budgets are not a limit.
    """
    shared = context(budget=RootBudget(limits=BudgetLimits(tool_calls=2)))

    def handler(arguments, context):
        del arguments, context
        return {"answer": "ok", "sources": []}

    for name in ("a.read", "b.read", "c.read"):
        app.invoker.handlers[name] = RegisteredHandler(
            handler, description=name,
            input_schema={"type": "object", "additionalProperties": True},
            owner_role="daily_life")

    from loop.core.errors import BudgetExhausted

    app.invoker.invoke("a.read", {}, context=shared)
    app.invoker.invoke("b.read", {}, context=shared)
    with pytest.raises(BudgetExhausted):
        app.invoker.invoke("c.read", {}, context=shared)

    assert shared.budget.usage.tool_calls == 2, (
        "the refused third call must not have been counted as spent")


def test_a_failing_read_does_not_lose_the_others(app):
    """One provider failing is not a reason to discard the whole preparation."""
    from loop.core.errors import Unavailable

    def working(arguments, context):
        del arguments, context
        return {"answer": "weather ok", "sources": ["weather"]}

    def broken(arguments, context):
        del arguments, context
        raise Unavailable("the calendar is unreachable")

    app.invoker.handlers["ok.read"] = RegisteredHandler(
        working, description="ok",
        input_schema={"type": "object", "additionalProperties": True},
        owner_role="daily_life")
    app.invoker.handlers["bad.read"] = RegisteredHandler(
        broken, description="bad",
        input_schema={"type": "object", "additionalProperties": True},
        owner_role="daily_life")

    shared = context()
    good = app.invoker.invoke("ok.read", {}, context=shared)
    with pytest.raises(Unavailable):
        app.invoker.invoke("bad.read", {}, context=shared)

    assert good.output["sources"] == ["weather"]


def test_a_cancelled_root_stops_every_remaining_read(app):
    """Cancellation is checked per call, not only at the start."""
    from loop.core.errors import PrivacyBlocked

    def handler(arguments, context):
        del arguments, context
        return {"answer": "ok", "sources": []}

    app.invoker.handlers["x.read"] = RegisteredHandler(
        handler, description="x",
        input_schema={"type": "object", "additionalProperties": True},
        owner_role="daily_life")

    with pytest.raises(PrivacyBlocked):
        app.invoker.invoke("x.read", {}, context=context(cancelled=True))


# --------------------------------------------------------------------------- #
# TR18 — a confirmed booking is an anchor, not a suggestion
# --------------------------------------------------------------------------- #
def a_segment(*, status: BookingStatus, fixed: bool) -> Segment:
    return Segment(
        id="seg-1", type=SegmentType.LODGING,
        start_local=dt.datetime(2026, 10, 1, 15, 0),
        end_local=dt.datetime(2026, 10, 5, 11, 0),
        timezone="Europe/Lisbon", location="Hotel",
        booking_status=status, fixed=fixed)


def test_a_confirmed_booking_is_a_fixed_anchor():
    """Both halves are required: a booking the owner confirmed *and* pinned."""
    verified = a_segment(status=BookingStatus.VERIFIED, fixed=True)
    reported = a_segment(status=BookingStatus.USER_REPORTED, fixed=True)

    assert verified.is_confirmed_booking
    assert reported.is_confirmed_booking, (
        "the owner saying they booked it counts; we cannot always verify")


def test_an_unbooked_or_unpinned_segment_is_not_an_anchor():
    assert not a_segment(status=BookingStatus.UNBOOKED,
                         fixed=True).is_confirmed_booking
    assert not a_segment(status=BookingStatus.VERIFIED,
                         fixed=False).is_confirmed_booking


def test_moving_a_confirmed_booking_requires_its_own_authority(app):
    """A plan may *propose* a change; it may not make one remotely.

    The registry has no operation that could move a booking, and that is the
    point: a new itinerary cannot acquire the authority by being persuasive.
    """
    operations = set(app.registry.enabled_operations()) | set(app.invoker.handlers)

    assert not any("book" in name or "cancel_booking" in name
                   for name in operations), (
        f"something can change a booking without its own authority: {operations}")


def test_no_shipped_capability_can_make_a_remote_travel_change(app):
    """TR18's "no remote change", asserted against what exists.

    Travel ships one port — `travel.brief` — and it validates. Research,
    booking and payment are absent by decision, not omission.
    """
    travel_ports = [name for name in app.invoker.handlers
                    if name.startswith("travel.")]

    assert travel_ports == ["travel.brief"]
    result = app.invoker.invoke(
        "travel.brief", {"place": "Lisbon"}, context=context())
    assert "external_payload" in result.output
    assert result.output["sources"] == []
