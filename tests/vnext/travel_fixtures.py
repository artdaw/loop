"""Synthetic trip fixtures. No provider, no network, no fare is real."""

from __future__ import annotations

import datetime as dt

from loop.capabilities.travel.brief import (
    Budget,
    DateWindow,
    Pace,
    PlaceRef,
    Travelers,
    TripBrief,
)
from loop.capabilities.travel.money import Basis, BudgetFit, MoneyEstimate
from loop.capabilities.travel.options import ConstraintCheck, Option
from loop.capabilities.travel.schedule import (
    Accessibility,
    BookingStatus,
    Segment,
    SegmentType,
)

BERLIN = PlaceRef("Berlin", "DE", latitude=52.52, longitude=13.405,
                  timezone="Europe/Berlin")
MILAN = PlaceRef("Milan", "IT", latitude=45.46, longitude=9.19,
                 timezone="Europe/Rome")
BOLOGNA = PlaceRef("Bologna", "IT", latitude=44.49, longitude=11.34,
                   timezone="Europe/Rome")
TOKYO = PlaceRef("Tokyo", "JP", latitude=35.68, longitude=139.69,
                 timezone="Asia/Tokyo")
UNRESOLVED = PlaceRef("Springfield")

START = dt.date(2026, 10, 5)
END = dt.date(2026, 10, 10)


def brief(**kw) -> TripBrief:
    defaults = {
        "description": "five relaxed days in northern Italy",
        "origin": BERLIN,
        "destinations": (MILAN, BOLOGNA),
        "date_window": DateWindow(start_date=START, end_date=END),
        "travelers": Travelers(adults=1),
        "budget": Budget(amount_minor=120_000, currency="EUR"),
        "interests": {"architecture": 2.0, "local_food": 1.0},
        "pace": Pace.RELAXED,
        "preferred_modes": ("rail",),
    }
    defaults.update(kw)
    return TripBrief(**defaults)


def segment(seg_id: str, *, kind: SegmentType = SegmentType.ACTIVITY,
            day: int = 5, start: str = "10:00", end: str = "12:00",
            timezone: str = "Europe/Rome", end_timezone: str = "",
            location: str = "Pinacoteca", fixed: bool = False,
            booking: BookingStatus = BookingStatus.UNBOOKED,
            accessibility: Accessibility = Accessibility.UNKNOWN,
            month: int = 10) -> Segment:
    start_h, start_m = (int(p) for p in start.split(":"))
    end_h, end_m = (int(p) for p in end.split(":"))
    start_dt = dt.datetime(2026, month, day, start_h, start_m)
    end_day = day if (end_h, end_m) >= (start_h, start_m) else day + 1
    end_dt = dt.datetime(2026, month, end_day, end_h, end_m)
    return Segment(id=seg_id, type=kind, start_local=start_dt, end_local=end_dt,
                   timezone=timezone, end_timezone=end_timezone,
                   location=location, fixed=fixed, booking_status=booking,
                   accessibility=accessibility)


def rail(seg_id: str, *, day: int = 5, start: str = "08:00", end: str = "16:00",
         location: str = "Berlin Hbf", timezone: str = "Europe/Berlin",
         end_timezone: str = "Europe/Rome") -> Segment:
    return segment(seg_id, kind=SegmentType.TRANSPORT, day=day, start=start,
                   end=end, location=location, timezone=timezone,
                   end_timezone=end_timezone)


def cost(category: str = "lodging", low: int = 40_000, high: int = 50_000,
         currency: str = "EUR", basis: Basis = Basis.PER_GROUP,
         **kw) -> MoneyEstimate:
    return MoneyEstimate(category=category, amount_min_minor=low,
                         amount_max_minor=high, currency=currency, basis=basis,
                         **kw)


def option(option_id: str = "opt-a", *, segments=None, costs=None,
           checks=None, matched=None, unresolved=None,
           budget_fit=BudgetFit.WITHIN, **kw) -> Option:
    """A fully checked option.

    `budget_fit` is explicit because `Option` defaults it to TENTATIVE, which
    honestly means "not checked yet" — a fixture standing in for a completed
    plan has to say the check happened.
    """
    return Option(id=option_id, summary=kw.pop("summary", "a plan"),
                  segments=list(segments or []), costs=costs,
                  budget_fit=budget_fit,
                  constraint_checks=list(checks or []),
                  matched_interests=dict(matched or {}),
                  unresolved=list(unresolved or []), **kw)


def passing_check(name: str = "budget") -> ConstraintCheck:
    return ConstraintCheck(name, True, "satisfied")
