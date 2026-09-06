"""Monitoring: bounded checkpoints and comparable change detection (§7).

Monitoring is off unless explicitly activated, and activation names the checks.
An unsupported check requested by the user is reported as unavailable — never
silently dropped, because a user who asked to be told about closures and hears
nothing will conclude there were none.

The change rules exist to stop a specific kind of noise:

* **Comparability before comparison.** "Price changed" needs a quote for the
  *same* dates, party and inclusions. A €200 quote for two people is not an
  increase over a €120 quote for one, and reporting it as one teaches the user
  to ignore alerts (TR17).
* **A no-change check invokes no model.** Deterministic comparison, so a quiet
  week costs nothing and cannot hallucinate a difference.
* **One notice per material change.** A provider that emits the same closure
  event forty times is forty events and one fact (TR16).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from enum import Enum
from zoneinfo import ZoneInfo

from loop.core.errors import InvalidInput

logger = logging.getLogger(__name__)


class Check(str, Enum):
    SCHEDULE = "schedule"
    CLOSURE = "closure"
    WEATHER = "weather"
    COST = "cost"


SUPPORTED_CHECKS = frozenset({Check.SCHEDULE, Check.CLOSURE, Check.WEATHER,
                              Check.COST})

#: Monitoring defaults (travel §7, §8).
PREDEPARTURE_DAYS = (7, 1)
DAILY_CHECK_AT = dt.time(7, 0)
EVENT_COOLDOWN_SECONDS = 21_600          # 6 hours
TRANSPORT_CHANGE_MINUTES = 30
COST_CHANGE_FRACTION = 0.2


@dataclass(frozen=True)
class Checkpoint:
    """One scheduled recheck, in UTC, with the local intent recorded."""

    at_utc: dt.datetime
    reason: str
    local_date: dt.date | None = None
    timezone: str = ""


@dataclass
class MonitorPlan:
    trip_id: str
    revision_id: str
    option_id: str
    checks: tuple[Check, ...]
    checkpoints: tuple[Checkpoint, ...]
    unsupported: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @property
    def is_activatable(self) -> bool:
        return bool(self.checkpoints) and not self.unsupported


def resolve_checks(requested: list[str] | None, *,
                   supported: frozenset[Check] = SUPPORTED_CHECKS
                   ) -> tuple[tuple[Check, ...], tuple[str, ...]]:
    """Resolve requested checks, naming any that cannot be done (TR23)."""
    if not requested:
        return tuple(sorted(supported, key=lambda c: c.value)), ()

    resolved: list[Check] = []
    unsupported: list[str] = []
    for name in requested:
        try:
            check = Check(name)
        except ValueError:
            unsupported.append(name)
            continue
        if check in supported:
            resolved.append(check)
        else:
            unsupported.append(name)
    return tuple(resolved), tuple(unsupported)


def build_checkpoints(*, departure_local: dt.datetime, travel_days: list[dt.date],
                      timezone: str, now_utc: dt.datetime,
                      owner_timezone: str = "",
                      predeparture_days: tuple[int, ...] = PREDEPARTURE_DAYS,
                      daily_at: dt.time = DAILY_CHECK_AT,
                      ) -> tuple[tuple[Checkpoint, ...], tuple[str, ...]]:
    """Build the bounded checkpoint set (travel §7).

    Past checkpoints are skipped rather than fired immediately: booking a trip
    three days out should not trigger the seven-days-before check the moment
    monitoring starts. Coinciding checkpoints deduplicate, so a departure-day
    daily check and a 24-hour check do not both fire.
    """
    zone = ZoneInfo(timezone)
    assumptions: list[str] = []
    seen: dict[dt.datetime, Checkpoint] = {}

    departure_utc = departure_local.replace(tzinfo=zone).astimezone(dt.UTC)
    for days in predeparture_days:
        moment = departure_utc - dt.timedelta(days=days)
        if moment <= now_utc:
            continue
        seen.setdefault(moment, Checkpoint(moment, f"{days} days before departure"))

    for day in sorted(travel_days):
        local = dt.datetime.combine(day, daily_at, tzinfo=zone)
        moment = local.astimezone(dt.UTC)
        if moment <= now_utc:
            continue
        seen.setdefault(moment, Checkpoint(
            moment, f"07:00 local on {day.isoformat()}", local_date=day,
            timezone=timezone))

    if owner_timezone and owner_timezone != timezone:
        assumptions.append(
            f"daily checks use {timezone}; on transit days that may differ "
            f"from your own timezone ({owner_timezone})")

    return tuple(sorted(seen.values(), key=lambda c: c.at_utc)), tuple(assumptions)


def plan_monitoring(*, trip_id: str, revision_id: str, option_id: str,
                    requested_checks: list[str] | None,
                    departure_local: dt.datetime, travel_days: list[dt.date],
                    timezone: str, now_utc: dt.datetime,
                    owner_timezone: str = "") -> MonitorPlan:
    """Prepare monitoring. Returns a plan; activation is a separate step."""
    if not option_id or not revision_id:
        raise InvalidInput(
            "Monitoring needs a selected revision and option.",
            details={"trip_id": trip_id})

    checks, unsupported = resolve_checks(requested_checks)
    checkpoints, assumptions = build_checkpoints(
        departure_local=departure_local, travel_days=travel_days,
        timezone=timezone, now_utc=now_utc, owner_timezone=owner_timezone)

    return MonitorPlan(trip_id=trip_id, revision_id=revision_id,
                       option_id=option_id, checks=checks,
                       checkpoints=checkpoints, unsupported=unsupported,
                       assumptions=assumptions)


# --------------------------------------------------------------------------- #
# Change detection
# --------------------------------------------------------------------------- #
class ChangeKind(str, Enum):
    NONE = "none"
    TRANSPORT_TIME = "transport_time"
    CLOSURE = "closure"
    COST = "cost"
    CONSTRAINT_BROKEN = "constraint_broken"
    NOT_COMPARABLE = "not_comparable"

    @property
    def notifies(self) -> bool:
        return self not in (ChangeKind.NONE, ChangeKind.NOT_COMPARABLE)


@dataclass(frozen=True)
class QuoteBasis:
    """What a quote is actually a quote *for* (TR17)."""

    start_date: str
    end_date: str
    travelers: int
    includes: tuple[str, ...] = ()

    def comparable_to(self, other: QuoteBasis) -> bool:
        return (self.start_date == other.start_date
                and self.end_date == other.end_date
                and self.travelers == other.travelers
                and set(self.includes) == set(other.includes))


@dataclass
class ChangeReport:
    kind: ChangeKind
    detail: str
    before: str = ""
    after: str = ""

    @property
    def notifies(self) -> bool:
        return self.kind.notifies


def compare_transport(before_minutes: int, after_minutes: int, *,
                      threshold: int = TRANSPORT_CHANGE_MINUTES) -> ChangeReport:
    """Deterministic. No model is invoked for a no-change check (§7)."""
    delta = abs(after_minutes - before_minutes)
    if delta < threshold:
        return ChangeReport(ChangeKind.NONE,
                            f"departure moved by {delta} minutes, below the "
                            f"{threshold}-minute threshold")
    return ChangeReport(ChangeKind.TRANSPORT_TIME,
                        f"departure moved by {delta} minutes",
                        before=str(before_minutes), after=str(after_minutes))


def compare_cost(before_minor: int, after_minor: int, *,
                 before_basis: QuoteBasis, after_basis: QuoteBasis,
                 currency: str = "EUR",
                 fraction: float = COST_CHANGE_FRACTION,
                 budget_minor: int | None = None) -> ChangeReport:
    """Compare two quotes, or refuse to (TR17)."""
    if not before_basis.comparable_to(after_basis):
        return ChangeReport(
            ChangeKind.NOT_COMPARABLE,
            "the new quote covers different dates, party size or inclusions, "
            "so it is not a price change")

    if before_minor <= 0:
        return ChangeReport(ChangeKind.NONE, "no comparable earlier quote")

    increase = (after_minor - before_minor) / before_minor
    over_budget = budget_minor is not None and after_minor > budget_minor

    if increase >= fraction or over_budget:
        reason = (f"cost rose {increase:.0%}" if increase >= fraction
                  else "cost now exceeds the budget")
        return ChangeReport(ChangeKind.COST, reason,
                            before=str(before_minor), after=str(after_minor))
    return ChangeReport(ChangeKind.NONE, f"cost changed {increase:.0%}")


@dataclass
class NoticeLedger:
    """Deduplicates notices per trip, subject and material revision (TR16)."""

    sent: dict[tuple[str, str], tuple] = field(default_factory=dict)
    last_event_at: dict[tuple[str, str], int] = field(default_factory=dict)

    def should_notify(self, *, trip_id: str, subject: str,
                      report: ChangeReport) -> bool:
        if not report.notifies:
            return False
        key = (trip_id, subject)
        signature = (report.kind, report.before, report.after)
        return self.sent.get(key) != signature

    def record(self, *, trip_id: str, subject: str,
               report: ChangeReport) -> None:
        self.sent[(trip_id, subject)] = (report.kind, report.before,
                                         report.after)

    def event_allowed(self, *, trip_id: str, subject: str, now: int,
                      explicit: bool = False,
                      cooldown: int = EVENT_COOLDOWN_SECONDS) -> bool:
        """Provider events are rate-limited; an explicit recheck is not (§7)."""
        if explicit:
            return True
        last = self.last_event_at.get((trip_id, subject))
        return last is None or now - last >= cooldown

    def record_event(self, *, trip_id: str, subject: str, now: int) -> None:
        self.last_event_at[(trip_id, subject)] = now

    def clear_trip(self, trip_id: str) -> int:
        """Drop pending notices for a cancelled trip (TR19)."""
        keys = [key for key in self.sent if key[0] == trip_id]
        for key in keys:
            del self.sent[key]
        for key in [k for k in self.last_event_at if k[0] == trip_id]:
            del self.last_event_at[key]
        return len(keys)
