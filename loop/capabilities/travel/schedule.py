"""Segments, days and deterministic feasibility checks (travel §5).

Every check here catches a way an itinerary can look fine on paper and fail in
person:

* **Overnight and date-line travel.** A segment leaving Tokyo at 22:00 and
  landing in Berlin at 04:00 arrives on a *different* date, and in a different
  zone. Storing local times without their zones makes both dates plausible and
  one of them wrong (TR06).
* **Omitted legs.** An itinerary that starts at the departure airport has
  quietly assumed the user teleports there. Door-to-door means the first leg is
  from the origin.
* **Opening days and last admission.** Arriving at 17:40 for a 17:30 last
  admission is not a short wait; it is a closed door.
* **Unknown accessibility.** Absence of an accessibility note is not a note
  saying it is accessible (TR07).

Buffers are disclosed, and a conservative buffer is never described as a
guarantee — operator connection rules override generic estimates.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from enum import Enum
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

#: Default local travel buffer (travel §5, §8).
LOCAL_BUFFER_MINUTES = 15
LOCAL_BUFFER_FRACTION = 0.2


class SegmentType(str, Enum):
    TRANSPORT = "transport"
    ACTIVITY = "activity"
    MEAL = "meal"
    LODGING = "lodging"
    REST = "rest"

    @property
    def is_anchor(self) -> bool:
        """Anchors count against the pace limit; meals and transit do not."""
        return self is SegmentType.ACTIVITY


class BookingStatus(str, Enum):
    UNBOOKED = "unbooked"
    USER_REPORTED = "user_reported"
    VERIFIED = "verified"


class Accessibility(str, Enum):
    ACCESSIBLE = "accessible"
    NOT_ACCESSIBLE = "not_accessible"
    UNKNOWN = "unknown"


@dataclass
class Segment:
    """One scheduled block, with its zone attached to its local times."""

    id: str
    type: SegmentType
    start_local: dt.datetime
    end_local: dt.datetime
    timezone: str
    location: str = ""
    end_timezone: str = ""
    route_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    cost_refs: tuple[str, ...] = ()
    booking_status: BookingStatus = BookingStatus.UNBOOKED
    fixed: bool = False
    accessibility: Accessibility = Accessibility.UNKNOWN

    def __post_init__(self) -> None:
        if self.end_local < self.start_local:
            raise ValueError(f"segment {self.id} ends before it starts")

    @property
    def arrival_timezone(self) -> str:
        """Where it *ends* — different from the start for a flight."""
        return self.end_timezone or self.timezone

    @property
    def start_utc(self) -> dt.datetime:
        return self.start_local.replace(
            tzinfo=ZoneInfo(self.timezone)).astimezone(dt.UTC)

    @property
    def end_utc(self) -> dt.datetime:
        return self.end_local.replace(
            tzinfo=ZoneInfo(self.arrival_timezone)).astimezone(dt.UTC)

    @property
    def arrival_local_date(self) -> dt.date:
        """The date it is *where you land*, which is the date that matters.

        `end_local` is by definition already in `arrival_timezone`, so this is a
        plain read. Routing it through UTC and back would look like it were
        doing work and would compute the same value. The check that actually
        carries the timezone correctness is `end_utc`, which converts using
        `arrival_timezone` rather than the departure zone.
        """
        return self.end_local.date()

    @property
    def duration_minutes(self) -> int:
        return int((self.end_utc - self.start_utc).total_seconds() // 60)

    @property
    def is_anchor(self) -> bool:
        return self.type.is_anchor

    @property
    def is_confirmed_booking(self) -> bool:
        """A confirmed booking is an anchor a revision must not move (TR18)."""
        return self.fixed and self.booking_status in (
            BookingStatus.VERIFIED, BookingStatus.USER_REPORTED)


@dataclass
class OpeningRule:
    """When a venue is actually open (travel §5)."""

    venue: str
    #: Weekday numbers it opens, Monday=0.
    open_weekdays: frozenset[int] = frozenset(range(7))
    opens_at: dt.time = dt.time(0, 0)
    closes_at: dt.time = dt.time(23, 59)
    last_admission: dt.time | None = None
    fetched_at: int = 0

    def admits(self, moment: dt.datetime) -> tuple[bool, str]:
        if moment.weekday() not in self.open_weekdays:
            return False, f"{self.venue} is closed on that day"
        if moment.time() < self.opens_at:
            return False, f"{self.venue} does not open until {self.opens_at:%H:%M}"
        if self.last_admission is not None and moment.time() > self.last_admission:
            return False, (f"{self.venue} stops admitting at "
                           f"{self.last_admission:%H:%M}")
        if moment.time() >= self.closes_at:
            return False, f"{self.venue} closes at {self.closes_at:%H:%M}"
        return True, ""


class Severity(str, Enum):
    VIOLATION = "violation"
    UNRESOLVED = "unresolved"


@dataclass
class ScheduleFinding:
    """A problem, or an unknown. Both matter; they mean different things."""

    code: str
    detail: str
    severity: Severity = Severity.VIOLATION
    segment_id: str = ""

    @property
    def is_violation(self) -> bool:
        return self.severity is Severity.VIOLATION


def buffer_minutes(leg_minutes: int, *,
                   minimum: int = LOCAL_BUFFER_MINUTES,
                   fraction: float = LOCAL_BUFFER_FRACTION) -> int:
    """max(15 minutes, 20% of the leg) — disclosed, not guaranteed (§5)."""
    return max(minimum, int(round(leg_minutes * fraction)))


def check_overlaps(segments: list[Segment]) -> list[ScheduleFinding]:
    """Two things at once is the most common invisible defect."""
    findings: list[ScheduleFinding] = []
    ordered = sorted(segments, key=lambda s: s.start_utc)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if later.start_utc < earlier.end_utc:
            findings.append(ScheduleFinding(
                "overlap",
                f"{earlier.id} runs until {earlier.end_local:%H:%M} but "
                f"{later.id} starts at {later.start_local:%H:%M}",
                segment_id=later.id))
    return findings


def check_transfers(segments: list[Segment], *,
                    minimum: int = LOCAL_BUFFER_MINUTES,
                    fraction: float = LOCAL_BUFFER_FRACTION,
                    operator_minimums: dict[str, int] | None = None,
                    ) -> list[ScheduleFinding]:
    """Check each connection against its required buffer (TR06).

    An operator's published connection minimum overrides the generic estimate:
    a 20-minute rule of thumb does not survive an airport that requires 60.
    """
    findings: list[ScheduleFinding] = []
    required_by_segment = operator_minimums or {}
    transport = sorted((s for s in segments if s.type is SegmentType.TRANSPORT),
                       key=lambda s: s.start_utc)

    for arriving, departing in zip(transport, transport[1:], strict=False):
        gap = int((departing.start_utc - arriving.end_utc).total_seconds() // 60)
        required = required_by_segment.get(
            departing.id, buffer_minutes(arriving.duration_minutes,
                                         minimum=minimum, fraction=fraction))
        if gap < required:
            findings.append(ScheduleFinding(
                "tight_transfer",
                f"{gap} minutes between {arriving.id} and {departing.id}; "
                f"{required} minutes are needed",
                segment_id=departing.id))
    return findings


def check_door_to_door(segments: list[Segment], *, origin: str,
                       departure_point: str) -> list[ScheduleFinding]:
    """The first leg must start at the origin, not at the airport (TR06)."""
    transport = [s for s in segments if s.type is SegmentType.TRANSPORT]
    if not transport:
        return [ScheduleFinding("no_transport", "the plan includes no travel")]

    first = min(transport, key=lambda s: s.start_utc)
    if first.location.strip().lower() == departure_point.strip().lower() \
            and origin.strip().lower() != departure_point.strip().lower():
        return [ScheduleFinding(
            "omitted_leg",
            f"the plan starts at {departure_point} with no leg from {origin}",
            segment_id=first.id)]
    return []


def check_opening(segments: list[Segment],
                  rules: dict[str, OpeningRule]) -> list[ScheduleFinding]:
    """Check each activity against its venue's opening rule (TR05)."""
    findings: list[ScheduleFinding] = []
    for segment in segments:
        rule = rules.get(segment.location)
        if rule is None:
            if segment.is_anchor:
                findings.append(ScheduleFinding(
                    "unknown_hours",
                    f"opening hours for {segment.location} are not known",
                    severity=Severity.UNRESOLVED, segment_id=segment.id))
            continue
        admitted, reason = rule.admits(segment.start_local)
        if not admitted:
            findings.append(ScheduleFinding("closed", reason,
                                            segment_id=segment.id))
    return findings


