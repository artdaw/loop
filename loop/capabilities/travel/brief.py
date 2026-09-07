"""The trip brief: what the user asked for, and what is still unknown (§3).

The brief is saved *before* research, so a request survives a failure partway
through. Unknowns stay visible rather than being guessed into compliance: a
missing origin means outbound feasibility cannot be claimed, and missing dates
mean a provisional outline — never an invented fare for a date nobody chose.

Only *blocking* details produce a question. Everything else uses a disclosed
default, because asking six questions before starting is its own failure mode.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from enum import Enum

from loop.core.privacy import ModelScope, PrivacyLabel

logger = logging.getLogger(__name__)


class Pace(str, Enum):
    RELAXED = "relaxed"
    BALANCED = "balanced"
    BUSY = "busy"


#: Anchor activities per day, excluding meals and transit (travel §5, §8).
ANCHOR_LIMITS = {Pace.RELAXED: 2, Pace.BALANCED: 3, Pace.BUSY: 4}

DEFAULT_PACE = Pace.BALANCED
MAX_CANDIDATES = 5
MAX_OPTIONS = 3


@dataclass(frozen=True)
class PlaceRef:
    """A place. Ambiguous until disambiguated — coordinates require it (§3)."""

    label: str
    country_code: str = ""
    provider_place_id: str = ""
    latitude: float | None = None
    longitude: float | None = None
    timezone: str = ""

    @property
    def is_resolved(self) -> bool:
        """Whether this place may be used for coordinates or transfer times."""
        return (self.latitude is not None and self.longitude is not None
                and bool(self.timezone))

    def to_json(self) -> dict:
        return {"label": self.label, "country_code": self.country_code,
                "provider_place_id": self.provider_place_id,
                "latitude": self.latitude, "longitude": self.longitude,
                "timezone": self.timezone}

    @classmethod
    def from_json(cls, data: dict) -> PlaceRef:
        return cls(**data)


@dataclass
class DateWindow:
    """Fixed dates, or a flexible window with a duration (§3)."""

    start_date: dt.date | None = None
    end_date: dt.date | None = None
    earliest_departure: dt.date | None = None
    latest_return: dt.date | None = None
    duration_days: int | None = None

    def __post_init__(self) -> None:
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date is before start_date")
        if self.duration_days is not None and self.duration_days <= 0:
            raise ValueError("duration_days must be positive")
        if self.is_fixed and self.is_flexible:
            raise ValueError(
                "use exactly one date representation: fixed dates or a window")

    def to_json(self) -> dict:
        return {
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "earliest_departure": self.earliest_departure.isoformat()
            if self.earliest_departure else None,
            "latest_return": self.latest_return.isoformat()
            if self.latest_return else None,
            "duration_days": self.duration_days}

    @classmethod
    def from_json(cls, data: dict) -> DateWindow:
        parse = lambda v: dt.date.fromisoformat(v) if v else None  # noqa: E731
        return cls(start_date=parse(data.get("start_date")),
                  end_date=parse(data.get("end_date")),
                  earliest_departure=parse(data.get("earliest_departure")),
                  latest_return=parse(data.get("latest_return")),
                  duration_days=data.get("duration_days"))

    @property
    def is_fixed(self) -> bool:
        return self.start_date is not None and self.end_date is not None

    @property
    def is_flexible(self) -> bool:
        return (self.earliest_departure is not None
                and self.latest_return is not None
                and self.duration_days is not None)

    @property
    def is_known(self) -> bool:
        return self.is_fixed or self.is_flexible

    def window_fits(self) -> bool:
        """Whether the flexible window is long enough for the trip."""
        if not self.is_flexible:
            return True
        assert self.latest_return and self.earliest_departure
        span = (self.latest_return - self.earliest_departure).days + 1
        return span >= (self.duration_days or 0)

    def display(self) -> str:
        """Always includes the year: "3–8 Oct" is ambiguous in December."""
        if self.is_fixed:
            assert self.start_date and self.end_date
            return (f"{self.start_date.isoformat()} to "
                    f"{self.end_date.isoformat()}")
        if self.is_flexible:
            assert self.earliest_departure and self.latest_return
            return (f"{self.duration_days} days between "
                    f"{self.earliest_departure.isoformat()} and "
                    f"{self.latest_return.isoformat()}")
        return "dates not yet chosen"


@dataclass
class Travelers:
    adults: int = 1
    children: int = 0
    #: Only when a quote actually needs them. No inferred passport data.
    child_ages: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.adults < 1:
            raise ValueError("a trip needs at least one adult traveller")
        if self.children < 0:
            raise ValueError("children cannot be negative")

    @property
    def count(self) -> int:
        return self.adults + self.children

    def to_json(self) -> dict:
        return {"adults": self.adults, "children": self.children,
                "child_ages": list(self.child_ages)}

    @classmethod
    def from_json(cls, data: dict) -> Travelers:
        return cls(adults=data.get("adults", 1), children=data.get("children", 0),
                  child_ages=tuple(data.get("child_ages", ())))


@dataclass
class Budget:
    amount_minor: int
    currency: str = "EUR"
    basis: str = "total"
    includes: tuple[str, ...] = ()
    hard_limit: bool = True

    def total_minor(self, *, travelers: int) -> int:
        return (self.amount_minor * travelers if self.basis == "per_person"
                else self.amount_minor)

    def to_json(self) -> dict:
        return {"amount_minor": self.amount_minor, "currency": self.currency,
                "basis": self.basis, "includes": list(self.includes),
                "hard_limit": self.hard_limit}

    @classmethod
    def from_json(cls, data: dict) -> Budget:
        return cls(amount_minor=data["amount_minor"],
                  currency=data.get("currency", "EUR"),
                  basis=data.get("basis", "total"),
                  includes=tuple(data.get("includes", ())),
                  hard_limit=data.get("hard_limit", True))


@dataclass
class Assumption:
    """A disclosed provisional value. Never satisfies a hard constraint (§3)."""

    field: str
    value: str
    reason: str
    source: str = "default"

    def to_json(self) -> dict:
        return {"field": self.field, "value": self.value,
                "reason": self.reason, "source": self.source}

    @classmethod
    def from_json(cls, data: dict) -> Assumption:
        return cls(**data)


@dataclass
class TripBrief:
    """The saved request (travel §3). Immutable once versioned."""

    description: str = ""
    origin: PlaceRef | None = None
    destinations: tuple[PlaceRef, ...] = ()
    date_window: DateWindow = field(default_factory=DateWindow)
    travelers: Travelers = field(default_factory=Travelers)
    budget: Budget | None = None
    interests: dict[str, float] = field(default_factory=dict)
    pace: Pace = DEFAULT_PACE
    allowed_modes: tuple[str, ...] = ()
    preferred_modes: tuple[str, ...] = ()
    max_transfers: int | None = None
    must_include: tuple[str, ...] = ()
    must_avoid: tuple[str, ...] = ()
    mobility_requirements: tuple[str, ...] = ()
    fixed_segments: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()
    priorities: tuple[str, ...] = ()
    assumptions: list[Assumption] = field(default_factory=list)
    version: int = 1
    #: Personal context stays local (travel §3).
    privacy: PrivacyLabel = field(
        default_factory=lambda: PrivacyLabel(model_scope=ModelScope.LOCAL_ONLY))

    @property
    def is_discovery(self) -> bool:
        """No destination named — candidates are compared first (TR02)."""
        return not self.destinations

    @property
    def anchor_limit(self) -> int:
        return ANCHOR_LIMITS[self.pace]

    def disclose(self, field_name: str, value: str, reason: str) -> Assumption:
        assumption = Assumption(field_name, value, reason)
        self.assumptions.append(assumption)
        return assumption

    def to_json(self) -> dict:
        """The whole brief as plain data, for the trip record to persist.

        Everything here is either a primitive or one of the small nested
        types above with its own `to_json`/`from_json` — no datetimes or
        segment-level detail, which is why the brief round-trips cleanly
        while a full itinerary result (§ TripStore) does not yet.
        """
        return {
            "description": self.description,
            "origin": self.origin.to_json() if self.origin else None,
            "destinations": [d.to_json() for d in self.destinations],
            "date_window": self.date_window.to_json(),
            "travelers": self.travelers.to_json(),
            "budget": self.budget.to_json() if self.budget else None,
            "interests": dict(self.interests),
            "pace": self.pace.value,
            "allowed_modes": list(self.allowed_modes),
            "preferred_modes": list(self.preferred_modes),
            "max_transfers": self.max_transfers,
            "must_include": list(self.must_include),
            "must_avoid": list(self.must_avoid),
            "mobility_requirements": list(self.mobility_requirements),
            "fixed_segments": list(self.fixed_segments),
            "context_refs": list(self.context_refs),
            "priorities": list(self.priorities),
            "assumptions": [a.to_json() for a in self.assumptions],
            "version": self.version,
            "privacy": self.privacy.to_json(),
        }

    @classmethod
    def from_json(cls, data: dict) -> TripBrief:
        return cls(
            description=data.get("description", ""),
            origin=PlaceRef.from_json(data["origin"]) if data.get("origin")
            else None,
            destinations=tuple(PlaceRef.from_json(d)
                               for d in data.get("destinations", ())),
            date_window=DateWindow.from_json(data.get("date_window", {})),
            travelers=Travelers.from_json(data.get("travelers", {})),
            budget=Budget.from_json(data["budget"]) if data.get("budget")
            else None,
            interests=dict(data.get("interests", {})),
            pace=Pace(data.get("pace", DEFAULT_PACE.value)),
            allowed_modes=tuple(data.get("allowed_modes", ())),
            preferred_modes=tuple(data.get("preferred_modes", ())),
            max_transfers=data.get("max_transfers"),
            must_include=tuple(data.get("must_include", ())),
            must_avoid=tuple(data.get("must_avoid", ())),
            mobility_requirements=tuple(data.get("mobility_requirements", ())),
            fixed_segments=tuple(data.get("fixed_segments", ())),
            context_refs=tuple(data.get("context_refs", ())),
            priorities=tuple(data.get("priorities", ())),
            assumptions=[Assumption.from_json(a)
                        for a in data.get("assumptions", ())],
            version=data.get("version", 1),
            privacy=PrivacyLabel.from_json(data.get("privacy")))


@dataclass
class MissingInformation:
    field: str
    question: str
    blocking: bool


def validate_brief(brief: TripBrief) -> list[MissingInformation]:
    """Identify what is missing, separating blockers from provisional gaps (§3).

    A missing origin does not stop planning; it stops *claiming* outbound
    feasibility. That distinction is the difference between a useful tentative
    outline and a refusal to help.
    """
    missing: list[MissingInformation] = []

    if brief.origin is None:
        missing.append(MissingInformation(
            "origin", "Where are you travelling from?", blocking=False))
    elif not brief.origin.is_resolved:
        missing.append(MissingInformation(
            "origin",
            f"Which {brief.origin.label} do you mean? I need the exact place "
            f"before I can compute transfer times.", blocking=True))

    if not brief.date_window.is_known:
        missing.append(MissingInformation(
            "date_window", "Which dates, or roughly which period?",
            blocking=False))
    elif not brief.date_window.window_fits():
        missing.append(MissingInformation(
            "date_window",
            "The window is shorter than the trip length you asked for. Which "
            "should I change?", blocking=True))

    for destination in brief.destinations:
        if not destination.is_resolved:
            missing.append(MissingInformation(
                "destinations",
                f"Which {destination.label} do you mean?", blocking=True))

    return missing


def can_claim_feasibility(brief: TripBrief) -> bool:
    """Outbound/return feasibility needs a resolved origin and known dates."""
    return (brief.origin is not None and brief.origin.is_resolved
            and brief.date_window.is_known)


#: Fields that must never leave the machine in an external query (travel §3).
PRIVATE_FIELDS = frozenset({
    "context_refs", "reservation_code", "passport", "payment", "profile",
    "calendar_title", "description",
})


def external_query_payload(brief: TripBrief) -> dict[str, object]:
    """The only fields an external search may receive (TR21).

    Built by naming what goes out, not by removing what must not. A remove-list
    silently ships each new field until someone notices.
    """
    payload: dict[str, object] = {
        "destinations": [place.label for place in brief.destinations],
        "travelers": brief.travelers.count,
        "modes": list(brief.preferred_modes or brief.allowed_modes),
    }
    if brief.origin is not None:
        payload["origin"] = brief.origin.label
    if brief.date_window.is_fixed:
        assert brief.date_window.start_date and brief.date_window.end_date
        payload["start_date"] = brief.date_window.start_date.isoformat()
        payload["end_date"] = brief.date_window.end_date.isoformat()
    return payload
