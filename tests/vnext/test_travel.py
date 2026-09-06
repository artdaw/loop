"""The travel.itinerary pack — TR01 to TR24.

Every fixture is synthetic. Nothing here books, pays, messages anyone, contacts
a provider, or writes a calendar event.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from loop.capabilities.travel.brief import (
    ANCHOR_LIMITS,
    Budget,
    DateWindow,
    Pace,
    can_claim_feasibility,
    external_query_payload,
    validate_brief,
)
from loop.capabilities.travel.evidence import (
    ClaimType,
    EvidenceItem,
    EvidenceSet,
    Freshness,
    SourceKind,
    claims_supported,
    detect_conflicts,
)
from loop.capabilities.travel.money import (
    Basis,
    BudgetFit,
    ExchangeRate,
    MoneyEstimate,
    compare_to_budget,
    format_money,
    to_decimal,
    total_costs,
)
from loop.capabilities.travel.monitor import (
    ChangeKind,
    Check,
    NoticeLedger,
    QuoteBasis,
    build_checkpoints,
    compare_cost,
    compare_transport,
    plan_monitoring,
    resolve_checks,
)
from loop.capabilities.travel.options import (
    ConstraintCheck,
    Feasibility,
    Relaxation,
    ResultStatus,
    build_result,
    rank_options,
)
from loop.capabilities.travel.schedule import (
    Accessibility,
    BookingStatus,
    OpeningRule,
    Segment,
    SegmentType,
    Severity,
    buffer_minutes,
    check_accessibility,
    check_door_to_door,
    check_opening,
    check_overlaps,
    check_pace,
    check_transfers,
)
from loop.capabilities.travel.trip import (
    SaveManifest,
    TripStatus,
    TripStore,
    projection_path,
)
from loop.core.errors import Conflict, InvalidInput
from tests.vnext import travel_fixtures as fx

NOW = 1_780_000_000


def _store_with_result(status=ResultStatus.READY, **kw):
    """A trip store holding one committed revision."""
    store = TripStore()
    brief = kw.pop("brief", fx.brief())
    store.create(trip_id="t1", title="Italy", brief=brief)
    option = fx.option("opt-a", checks=[fx.passing_check()],
                       matched={"architecture": "Pinacoteca"})
    result = build_result([option], brief, trip_id="t1", revision_id="rev-1",
                          generated_at=NOW)
    store.add_revision(revision_id="rev-1", trip_id="t1", result=result,
                       created_at=NOW, expected_version=1)
    return store


# --------------------------------------------------------------------------- #
# TR01 — a complete brief yields sourced, ranked options
# --------------------------------------------------------------------------- #
def test_tr01_a_complete_brief_needs_no_questions():
    assert validate_brief(fx.brief()) == []


def test_tr01_feasibility_can_be_claimed_with_origin_and_dates():
    assert can_claim_feasibility(fx.brief()) is True


def test_tr01_a_result_recommends_one_option():
    brief = fx.brief()
    options = [fx.option("opt-a", checks=[fx.passing_check()],
                         matched={"architecture": "Duomo"}),
               fx.option("opt-b", checks=[fx.passing_check()])]
    result = build_result(options, brief, trip_id="t1", revision_id="r1",
                          generated_at=NOW)

    assert result.recommended_option_id == "opt-a"
    assert result.status is ResultStatus.READY


def test_tr01_the_recommendation_explains_itself():
    brief = fx.brief()
    result = build_result(
        [fx.option("opt-a", checks=[fx.passing_check()],
                   matched={"architecture": "Duomo"})],
        brief, trip_id="t1", revision_id="r1", generated_at=NOW)

    assert "interests" in result.options[0].why_recommended


def test_tr01_at_most_three_options_are_returned():
    brief = fx.brief()
    options = [fx.option(f"opt-{i}", checks=[fx.passing_check()])
               for i in range(6)]
    result = build_result(options, brief, trip_id="t1", revision_id="r1",
                          generated_at=NOW)

    assert len(result.options) == 3


def test_tr01_the_date_range_displays_its_year():
    assert "2026" in fx.brief().date_window.display()


# --------------------------------------------------------------------------- #
# TR02 — destination discovery with a flexible window
# --------------------------------------------------------------------------- #
def test_tr02_a_brief_without_destinations_is_discovery():
    assert fx.brief(destinations=()).is_discovery is True


def test_tr02_a_flexible_window_is_valid():
    window = DateWindow(earliest_departure=dt.date(2026, 10, 1),
                        latest_return=dt.date(2026, 10, 31), duration_days=5)
    assert window.is_flexible and window.is_known


def test_tr02_a_window_too_short_for_the_trip_is_blocking():
    brief = fx.brief(date_window=DateWindow(
        earliest_departure=dt.date(2026, 10, 1),
        latest_return=dt.date(2026, 10, 3), duration_days=5))
    missing = validate_brief(brief)

    assert missing[0].field == "date_window"
    assert missing[0].blocking is True


def test_tr02_mixing_both_date_representations_is_rejected():
    with pytest.raises(ValueError, match="exactly one"):
        DateWindow(start_date=dt.date(2026, 10, 5), end_date=dt.date(2026, 10, 10),
                   earliest_departure=dt.date(2026, 10, 1),
                   latest_return=dt.date(2026, 10, 31), duration_days=5)


def test_tr02_fewer_supported_options_are_returned_rather_than_padded():
    """Three near-identical options is not a comparison."""
    brief = fx.brief()
    result = build_result([fx.option("opt-a", checks=[fx.passing_check()])],
                          brief, trip_id="t1", revision_id="r1",
                          generated_at=NOW)
    assert len(result.options) == 1


# --------------------------------------------------------------------------- #
# TR03 — absent dates or origin
# --------------------------------------------------------------------------- #
def test_tr03_a_missing_origin_is_not_blocking():
    missing = validate_brief(fx.brief(origin=None))
    assert missing[0].field == "origin" and missing[0].blocking is False


def test_tr03_but_outbound_feasibility_cannot_be_claimed():
    assert can_claim_feasibility(fx.brief(origin=None)) is False


def test_tr03_missing_dates_prevent_a_feasibility_claim():
    assert can_claim_feasibility(fx.brief(date_window=DateWindow())) is False


def test_tr03_an_unresolved_place_blocks_transfer_computation():
    missing = validate_brief(fx.brief(origin=fx.UNRESOLVED))

    assert missing[0].blocking is True
    assert "Springfield" in missing[0].question


def test_tr03_a_fare_claim_needs_fare_evidence():
    """No dates means no fare was fetched, so no fare may be quoted."""
    supported, reason = claims_supported(ClaimType.FARE, EvidenceSet(), now=NOW)

    assert supported is False
    assert "no source was read" in reason


def test_tr03_the_brief_is_saved_before_research():
    store = TripStore()
    trip = store.create(trip_id="t1", title="somewhere warm",
                        brief=fx.brief(destinations=(), date_window=DateWindow()))

    assert store.get("t1") is trip
    assert trip.status is TripStatus.PLANNING


# --------------------------------------------------------------------------- #
# TR04 — nothing is feasible
# --------------------------------------------------------------------------- #
def test_tr04_no_feasible_plan_is_reported():
    brief = fx.brief()
    infeasible = fx.option("opt-a", checks=[
        ConstraintCheck("budget", False, "cheapest route is €1,900")])
    result = build_result([infeasible], brief, trip_id="t1", revision_id="r1",
                          generated_at=NOW)

    assert result.status is ResultStatus.NO_FEASIBLE_PLAN
    assert result.options == []


def test_tr04_the_conflicting_constraint_is_named():
    brief = fx.brief()
    result = build_result(
        [fx.option("opt-a", checks=[
            ConstraintCheck("budget", False, "cheapest route is €1,900")])],
        brief, trip_id="t1", revision_id="r1", generated_at=NOW)

    assert result.relaxations[0].constraint == "budget"


def test_tr04_concrete_relaxations_are_offered():
    brief = fx.brief()
    result = build_result(
        [fx.option("opt-a", checks=[
            ConstraintCheck("max_transfers", False, "needs three changes")])],
        brief, trip_id="t1", revision_id="r1", generated_at=NOW,
        relaxations=[Relaxation("max_transfers", "allow one more change")])

    assert result.relaxations[0].proposal == "allow one more change"


def test_tr04_a_budget_violation_is_never_silently_dropped():
    """Producing three options by ignoring the budget answers another question."""
    option = fx.option("opt-a", checks=[fx.passing_check()],
                       budget_fit=BudgetFit.OVER)
    assert option.feasibility is Feasibility.INFEASIBLE


# --------------------------------------------------------------------------- #
# TR05 — venue closures and last admission
# --------------------------------------------------------------------------- #
def _museum(**kw) -> OpeningRule:
    defaults = {"venue": "Pinacoteca",
                "open_weekdays": frozenset({1, 2, 3, 4, 5, 6}),
                "opens_at": dt.time(9, 0), "closes_at": dt.time(18, 0),
                "last_admission": dt.time(17, 30)}
    defaults.update(kw)
    return OpeningRule(**defaults)


def test_tr05_a_closed_day_is_a_violation():
    monday = fx.segment("s1", day=5, start="10:00", end="12:00")  # 5 Oct is Mon
    findings = check_opening([monday], {"Pinacoteca": _museum()})

    assert findings[0].code == "closed"
    assert "closed on that day" in findings[0].detail


def test_tr05_arriving_after_last_admission_is_a_violation():
    late = fx.segment("s1", day=6, start="17:40", end="18:00")
    findings = check_opening([late], {"Pinacoteca": _museum()})

    assert findings[0].code == "closed"
    assert "stops admitting" in findings[0].detail


def test_tr05_a_valid_visit_produces_no_finding():
    fine = fx.segment("s1", day=6, start="10:00", end="12:00")
    assert check_opening([fine], {"Pinacoteca": _museum()}) == []


def test_tr05_the_option_becomes_infeasible():
    option = fx.option("opt-a", checks=[fx.passing_check()])
    option.findings = check_opening([fx.segment("s1", day=5)],
                                    {"Pinacoteca": _museum()})
    assert option.feasibility is Feasibility.INFEASIBLE


def test_tr05_unknown_hours_are_unresolved_not_a_violation():
    findings = check_opening([fx.segment("s1", location="Unlisted Chapel")], {})

    assert findings[0].severity is Severity.UNRESOLVED
    assert findings[0].code == "unknown_hours"


# --------------------------------------------------------------------------- #
# TR06 — transfers, overnight and date-line travel
# --------------------------------------------------------------------------- #
def test_tr06_a_tight_transfer_is_detected():
    arriving = fx.rail("t1", day=5, start="08:00", end="12:00",
                       timezone="Europe/Rome", end_timezone="Europe/Rome")
    departing = fx.rail("t2", day=5, start="12:05", end="13:00",
                        timezone="Europe/Rome", end_timezone="Europe/Rome")
    findings = check_transfers([arriving, departing])

    assert findings[0].code == "tight_transfer"


def test_tr06_an_operator_minimum_overrides_the_generic_buffer():
    """A 20% rule of thumb does not survive an airport requiring 60 minutes."""
    arriving = fx.rail("t1", day=5, start="08:00", end="09:00",
                       timezone="Europe/Rome", end_timezone="Europe/Rome")
    departing = fx.rail("t2", day=5, start="09:30", end="10:00",
                        timezone="Europe/Rome", end_timezone="Europe/Rome")

    assert check_transfers([arriving, departing]) == []
    assert check_transfers([arriving, departing],
                           operator_minimums={"t2": 60})[0].code \
        == "tight_transfer"


def test_tr06_the_buffer_is_the_larger_of_the_two_rules():
    assert buffer_minutes(30) == 15          # the floor wins
    assert buffer_minutes(300) == 60         # the fraction wins


def test_tr06_an_omitted_first_leg_is_detected():
    findings = check_door_to_door(
        [fx.rail("t1", location="Berlin Brandenburg Airport")],
        origin="Berlin city centre",
        departure_point="Berlin Brandenburg Airport")

    assert findings[0].code == "omitted_leg"


def test_tr06_a_door_to_door_plan_passes():
    assert check_door_to_door([fx.rail("t1", location="Berlin city centre")],
                              origin="Berlin city centre",
                              departure_point="Berlin Brandenburg Airport") == []


def _tokyo_berlin() -> Segment:
    """22:00 Tokyo on 5 Oct, landing 04:00 Berlin on 6 Oct."""
    return fx.segment("f1", kind=SegmentType.TRANSPORT, day=5, start="22:00",
                      end="04:00", timezone="Asia/Tokyo",
                      end_timezone="Europe/Berlin")


def test_tr06_the_arrival_date_is_the_date_where_you_land():
    flight = _tokyo_berlin()

    assert flight.start_local.date() == dt.date(2026, 10, 5)
    assert flight.arrival_local_date == dt.date(2026, 10, 6)


def test_tr06_the_utc_instants_are_derived_from_the_right_zones():
    """The clock says six hours. The flight is thirteen."""
    flight = _tokyo_berlin()

    naive = (flight.end_local - flight.start_local).total_seconds() / 60
    assert naive == 360
    assert flight.duration_minutes == 780


def test_tr06_the_departure_instant_precedes_the_arrival_instant():
    flight = _tokyo_berlin()
    assert flight.start_utc < flight.end_utc


def test_tr06_overlapping_segments_are_detected():
    a = fx.segment("s1", day=6, start="10:00", end="13:00")
    b = fx.segment("s2", day=6, start="12:00", end="14:00")

    assert check_overlaps([a, b])[0].code == "overlap"


def test_tr06_a_segment_ending_before_it_starts_is_rejected():
    with pytest.raises(ValueError):
        Segment(id="s1", type=SegmentType.ACTIVITY,
                start_local=dt.datetime(2026, 10, 6, 12),
                end_local=dt.datetime(2026, 10, 6, 10),
                timezone="Europe/Rome")


# --------------------------------------------------------------------------- #
# TR07 — pace and accessibility
# --------------------------------------------------------------------------- #
def test_tr07_relaxed_pace_allows_two_anchors_a_day():
    assert ANCHOR_LIMITS[Pace.RELAXED] == 2


def test_tr07_exceeding_the_anchor_limit_is_a_finding():
    segments = [fx.segment(f"s{i}", day=6, start=f"{9 + i * 2}:00",
                           end=f"{10 + i * 2}:00") for i in range(3)]
    findings = check_pace(segments, anchor_limit=2)

    assert findings[0].code == "pace"


def test_tr07_meals_and_transit_do_not_count_as_anchors():
    segments = [fx.segment("a1", day=6, start="09:00", end="11:00"),
                fx.segment("a2", day=6, start="14:00", end="16:00"),
                fx.segment("m1", kind=SegmentType.MEAL, day=6, start="12:00",
                           end="13:00"),
                fx.rail("t1", day=6, start="17:00", end="18:00")]

    assert check_pace(segments, anchor_limit=2) == []


def test_tr07_an_inaccessible_venue_is_a_violation():
    segment = fx.segment("s1", accessibility=Accessibility.NOT_ACCESSIBLE)
    findings = check_accessibility([segment], required=True)

    assert findings[0].code == "not_accessible"
    assert findings[0].is_violation


def test_tr07_unknown_accessibility_is_unresolved_not_verified():
    """No accessibility note is not a note saying it is accessible."""
    findings = check_accessibility([fx.segment("s1")], required=True)

    assert findings[0].severity is Severity.UNRESOLVED
    assert findings[0].code == "accessibility_unknown"


def test_tr07_an_unresolved_accessibility_makes_the_option_tentative():
    option = fx.option("opt-a", checks=[fx.passing_check()])
    option.findings = check_accessibility([fx.segment("s1")], required=True)

    assert option.feasibility is Feasibility.TENTATIVE


def test_tr07_accessibility_is_not_checked_when_not_required():
    assert check_accessibility([fx.segment("s1")], required=False) == []


# --------------------------------------------------------------------------- #
# TR08 — traveller counts, sharing and fees
# --------------------------------------------------------------------------- #
def test_tr08_a_per_person_cost_multiplies():
    estimate = fx.cost("rail", 9000, 9000, basis=Basis.PER_PERSON)
    assert estimate.total_minor(travelers=3) == (27_000, 27_000)


def test_tr08_a_shared_room_is_counted_once():
    estimate = fx.cost("lodging", 40_000, 50_000, basis=Basis.PER_ROOM)
    assert estimate.total_minor(travelers=3) == (40_000, 50_000)


def test_tr08_quantity_multiplies_a_shared_cost():
    estimate = fx.cost("lodging", 40_000, 50_000, basis=Basis.PER_ROOM,
                       quantity=2)
    assert estimate.total_minor(travelers=4) == (80_000, 100_000)


def test_tr08_totals_use_integer_minor_units():
    total = total_costs([fx.cost("a", 1, 1), fx.cost("b", 2, 2)], travelers=1)
    assert total.by_currency["EUR"] == (3, 3)


def test_tr08_decimal_conversion_is_exact():
    """0.1 + 0.2 != 0.3 in binary floating point; budgets cannot use it."""
    assert to_decimal(10, "EUR") + to_decimal(20, "EUR") == Decimal("0.30")


def test_tr08_a_currency_without_minor_units_formats_correctly():
    assert format_money(1200, "JPY") == "1200 JPY"


def test_tr08_an_unknown_mandatory_fee_makes_compliance_tentative():
    estimates = [fx.cost("lodging", 40_000, 50_000,
                         unknown_mandatory=("city tax",))]
    total = total_costs(estimates, travelers=1)
    fit, reason = compare_to_budget(total, budget_minor=120_000, currency="EUR")

    assert fit is BudgetFit.TENTATIVE
    assert "city tax" in reason


def test_tr08_a_clear_total_under_budget_is_within():
    total = total_costs([fx.cost("lodging", 40_000, 50_000)], travelers=1)
    fit, _ = compare_to_budget(total, budget_minor=120_000, currency="EUR")

    assert fit is BudgetFit.WITHIN


def test_tr08_a_range_straddling_the_budget_is_tentative():
    total = total_costs([fx.cost("lodging", 100_000, 140_000)], travelers=1)
    fit, _ = compare_to_budget(total, budget_minor=120_000, currency="EUR")

    assert fit is BudgetFit.TENTATIVE


def test_tr08_a_budget_per_person_scales():
    budget = Budget(amount_minor=60_000, currency="EUR", basis="per_person")
    assert budget.total_minor(travelers=2) == 120_000


def test_tr08_a_negative_range_is_rejected():
    with pytest.raises(ValueError):
        MoneyEstimate("x", 500, 100, "EUR")


# --------------------------------------------------------------------------- #
# TR09 — mixed currencies
# --------------------------------------------------------------------------- #
def test_tr09_mixed_currencies_without_a_rate_are_uncomparable():
    total = total_costs([fx.cost("lodging", 40_000, 50_000, currency="EUR"),
                         fx.cost("rail", 8_000, 8_000, currency="CHF")],
                        travelers=1)
    fit, reason = compare_to_budget(total, budget_minor=120_000, currency="EUR")

    assert fit is BudgetFit.UNCOMPARABLE
    assert "separately" in reason


def test_tr09_the_separate_totals_are_still_shown():
    total = total_costs([fx.cost("lodging", 40_000, 50_000, currency="EUR"),
                         fx.cost("rail", 8_000, 8_000, currency="CHF")],
                        travelers=1)

    assert total.currencies == ["CHF", "EUR"]
    assert len(total.describe()) == 2


def test_tr09_no_conversion_is_invented():
    total = total_costs([fx.cost("rail", 8_000, 8_000, currency="CHF")],
                        travelers=1)
    fit, _ = compare_to_budget(total, budget_minor=120_000, currency="EUR")

    assert fit.is_verdict is False


def test_tr09_a_dated_sourced_rate_permits_a_verdict():
    rate = ExchangeRate("CHF", "EUR", Decimal("1.05"), as_of="2026-10-01",
                        source="ECB")
    total = total_costs([fx.cost("lodging", 40_000, 50_000, currency="EUR"),
                         fx.cost("rail", 8_000, 8_000, currency="CHF")],
                        travelers=1)
    fit, _ = compare_to_budget(total, budget_minor=120_000, currency="EUR",
                               rate=rate)

    assert fit is BudgetFit.WITHIN


def test_tr09_conversion_uses_decimal_arithmetic():
    rate = ExchangeRate("CHF", "EUR", Decimal("1.05"), as_of="2026-10-01",
                        source="ECB")
    assert rate.convert_minor(8_000) == 8_400


# --------------------------------------------------------------------------- #
# TR10 — stale evidence
# --------------------------------------------------------------------------- #
def _item(claim: ClaimType, *, age: int, kind=SourceKind.OFFICIAL_PAGE,
          item_id: str = "e1", read: bool = True) -> EvidenceItem:
    return EvidenceItem(id=item_id, url="https://example.invalid/x", kind=kind,
                        claim_type=claim, fetched_at=NOW - age, read=read)


def test_tr10_a_fare_expires_in_fifteen_minutes():
    assert _item(ClaimType.FARE, age=1000).freshness(now=NOW) is Freshness.STALE
    assert _item(ClaimType.FARE, age=600).freshness(now=NOW) is Freshness.FRESH


def test_tr10_a_schedule_lasts_a_day():
    assert _item(ClaimType.SCHEDULE, age=3600).freshness(now=NOW) \
        is Freshness.FRESH


def test_tr10_venue_hours_last_a_week():
    assert _item(ClaimType.VENUE_HOURS, age=200_000).freshness(now=NOW) \
        is Freshness.FRESH


def test_tr10_a_provider_expiry_overrides_the_ceiling():
    item = _item(ClaimType.SCHEDULE, age=0)
    item.expires_at = NOW - 1
    assert item.freshness(now=NOW) is Freshness.STALE


def test_tr10_unknown_fetch_time_is_unknown_not_fresh():
    item = _item(ClaimType.FARE, age=0)
    item.fetched_at = 0
    assert item.freshness(now=NOW) is Freshness.UNKNOWN


def test_tr10_a_stale_fare_supports_no_availability_claim():
    evidence = EvidenceSet([_item(ClaimType.FARE, age=1000)])
    supported, reason = claims_supported(ClaimType.FARE, evidence, now=NOW)

    assert supported is False
    assert "expired" in reason and "recheck" in reason


def test_tr10_a_fresh_fare_does_support_one():
    evidence = EvidenceSet([_item(ClaimType.FARE, age=100)])
    assert claims_supported(ClaimType.FARE, evidence, now=NOW)[0] is True


# --------------------------------------------------------------------------- #
# TR11 — snippets, conflicts and missing adapters
# --------------------------------------------------------------------------- #
def test_tr11_a_search_snippet_is_not_evidence():
    assert SourceKind.SEARCH_SNIPPET.is_readable_evidence is False


def test_tr11_a_snippet_alone_supports_no_claim():
    evidence = EvidenceSet([_item(ClaimType.SCHEDULE, age=10,
                                  kind=SourceKind.SEARCH_SNIPPET)])
    supported, reason = claims_supported(ClaimType.SCHEDULE, evidence, now=NOW)

    assert supported is False
    assert "not read" in reason


def test_tr11_an_official_page_read_directly_does_support_it():
    evidence = EvidenceSet([_item(ClaimType.SCHEDULE, age=10)])
    assert claims_supported(ClaimType.SCHEDULE, evidence, now=NOW)[0] is True


def test_tr11_an_unread_page_supports_nothing():
    evidence = EvidenceSet([_item(ClaimType.SCHEDULE, age=10, read=False)])
    assert claims_supported(ClaimType.SCHEDULE, evidence, now=NOW)[0] is False


def test_tr11_conflicting_authoritative_pages_stay_unresolved():
    items = [_item(ClaimType.SCHEDULE, age=10, item_id="a"),
             _item(ClaimType.SCHEDULE, age=10, item_id="b")]
    conflicts = detect_conflicts(items, values={"a": "08:15", "b": "08:45"})

    assert conflicts[0].claim_type is ClaimType.SCHEDULE
    assert conflicts[0].source_ids == ("a", "b")


def test_tr11_agreeing_sources_produce_no_conflict():
    items = [_item(ClaimType.SCHEDULE, age=10, item_id="a"),
             _item(ClaimType.SCHEDULE, age=10, item_id="b")]
    assert detect_conflicts(items, values={"a": "08:15", "b": "08:15"}) == []


def test_tr11_editorial_sources_do_not_create_authoritative_conflicts():
    items = [_item(ClaimType.VENUE_HOURS, age=10, item_id="a"),
             _item(ClaimType.VENUE_HOURS, age=10, item_id="b",
                   kind=SourceKind.EDITORIAL)]
    assert detect_conflicts(items, values={"a": "09:00", "b": "10:00"}) == []


# --------------------------------------------------------------------------- #
# TR12 — transparent ranking
# --------------------------------------------------------------------------- #
def test_tr12_feasible_options_outrank_tentative_ones():
    brief = fx.brief()
    tentative = fx.option("opt-a", checks=[ConstraintCheck("x", None, "unknown")])
    feasible = fx.option("opt-b", checks=[fx.passing_check()])

    assert [o.id for o in rank_options([tentative, feasible], brief)] \
        == ["opt-b", "opt-a"]


def test_tr12_interest_coverage_ranks_first_by_default():
    brief = fx.brief()
    low = fx.option("opt-a", checks=[fx.passing_check()],
                    matched={"local_food": "trattoria"})
    high = fx.option("opt-b", checks=[fx.passing_check()],
                     matched={"architecture": "Duomo"})

    assert rank_options([low, high], brief)[0].id == "opt-b"


def test_tr12_stated_priorities_override_the_default_order():
    brief = fx.brief(priorities=("cost",))
    dear = fx.option("opt-a", checks=[fx.passing_check()],
                     matched={"architecture": "Duomo"},
                     costs=total_costs([fx.cost("x", 90_000, 90_000)],
                                       travelers=1))
    cheap = fx.option("opt-b", checks=[fx.passing_check()],
                      costs=total_costs([fx.cost("x", 30_000, 30_000)],
                                        travelers=1))

    assert rank_options([dear, cheap], brief)[0].id == "opt-b"


def test_tr12_missing_interests_skip_that_criterion():
    brief = fx.brief(interests={})
    assert fx.option("opt-a").interest_coverage(brief) is None


def test_tr12_ranking_is_deterministic():
    brief = fx.brief()
    options = [fx.option(f"opt-{i}", checks=[fx.passing_check()])
               for i in "abcd"]

    assert rank_options(list(options), brief) == rank_options(list(options), brief)


def test_tr12_ties_break_on_the_stable_option_id():
    brief = fx.brief(interests={})
    options = [fx.option("opt-z", checks=[fx.passing_check()]),
               fx.option("opt-a", checks=[fx.passing_check()])]

    assert [o.id for o in rank_options(options, brief)] == ["opt-a", "opt-z"]


def test_tr12_coverage_is_matched_weight_over_requested_weight():
    brief = fx.brief()          # architecture 2.0, local_food 1.0
    option = fx.option("opt-a", matched={"architecture": "Duomo"})

    assert option.interest_coverage(brief) == pytest.approx(2 / 3)


# --------------------------------------------------------------------------- #
# TR13 — revisions and concurrent edits
# --------------------------------------------------------------------------- #
def test_tr13_a_revision_is_committed_against_its_version():
    store = _store_with_result()
    assert store.get("t1").current_revision_id == "rev-1"


def test_tr13_a_stale_expected_version_is_a_conflict():
    store = _store_with_result()
    with pytest.raises(Conflict):
        store.revise_brief("t1", expected_version=1,
                           changes={"pace": Pace.RELAXED})


def test_tr13_the_losing_writer_is_told_which_version_won():
    store = _store_with_result()
    with pytest.raises(Conflict) as excinfo:
        store.revise_brief("t1", expected_version=1, changes={})

    assert excinfo.value.details["current_version"] == 2


def test_tr13_revising_the_brief_bumps_its_version():
    store = _store_with_result()
    trip = store.revise_brief("t1", expected_version=2,
                              changes={"pace": Pace.RELAXED})
    assert trip.brief.version == 2


def test_tr13_a_new_brief_version_marks_old_proposals_stale():
    store = _store_with_result()
    store.revise_brief("t1", expected_version=2, changes={"pace": Pace.RELAXED})

    assert store.revision("rev-1").stale is True


def test_tr13_a_result_planned_from_an_older_brief_is_rejected():
    store = _store_with_result()
    brief = store.get("t1").brief
    old_result = build_result([fx.option("opt-x", checks=[fx.passing_check()])],
                              brief, trip_id="t1", revision_id="rev-2",
                              generated_at=NOW)
    store.revise_brief("t1", expected_version=2, changes={"pace": Pace.BUSY})

    with pytest.raises(Conflict, match="earlier version"):
        store.add_revision(revision_id="rev-2", trip_id="t1", result=old_result,
                           created_at=NOW, expected_version=3)


def test_tr13_the_selected_plan_stays_selected_after_a_new_proposal():
    """A background recheck must not move the plan the user is travelling on."""
    store = _store_with_result()
    store.select("t1", expected_version=2, revision_id="rev-1",
                 option_id="opt-a")
    trip = store.get("t1")
    result = build_result([fx.option("opt-b", checks=[fx.passing_check()])],
                          trip.brief, trip_id="t1", revision_id="rev-2",
                          generated_at=NOW)
    store.add_revision(revision_id="rev-2", trip_id="t1", result=result,
                       created_at=NOW, expected_version=3)

    assert store.get("t1").selected_revision_id == "rev-1"


def test_tr13_but_it_is_flagged_for_review():
    store = _store_with_result()
    store.select("t1", expected_version=2, revision_id="rev-1",
                 option_id="opt-a")
    trip = store.get("t1")
    result = build_result([fx.option("opt-b", checks=[fx.passing_check()])],
                          trip.brief, trip_id="t1", revision_id="rev-2",
                          generated_at=NOW)
    store.add_revision(revision_id="rev-2", trip_id="t1", result=result,
                       created_at=NOW, expected_version=3)

    assert store.get("t1").needs_review is True


def test_tr13_earlier_revisions_remain_available():
    store = _store_with_result()
    trip = store.get("t1")
    result = build_result([fx.option("opt-b", checks=[fx.passing_check()])],
                          trip.brief, trip_id="t1", revision_id="rev-2",
                          generated_at=NOW)
    store.add_revision(revision_id="rev-2", trip_id="t1", result=result,
                       created_at=NOW, expected_version=2)

    assert len(store.revisions_for("t1")) == 2
    assert store.revision("rev-1") is not None


def test_tr13_selecting_an_option_from_another_revision_is_rejected():
    store = _store_with_result()
    with pytest.raises(InvalidInput):
        store.select("t1", expected_version=2, revision_id="rev-1",
                     option_id="opt-nonexistent")


# --------------------------------------------------------------------------- #
# TR14 — planning books nothing
# --------------------------------------------------------------------------- #
def test_tr14_a_ready_trip_is_not_booked():
    store = _store_with_result()
    assert store.get("t1").status is TripStatus.READY
    assert TripStatus.READY.means_booked is False


def test_tr14_selecting_does_not_start_monitoring():
    store = _store_with_result()
    store.select("t1", expected_version=2, revision_id="rev-1",
                 option_id="opt-a")

    assert store.get("t1").is_monitored is False


def test_tr14_segments_default_to_unbooked():
    assert fx.segment("s1").booking_status is BookingStatus.UNBOOKED


def test_tr14_selecting_does_not_change_booking_status():
    store = _store_with_result()
    store.select("t1", expected_version=2, revision_id="rev-1",
                 option_id="opt-a")
    _, option = store.selected("t1")

    assert all(s.booking_status is BookingStatus.UNBOOKED
               for s in option.segments)


def test_tr14_the_save_manifest_says_nothing_is_booked():
    manifest = SaveManifest(path="p", trip_id="t1", revision_id="r1",
                            option_id="o1", generated_at=NOW)
    assert "No booking has been made" in manifest.unbooked_note


# --------------------------------------------------------------------------- #
# TR15 — explicit monitoring and restart
# --------------------------------------------------------------------------- #
DEPARTURE = dt.datetime(2026, 10, 5, 8, 0)
TRAVEL_DAYS = [dt.date(2026, 10, 5), dt.date(2026, 10, 6)]


def _now(days_before: int) -> dt.datetime:
    return (DEPARTURE.replace(tzinfo=dt.UTC) - dt.timedelta(days=days_before))


def test_tr15_monitoring_needs_a_selected_option():
    with pytest.raises(InvalidInput):
        plan_monitoring(trip_id="t1", revision_id="rev-1", option_id="",
                        requested_checks=None, departure_local=DEPARTURE,
                        travel_days=TRAVEL_DAYS, timezone="Europe/Rome",
                        now_utc=_now(30))


def test_tr15_checkpoints_are_created_before_departure():
    plan = plan_monitoring(trip_id="t1", revision_id="rev-1", option_id="opt-a",
                           requested_checks=None, departure_local=DEPARTURE,
                           travel_days=TRAVEL_DAYS, timezone="Europe/Rome",
                           now_utc=_now(30))

    reasons = [c.reason for c in plan.checkpoints]
    assert "7 days before departure" in reasons
    assert "1 days before departure" in reasons


def test_tr15_already_past_checkpoints_are_skipped():
    """Monitoring a trip three days out must not fire the 7-day check now."""
    plan = plan_monitoring(trip_id="t1", revision_id="rev-1", option_id="opt-a",
                           requested_checks=None, departure_local=DEPARTURE,
                           travel_days=TRAVEL_DAYS, timezone="Europe/Rome",
                           now_utc=_now(3))

    assert "7 days before departure" not in [c.reason for c in plan.checkpoints]


def test_tr15_coincident_checkpoints_are_deduplicated():
    checkpoints, _ = build_checkpoints(
        departure_local=dt.datetime(2026, 10, 5, 7, 0),
        travel_days=[dt.date(2026, 10, 4)], timezone="Europe/Rome",
        now_utc=_now(30))
    moments = [c.at_utc for c in checkpoints]

    assert len(moments) == len(set(moments))


def test_tr15_checkpoints_are_ordered_and_durable():
    plan = plan_monitoring(trip_id="t1", revision_id="rev-1", option_id="opt-a",
                           requested_checks=None, departure_local=DEPARTURE,
                           travel_days=TRAVEL_DAYS, timezone="Europe/Rome",
                           now_utc=_now(30))
    moments = [c.at_utc for c in plan.checkpoints]

    assert moments == sorted(moments)


def test_tr15_a_transit_day_timezone_assumption_is_disclosed():
    plan = plan_monitoring(trip_id="t1", revision_id="rev-1", option_id="opt-a",
                           requested_checks=None, departure_local=DEPARTURE,
                           travel_days=TRAVEL_DAYS, timezone="Europe/Rome",
                           now_utc=_now(30), owner_timezone="Europe/Berlin")

    assert any("timezone" in a for a in plan.assumptions)


def test_tr15_rebuilding_after_a_restart_gives_the_same_checkpoints():
    kwargs = {"departure_local": DEPARTURE, "travel_days": TRAVEL_DAYS,
              "timezone": "Europe/Rome", "now_utc": _now(30)}
    assert build_checkpoints(**kwargs)[0] == build_checkpoints(**kwargs)[0]


# --------------------------------------------------------------------------- #
# TR16 — repeated events and no-change checks
# --------------------------------------------------------------------------- #
def test_tr16_a_no_change_check_reports_none():
    assert compare_transport(480, 490).kind is ChangeKind.NONE


def test_tr16_a_thirty_minute_shift_is_material():
    report = compare_transport(480, 515)

    assert report.kind is ChangeKind.TRANSPORT_TIME
    assert report.before == "480" and report.after == "515"


def test_tr16_a_repeated_event_notifies_once():
    ledger = NoticeLedger()
    report = compare_transport(480, 515)

    assert ledger.should_notify(trip_id="t1", subject="leg-1", report=report)
    ledger.record(trip_id="t1", subject="leg-1", report=report)
    assert ledger.should_notify(trip_id="t1", subject="leg-1",
                                report=report) is False


def test_tr16_a_further_change_notifies_again():
    ledger = NoticeLedger()
    ledger.record(trip_id="t1", subject="leg-1", report=compare_transport(480, 515))

    assert ledger.should_notify(trip_id="t1", subject="leg-1",
                                report=compare_transport(480, 600))


def test_tr16_a_no_change_report_never_notifies():
    ledger = NoticeLedger()
    assert ledger.should_notify(trip_id="t1", subject="leg-1",
                                report=compare_transport(480, 485)) is False


def test_tr16_provider_events_have_a_six_hour_cooldown():
    ledger = NoticeLedger()
    ledger.record_event(trip_id="t1", subject="leg-1", now=NOW)

    assert ledger.event_allowed(trip_id="t1", subject="leg-1",
                                now=NOW + 3600) is False
    assert ledger.event_allowed(trip_id="t1", subject="leg-1",
                                now=NOW + 21_600) is True


def test_tr16_an_explicit_recheck_bypasses_the_cooldown():
    ledger = NoticeLedger()
    ledger.record_event(trip_id="t1", subject="leg-1", now=NOW)

    assert ledger.event_allowed(trip_id="t1", subject="leg-1", now=NOW + 60,
                                explicit=True) is True


# --------------------------------------------------------------------------- #
# TR17 — non-comparable quotes
# --------------------------------------------------------------------------- #
ONE_PERSON = QuoteBasis("2026-10-05", "2026-10-10", 1, ("breakfast",))
TWO_PEOPLE = QuoteBasis("2026-10-05", "2026-10-10", 2, ("breakfast",))
NO_BREAKFAST = QuoteBasis("2026-10-05", "2026-10-10", 1, ())


def test_tr17_a_different_party_size_is_not_a_price_change():
    report = compare_cost(12_000, 20_000, before_basis=ONE_PERSON,
                          after_basis=TWO_PEOPLE)

    assert report.kind is ChangeKind.NOT_COMPARABLE
    assert report.notifies is False


def test_tr17_different_inclusions_are_not_comparable():
    report = compare_cost(12_000, 15_000, before_basis=ONE_PERSON,
                          after_basis=NO_BREAKFAST)
    assert report.kind is ChangeKind.NOT_COMPARABLE


def test_tr17_different_dates_are_not_comparable():
    other_dates = QuoteBasis("2026-11-05", "2026-11-10", 1, ("breakfast",))
    report = compare_cost(12_000, 15_000, before_basis=ONE_PERSON,
                          after_basis=other_dates)
    assert report.kind is ChangeKind.NOT_COMPARABLE


def test_tr17_a_comparable_twenty_percent_rise_does_notify():
    report = compare_cost(10_000, 12_500, before_basis=ONE_PERSON,
                          after_basis=ONE_PERSON)

    assert report.kind is ChangeKind.COST
    assert report.notifies is True


def test_tr17_a_small_comparable_change_does_not_notify():
    report = compare_cost(10_000, 10_500, before_basis=ONE_PERSON,
                          after_basis=ONE_PERSON)
    assert report.kind is ChangeKind.NONE


def test_tr17_exceeding_the_budget_notifies_even_below_the_fraction():
    report = compare_cost(10_000, 10_500, before_basis=ONE_PERSON,
                          after_basis=ONE_PERSON, budget_minor=10_200)

    assert report.kind is ChangeKind.COST
    assert "exceeds the budget" in report.detail


# --------------------------------------------------------------------------- #
# TR18 — a confirmed booking is an anchor
# --------------------------------------------------------------------------- #
def test_tr18_a_confirmed_booking_is_recognised():
    segment = fx.segment("s1", fixed=True, booking=BookingStatus.VERIFIED)
    assert segment.is_confirmed_booking is True


def test_tr18_an_unbooked_fixed_segment_is_not_a_booking():
    assert fx.segment("s1", fixed=True).is_confirmed_booking is False


def test_tr18_a_revision_lists_confirmed_bookings_as_anchors():
    option = fx.option("opt-a", segments=[
        fx.segment("s1", fixed=True, booking=BookingStatus.VERIFIED),
        fx.segment("s2")])

    assert [s.id for s in option.confirmed_bookings()] == ["s1"]


def test_tr18_booking_status_is_verified_only_from_a_source():
    """Nothing in this pack can set `verified`; it comes from an authorised read."""
    assert fx.segment("s1").booking_status is BookingStatus.UNBOOKED


def test_tr18_a_new_proposal_does_not_alter_the_selected_plan():
    store = _store_with_result()
    store.select("t1", expected_version=2, revision_id="rev-1",
                 option_id="opt-a")
    before, _ = store.selected("t1")
    trip = store.get("t1")
    result = build_result([fx.option("opt-b", checks=[fx.passing_check()])],
                          trip.brief, trip_id="t1", revision_id="rev-2",
                          generated_at=NOW)
    store.add_revision(revision_id="rev-2", trip_id="t1", result=result,
                       created_at=NOW, expected_version=3)
    after, _ = store.selected("t1")

    assert before.id == after.id == "rev-1"


# --------------------------------------------------------------------------- #
# TR19 — stopping, cancelling and reselecting
# --------------------------------------------------------------------------- #
def test_tr19_cancelling_clears_monitoring():
    store = _store_with_result()
    store.get("t1").monitoring_routine_id = "routine-1"
    trip = store.cancel("t1", expected_version=2)

    assert trip.status is TripStatus.CANCELLED
    assert trip.is_monitored is False


def test_tr19_cancelling_drops_pending_notices():
    ledger = NoticeLedger()
    ledger.record(trip_id="t1", subject="leg-1", report=compare_transport(480, 515))
    ledger.record(trip_id="t2", subject="leg-1", report=compare_transport(480, 515))

    assert ledger.clear_trip("t1") == 1
    assert ledger.should_notify(trip_id="t2", subject="leg-1",
                                report=compare_transport(480, 515)) is False


def test_tr19_cancelling_needs_the_current_version():
    store = _store_with_result()
    with pytest.raises(Conflict):
        store.cancel("t1", expected_version=1)


def test_tr19_selecting_a_new_revision_replaces_the_selection():
    store = _store_with_result()
    store.select("t1", expected_version=2, revision_id="rev-1",
                 option_id="opt-a")
    trip = store.get("t1")
    result = build_result([fx.option("opt-b", checks=[fx.passing_check()])],
                          trip.brief, trip_id="t1", revision_id="rev-2",
                          generated_at=NOW)
    store.add_revision(revision_id="rev-2", trip_id="t1", result=result,
                       created_at=NOW, expected_version=3)
    store.select("t1", expected_version=4, revision_id="rev-2",
                 option_id="opt-b")

    assert store.get("t1").selected_option_id == "opt-b"
    assert store.get("t1").needs_review is False


def test_tr19_external_bookings_are_untouched_by_cancellation():
    """Cancelling the local trip is not cancelling a reservation."""
    store = _store_with_result()
    option = store.revision("rev-1").option("opt-a")
    option.segments.append(fx.segment("s1", fixed=True,
                                      booking=BookingStatus.VERIFIED))
    store.cancel("t1", expected_version=2)

    assert option.segments[-1].booking_status is BookingStatus.VERIFIED


# --------------------------------------------------------------------------- #
# TR20 — saving into a project
# --------------------------------------------------------------------------- #
def test_tr20_a_free_path_is_used_directly():
    assert projection_path("italy", existing=set()) \
        == "2-projects/italy/Process/Itinerary.md"


def test_tr20_a_human_file_is_never_overwritten():
    existing = {"2-projects/italy/Process/Itinerary.md"}
    path = projection_path("italy", existing=existing)

    assert path not in existing
    assert path == "2-projects/italy/Process/Itinerary-loop-2.md"


def test_tr20_successive_collisions_keep_suffixing():
    existing = {"2-projects/italy/Process/Itinerary.md",
                "2-projects/italy/Process/Itinerary-loop-2.md"}
    assert projection_path("italy", existing=existing) \
        == "2-projects/italy/Process/Itinerary-loop-3.md"


def test_tr20_the_saved_file_carries_its_provenance():
    manifest = SaveManifest(path="p", trip_id="t1", revision_id="rev-1",
                            option_id="opt-a", generated_at=NOW,
                            assumptions=("pace assumed relaxed",))
    front = manifest.frontmatter()

    assert front["loop_trip_id"] == "t1"
    assert front["revision"] == "rev-1"
    assert front["assumptions"] == ["pace assumed relaxed"]


def test_tr20_creating_a_project_is_explicit():
    manifest = SaveManifest(path="p", trip_id="t1", revision_id="r", option_id="o",
                            generated_at=NOW)
    assert manifest.creates_project is False


# --------------------------------------------------------------------------- #
# TR21 — private context stays local
# --------------------------------------------------------------------------- #
def test_tr21_the_brief_is_local_only_by_default():
    assert fx.brief().privacy.is_local_only is True


def test_tr21_an_external_query_carries_only_the_needed_fields():
    payload = external_query_payload(fx.brief())

    assert set(payload) == {"destinations", "travelers", "modes", "origin",
                            "start_date", "end_date"}


def test_tr21_context_refs_never_leave():
    brief = fx.brief(context_refs=("booking:ABC123", "calendar:oncology"))
    payload = external_query_payload(brief)

    assert "ABC123" not in str(payload)
    assert "oncology" not in str(payload)


def test_tr21_the_free_text_description_is_not_forwarded():
    brief = fx.brief(description="anniversary trip, don't tell Anna")
    assert "Anna" not in str(external_query_payload(brief))


def test_tr21_only_place_labels_are_sent_not_coordinates_of_home():
    payload = external_query_payload(fx.brief())
    assert payload["origin"] == "Berlin"


# --------------------------------------------------------------------------- #
# TR22 — a selection is not a habit
# --------------------------------------------------------------------------- #
def test_tr22_selecting_an_option_records_no_preference():
    """One itinerary choice is not "I always prefer this"."""
    from loop.services.learning import PreferenceStore

    store = _store_with_result()
    preferences = PreferenceStore()
    store.select("t1", expected_version=2, revision_id="rev-1",
                 option_id="opt-a")

    assert preferences.all() == []


def test_tr22_an_explicit_correction_becomes_a_scoped_preference():
    from loop.services.learning import PreferenceStore

    preferences = PreferenceStore()
    preferences.record_explicit(preference_id="p1", key="hotel_changes",
                                value="fewer", scope="travel", now=NOW)

    assert preferences.active("hotel_changes", scope="travel").value == "fewer"


def test_tr22_a_travel_preference_does_not_apply_to_another_scope():
    from loop.services.learning import PreferenceStore

    preferences = PreferenceStore()
    preferences.record_explicit(preference_id="p1", key="hotel_changes",
                                value="fewer", scope="travel:work", now=NOW)

    assert preferences.active("hotel_changes", scope="travel:family") is None


def test_tr22_the_brief_pace_is_per_trip_not_global():
    work = fx.brief(pace=Pace.BUSY)
    family = fx.brief(pace=Pace.RELAXED)

    assert work.pace is Pace.BUSY and family.pace is Pace.RELAXED


# --------------------------------------------------------------------------- #
# TR23 — unsupported checks and partial state
# --------------------------------------------------------------------------- #
def test_tr23_an_unsupported_check_is_named():
    checks, unsupported = resolve_checks(["schedule", "strike_risk"])

    assert checks == (Check.SCHEDULE,)
    assert unsupported == ("strike_risk",)


def test_tr23_it_is_never_silently_dropped():
    plan = plan_monitoring(trip_id="t1", revision_id="rev-1", option_id="opt-a",
                           requested_checks=["schedule", "strike_risk"],
                           departure_local=DEPARTURE, travel_days=TRAVEL_DAYS,
                           timezone="Europe/Rome", now_utc=_now(30))

    assert plan.unsupported == ("strike_risk",)
    assert plan.is_activatable is False


def test_tr23_omitted_checks_default_to_the_supported_set():
    checks, unsupported = resolve_checks(None)

    assert set(checks) == set(Check)
    assert unsupported == ()


def test_tr23_a_tentative_option_makes_the_result_partial():
    brief = fx.brief()
    result = build_result(
        [fx.option("opt-a", checks=[ConstraintCheck("hours", None, "unknown")])],
        brief, trip_id="t1", revision_id="r1", generated_at=NOW)

    assert result.status is ResultStatus.PARTIAL
    assert result.status.claims_a_plan is False


def test_tr23_missing_information_yields_needs_input():
    brief = fx.brief()
    result = build_result([fx.option("opt-a", checks=[fx.passing_check()])],
                          brief, trip_id="t1", revision_id="r1",
                          generated_at=NOW, missing=["which Springfield?"])

    assert result.status is ResultStatus.NEEDS_INPUT
    assert result.recommended_option_id == ""


def test_tr23_a_needs_input_result_recommends_nothing():
    """The outline stays useful; what it must not do is recommend one (TR03)."""
    brief = fx.brief()
    result = build_result([fx.option("opt-a", checks=[fx.passing_check()])],
                          brief, trip_id="t1", revision_id="r1",
                          generated_at=NOW, missing=["dates?"])

    assert result.recommended_option_id == ""
    assert result.missing_information == ["dates?"]


# --------------------------------------------------------------------------- #
# TR24 — one logical effect across surfaces
# --------------------------------------------------------------------------- #
def test_tr24_every_surface_uses_the_same_store():
    store = _store_with_result()
    trip = store.get("t1")

    assert trip.version == 2
    assert store.revision(trip.current_revision_id).result.status \
        is ResultStatus.READY


def test_tr24_a_replayed_select_with_a_stale_version_is_a_conflict():
    """The second delivery of the same request must not apply twice."""
    store = _store_with_result()
    store.select("t1", expected_version=2, revision_id="rev-1",
                 option_id="opt-a")

    with pytest.raises(Conflict):
        store.select("t1", expected_version=2, revision_id="rev-1",
                     option_id="opt-a")


def test_tr24_a_blind_write_is_impossible():
    store = _store_with_result()
    with pytest.raises(TypeError):
        store.select("t1", revision_id="rev-1", option_id="opt-a")  # type: ignore[call-arg]


def test_tr24_an_unknown_trip_is_rejected_the_same_way():
    store = TripStore()
    with pytest.raises(InvalidInput):
        store.cancel("nope", expected_version=1)


def test_tr24_the_same_brief_produces_the_same_ranking_everywhere():
    brief = fx.brief()
    options = [fx.option("opt-a", checks=[fx.passing_check()],
                         matched={"architecture": "Duomo"}),
               fx.option("opt-b", checks=[fx.passing_check()])]

    first = build_result(list(options), brief, trip_id="t1", revision_id="r1",
                         generated_at=NOW)
    second = build_result(list(options), brief, trip_id="t1", revision_id="r1",
                          generated_at=NOW)

    assert [o.id for o in first.options] == [o.id for o in second.options]