def check_pace(segments: list[Segment], *, anchor_limit: int
               ) -> list[ScheduleFinding]:
    """Count anchor activities per destination-local day (travel §5)."""
    per_day: dict[dt.date, int] = {}
    for segment in segments:
        if segment.is_anchor:
            day = segment.start_local.date()
            per_day[day] = per_day.get(day, 0) + 1

    return [ScheduleFinding(
        "pace", f"{count} anchor activities on {day.isoformat()}, above the "
                f"limit of {anchor_limit} for this pace")
        for day, count in sorted(per_day.items()) if count > anchor_limit]


def check_accessibility(segments: list[Segment], *, required: bool
                        ) -> list[ScheduleFinding]:
    """Unknown accessibility is unresolved, never verified accessible (TR07)."""
    if not required:
        return []
    findings: list[ScheduleFinding] = []
    for segment in segments:
        if segment.accessibility is Accessibility.NOT_ACCESSIBLE:
            findings.append(ScheduleFinding(
                "not_accessible", f"{segment.location} is not step-free",
                segment_id=segment.id))
        elif segment.accessibility is Accessibility.UNKNOWN and segment.is_anchor:
            findings.append(ScheduleFinding(
                "accessibility_unknown",
                f"step-free access at {segment.location} is not confirmed",
                severity=Severity.UNRESOLVED, segment_id=segment.id))
    return findings


def days_of(segments: list[Segment]) -> dict[dt.date, list[Segment]]:
    """Group by destination-local date, using arrival dates for travel (§5)."""
    grouped: dict[dt.date, list[Segment]] = {}
    for segment in sorted(segments, key=lambda s: s.start_utc):
        grouped.setdefault(segment.start_local.date(), []).append(segment)
    return grouped
