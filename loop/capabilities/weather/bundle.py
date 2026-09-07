"""Bundle and brief assembly (weather §5, §6).

A bundle is evidence with its provenance attached; a brief is what a person
reads. Both are produced deterministically — a model may phrase the brief, but
raw values, provenance, warnings and uncertainty pass through unchanged, and no
model call is needed when a template renders the result.

Three refusals live here, and each is a case where the tempting behaviour is
worse than saying less:

* **Missing is not zero.** No precipitation data means unknown, never "no rain"
  (WF16). A window past every source's horizon returns missing coverage rather
  than an extrapolation.
* **An outage is not calm weather.** Losing three of four providers yields a
  bounded partial result that names what was skipped, not a confident forecast
  from what survived (WF18, WF02).
* **Feed text is data.** An instruction embedded in a provider's payload is
  content to display, never a request to obey (WF19).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from loop.capabilities.weather.compare import (
    Confidence,
    Coverage,
    FieldComparison,
    compare_field,
    coverage_state,
    crosses_threshold_only_in_comparator,
)
from loop.capabilities.weather.normalize import (
    Freshness,
    WeatherSample,
    aligned,
    normalize,
)
from loop.capabilities.weather.sources import (
    MAX_PROVIDER_REQUESTS,
    ProductType,
    SelectionFinding,
    SourceCatalogue,
    SourceDescriptor,
)
from loop.capabilities.weather.warnings import (
    FeedRead,
    OfficialWarning,
    WarningState,
    warning_state,
)
from loop.core.errors import NeedsClarification
from loop.core.privacy import PrivacyLabel

logger = logging.getLogger(__name__)


class BundleStatus(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass
class WeatherRequest:
    """Exactly one of ``location_ref`` or an explicit location (weather §2)."""

    location_ref: str = ""
    latitude: float | None = None
    longitude: float | None = None
    timezone: str = ""
    elevation_m: float | None = None
    window_start: int | None = None
    window_end: int | None = None
    horizon_hours: float = 3.0
    purposes: tuple[str, ...] = ("general",)
    fields: tuple[str, ...] = ("temperature", "precipitation_probability",
                               "wind_speed")
    source_ids: tuple[str, ...] = ()
    compare: bool = True
    schema_version: int = 1

    def resolved_location(self, resolver: dict[str, dict[str, Any]] | None = None
                          ) -> dict[str, Any]:
        """Resolve coordinates, asking rather than guessing (WF16).

        A timezone never determines coordinates. `Europe/Berlin` is most of a
        country, and picking a point inside it would produce confident advice
        about somewhere the user is not.
        """
        if self.latitude is not None and self.longitude is not None:
            return {"latitude": self.latitude, "longitude": self.longitude,
                    "timezone": self.timezone, "elevation_m": self.elevation_m}
        entry = (resolver or {}).get(self.location_ref)
        if entry is None:
            raise NeedsClarification(
                "Which location should I check the weather for?",
                details={"location_ref": self.location_ref,
                         "field": "location"})
        return entry

    def window(self, *, now: int) -> tuple[int, int]:
        if self.window_start is not None and self.window_end is not None:
            if self.window_end <= self.window_start:
                raise ValueError("weather window must be positive")
            return self.window_start, self.window_end
        return now, now + int(self.horizon_hours * 3600)


@dataclass
class SourceError:
    source_id: str
    reason: str
    attempted: bool = True


@dataclass
class WeatherBundle:
    """Evidence plus provenance. One bundle serves every surface (WF24)."""

    id: str
    request: WeatherRequest
    generated_at: int
    samples: list[WeatherSample] = field(default_factory=list)
    sources_attempted: list[str] = field(default_factory=list)
    sources_skipped: list[SelectionFinding] = field(default_factory=list)
    comparisons: list[FieldComparison] = field(default_factory=list)
    selected_by_field: dict[str, str] = field(default_factory=dict)
    coverage_by_product: dict[str, str] = field(default_factory=dict)
    warnings: list[OfficialWarning] = field(default_factory=list)
    warning_state: WarningState = WarningState.UNKNOWN
    warning_caveats: list[str] = field(default_factory=list)
    source_errors: list[SourceError] = field(default_factory=list)
    #: Sources whose publisher does not state an issue time.
    unknown_freshness: list[str] = field(default_factory=list)
    overall_status: BundleStatus = BundleStatus.UNAVAILABLE
    expires_at: int | None = None
    policy_revision: str = ""
    privacy: PrivacyLabel = field(default_factory=PrivacyLabel)

    def comparison_for(self, variable: str) -> FieldComparison | None:
        return next((c for c in self.comparisons if c.variable == variable), None)

    def value_for(self, variable: str) -> float | None:
        comparison = self.comparison_for(variable)
        return comparison.primary_value if comparison else None

    def matches(self, request: WeatherRequest, *, now: int,
                policy_revision: str) -> bool:
        """A cached bundle is reusable only if it answers *this* question (§2)."""
        if self.expires_at is not None and now >= self.expires_at:
            return False
        if self.policy_revision != policy_revision:
            return False
        return (self.request.location_ref == request.location_ref
                and self.request.window(now=self.generated_at)
                == request.window(now=self.generated_at)
                and set(self.request.fields) >= set(request.fields))


# --------------------------------------------------------------------------- #
# Untrusted provider text (WF19)
# --------------------------------------------------------------------------- #
_INSTRUCTION_PATTERNS = (
    r"ignore (?:all )?previous", r"disregard (?:the )?(?:above|instructions)",
    r"you are now", r"system:", r"fetch https?://", r"send .* to https?://",
    r"call the .* tool", r"reveal", r"api[ _-]?key",
)


@dataclass
class UntrustedFeedText:
    """Provider prose. Displayable, never executable.

    Warning instructions are meant to be read by a person, so they are kept
    verbatim — but they arrive over the network from outside the trust boundary,
    and a payload that says "also fetch this URL" is a payload, not a policy.
    """

    value: str
    source_id: str

    def __str__(self) -> str:
        return self.value

    @property
    def looks_like_instructions(self) -> bool:
        lowered = self.value.lower()
        return any(re.search(pattern, lowered) for pattern in _INSTRUCTION_PATTERNS)

    def for_display(self) -> str:
        return self.value

    def for_model(self) -> str:
        """Fenced when handed to a model, so prose cannot become direction."""
        return (f"<untrusted source=\"{self.source_id}\">\n{self.value}\n"
                f"</untrusted>")


# --------------------------------------------------------------------------- #
# Bounded fetching (WF18)
# --------------------------------------------------------------------------- #
@dataclass
class FetchBudget:
    """Provider requests are capped per call, whatever the plan asks for."""

    max_requests: int = MAX_PROVIDER_REQUESTS
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_requests - self.used)

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.used += 1
        return True


def fetch_samples(descriptors: list[SourceDescriptor],
                  fetcher: Any, *, budget: FetchBudget,
                  ) -> tuple[list[WeatherSample], list[SourceError]]:
    """Fetch from each source in order, stopping at the budget.

    A provider that fails or times out costs its request and produces an error
    entry — the successful sources are kept. Discarding a whole bundle because
    one provider was slow turns a partial answer into no answer (WF18).
    """
    samples: list[WeatherSample] = []
    errors: list[SourceError] = []

    for descriptor in descriptors:
        if not budget.take():
            errors.append(SourceError(
                descriptor.id, "not checked: provider request budget reached",
                attempted=False))
            continue
        try:
            fetched = fetcher(descriptor)
        except Exception as exc:                       # noqa: BLE001 — reported
            errors.append(SourceError(
                descriptor.id, f"{exc.__class__.__name__}: {exc}"))
            continue
        samples.extend(normalize(sample) for sample in fetched)

    return samples, errors


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def build_bundle(request: WeatherRequest, *, bundle_id: str, now: int,
                 samples: list[WeatherSample],
                 catalogue: SourceCatalogue,
                 preferred_ids: list[str] | None = None,
                 source_errors: list[SourceError] | None = None,
                 skipped: list[SelectionFinding] | None = None,
                 warning_reads: list[FeedRead] | None = None,
                 location: dict[str, Any] | None = None,
                 policy_revision: str = "",
                 privacy: PrivacyLabel | None = None) -> WeatherBundle:
    """Assemble evidence into a bundle, honest about every gap."""
    errors = list(source_errors or [])
    bundle = WeatherBundle(
        id=bundle_id, request=request, generated_at=now,
        samples=list(samples),
        sources_attempted=sorted({s.source_id for s in samples}
                                 | {e.source_id for e in errors if e.attempted}),
        sources_skipped=list(skipped or []),
        source_errors=errors, policy_revision=policy_revision,
        privacy=privacy or PrivacyLabel())

    preference = list(preferred_ids or [])

    # Stale and unknown are different answers and get different treatment.
    # A stale product has a known issue time that is too old, so it is excluded.
    # An unknown one has no published issue time at all: it is still the data
    # the provider is serving, so it is used — but it cannot support a claim of
    # *verified current* coverage, so the bundle is degraded and says why
    # (weather §4). Collapsing the two either throws away every source that
    # does not publish a run time, or lets one masquerade as fresh.
    stale_ids: set[str] = set()
    unknown_freshness_ids: set[str] = set()
    for sample in samples:
        descriptor = catalogue.get(sample.source_id)
        if descriptor is None:
            continue
        age = sample.freshness(
            now=now, max_issue_age_seconds=descriptor.max_issue_age_seconds)
        if age is Freshness.STALE:
            stale_ids.add(sample.source_id)
        elif age is Freshness.UNKNOWN:
            unknown_freshness_ids.add(sample.source_id)

    usable = [s for s in samples if s.source_id not in stale_ids]
    for source_id in sorted(stale_ids):
        errors.append(SourceError(source_id, "data older than its declared max issue age"))
    bundle.unknown_freshness = sorted(unknown_freshness_ids)

    for group in aligned(usable):
        variable = group[0].variable
        if variable not in request.fields:
            continue
        primary = _primary_for(group, preference)
        comparison = compare_field(group, primary_source_id=primary)
        bundle.comparisons.append(comparison)
        bundle.selected_by_field[variable] = comparison.primary_source_id

    for sample in usable:
        bundle.coverage_by_product.setdefault(sample.product_type, "available")

    if location and warning_reads is not None:
        state, applicable, caveats = warning_state(
            warning_reads, now=now,
            latitude=float(location["latitude"]),
            longitude=float(location["longitude"]),
            window=request.window(now=now))
        bundle.warning_state = state
        bundle.warnings = applicable
        bundle.warning_caveats = caveats
    elif warning_reads is None:
        bundle.warning_caveats = ["no warning feed was checked"]

    bundle.expires_at = _earliest_expiry(usable, catalogue, now=now)
    bundle.overall_status = _status(bundle, request, usable, stale_ids)
    return bundle


def _primary_for(group: list[WeatherSample], preference: list[str]) -> str:
    """Prefer the configured source when it is actually present in the group."""
    for source_id in preference:
        if any(s.source_id == source_id for s in group):
            return source_id
    return sorted(s.source_id for s in group)[0]


def _earliest_expiry(samples: list[WeatherSample], catalogue: SourceCatalogue, *,
                     now: int) -> int | None:
    """The earliest relevant product expiry.

    Per-product status is retained separately so one expired optional field
    cannot be presented as fresh, nor erase the healthy fields (weather §5).
    """
    from loop.capabilities.weather.sources import CACHE_TTL_SECONDS, ProductType

    expiries = []
    for sample in samples:
        try:
            product = ProductType(sample.product_type)
        except ValueError:
            continue
        expiries.append(sample.fetched_at + CACHE_TTL_SECONDS[product])
    return min(expiries) if expiries else None


def _status(bundle: WeatherBundle, request: WeatherRequest,
            usable: list[WeatherSample], stale_ids: set[str]) -> BundleStatus:
    if not usable:
        return BundleStatus.UNAVAILABLE

    state = coverage_state(usable, requested_fields=list(request.fields),
                           stale_source_ids=stale_ids)
    if state is not Coverage.COMPARED:
        return BundleStatus.DEGRADED
    # A usable forecast with missing requested alerts or comparison is degraded.
    if bundle.warning_state is WarningState.UNKNOWN:
        return BundleStatus.DEGRADED
    if request.compare and any(
            c.confidence in (Confidence.SINGLE_SOURCE, Confidence.CORRELATED_SOURCES)
            for c in bundle.comparisons):
        return BundleStatus.DEGRADED
    if bundle.unknown_freshness:
        return BundleStatus.DEGRADED
    if bundle.source_errors:
        return BundleStatus.DEGRADED
    return BundleStatus.AVAILABLE


# --------------------------------------------------------------------------- #
# The brief (weather §5)
# --------------------------------------------------------------------------- #
@dataclass
class WeatherBrief:
    headline: str
    bundle_id: str
    checked_at: int
    forecast_summary: list[str] = field(default_factory=list)
    preparation: list[str] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)
    source_links: list[str] = field(default_factory=list)
    notification_candidates: list[dict[str, Any]] = field(default_factory=list)


#: Action thresholds reused from the interfaces preparation rules.
RAIN_ACTION_PROBABILITY = 40.0
WIND_ACTION_KPH = 40.0
COLD_ACTION_C = 5.0


def render_brief(bundle: WeatherBundle) -> WeatherBrief:
    """Render deterministically. No model call is required for this (weather §1)."""
    brief = WeatherBrief(headline="", bundle_id=bundle.id,
                         checked_at=bundle.generated_at)

    if bundle.overall_status is BundleStatus.UNAVAILABLE:
        brief.headline = "I could not get weather data for this location."
        brief.uncertainty.append(
            "No source returned usable data, so conditions are unknown — this "
            "is not a forecast of calm weather.")
        for error in bundle.source_errors:
            brief.uncertainty.append(f"{error.source_id}: {error.reason}")
        return brief

    for comparison in bundle.comparisons:
        if comparison.primary_value is None:
            brief.forecast_summary.append(
                f"{comparison.variable}: not supplied by any checked source")
            continue
        line = (f"{comparison.variable}: {comparison.primary_value:g} "
                f"{comparison.unit} ({comparison.primary_source_id})")
        if comparison.disagrees:
            low, high = comparison.value_range or (0.0, 0.0)
            line += f"; other sources {low:g}–{high:g} {comparison.unit}"
        brief.forecast_summary.append(line)
        brief.uncertainty.extend(comparison.notes)

    _note_present_versus_future(bundle, brief)
    _add_preparation(bundle, brief)

    if bundle.warning_state is WarningState.ACTIVE:
        for warning in bundle.warnings:
            brief.forecast_summary.append(
                f"{warning.publisher} {warning.event_type} warning "
                f"({warning.severity.value}): {warning.headline}")
            if warning.instruction:
                brief.preparation.append(
                    f"{warning.publisher}: {warning.instruction}")
            brief.source_links.append(warning.source_url)
    elif bundle.warning_state is WarningState.NONE_IN_CHECKED_FEED:
        brief.uncertainty.append(
            "No active warnings in the official feed that was checked.")
    else:
        brief.uncertainty.append(
            "Warning state is unknown — the official feed could not be "
            "confirmed, so this is not an all-clear.")
    brief.uncertainty.extend(bundle.warning_caveats)

    for source_id in bundle.unknown_freshness:
        brief.uncertainty.append(
            f"{source_id} does not publish a model run time, so how current "
            f"this data is cannot be confirmed.")
    for error in bundle.source_errors:
        brief.uncertainty.append(f"{error.source_id}: {error.reason}")
    for finding in bundle.sources_skipped:
        brief.uncertainty.append(f"{finding.source_id}: {finding.reason}")

    brief.headline = brief.forecast_summary[0] if brief.forecast_summary else \
        "No requested fields were covered."
    return brief


def _note_present_versus_future(bundle: WeatherBundle,
                                brief: WeatherBrief) -> None:
    """Say plainly when the reading describes now rather than later (WF09).

    An observation and a forecast read identically once rendered — "12 C" says
    nothing about whether it is a measurement or an expectation. When the only
    thing covering a field is an observation, the requested window is *not*
    covered, and presenting it without saying so answers "will it rain later?"
    with what the sky is doing at this moment.
    """
    products = set(bundle.coverage_by_product)
    if ProductType.OBSERVATION.value not in products:
        return

    brief.uncertainty.append(
        "These are present conditions, measured now.")
    if ProductType.FORECAST.value not in products:
        brief.uncertainty.append(
            "No forecast covered the requested window, so nothing here "
            "describes later — this is not a quiet outlook.")


def _add_preparation(bundle: WeatherBundle, brief: WeatherBrief) -> None:
    """Advice from source-backed fields only (weather §5).

    When a qualified comparator crosses an action threshold and the primary does
    not, the advice is conditional and attributed. The alternative — nudging the
    primary probability up so the advice looks consistent — states a number no
    source issued (WF17).
    """
    rain = bundle.comparison_for("precipitation_probability")
    if rain is not None:
        if rain.primary_value is not None and rain.primary_value >= RAIN_ACTION_PROBABILITY:
            brief.preparation.append(
                f"Rain is likely ({rain.primary_value:g}% per "
                f"{rain.primary_source_id}); take a waterproof layer.")
        else:
            crossing = crosses_threshold_only_in_comparator(
                rain, action_threshold=RAIN_ACTION_PROBABILITY)
            if crossing:
                values = ", ".join(
                    f"{s} {rain.values_by_source[s]:g}%" for s in crossing)
                brief.preparation.append(
                    f"{rain.primary_source_id} is drier, but {values} — take a "
                    f"waterproof layer if you would rather not risk it.")
                brief.uncertainty.append(
                    "Sources disagree about rain; the primary value is "
                    "unchanged and both are shown.")

    wind = bundle.comparison_for("wind_gust") or bundle.comparison_for("wind_speed")
    if wind is not None and wind.primary_value is not None \
            and wind.primary_value >= WIND_ACTION_KPH:
        brief.preparation.append(
            f"Gusts around {wind.primary_value:g} km/h ({wind.primary_source_id}).")

    temperature = bundle.comparison_for("temperature")
    if temperature is not None and temperature.primary_value is not None \
            and temperature.primary_value <= COLD_ACTION_C:
        brief.preparation.append(
            f"Cold at {temperature.primary_value:g}°C "
            f"({temperature.primary_source_id}); dress warmly.")
