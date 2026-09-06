"""Source descriptors, the catalogue and per-field selection (weather §3).

Locality is a *preference*, not a trump card. Three conditions gate it, and the
spec is explicit that all three must hold before local wins: freshness, product
suitability, and actual coverage of the requested point. A stale local forecast
does not outrank a fresh suitable fallback (WF02), and a valley station two
kilometres from a mountain destination is not representative of it (WF10).

Selection runs per product/variable/time window rather than once per call,
because a source can be the right answer for temperature and the wrong one for
warnings. Adding an installed source is a policy edit — the catalogue resolves
IDs, so no planner, bot or scheduler code changes (WF22).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class Authority(str, Enum):
    OFFICIAL = "official"
    QUALIFIED_OTHER = "qualified_other"
    UNKNOWN = "unknown"


class ProductType(str, Enum):
    OBSERVATION = "observation"
    RADAR = "radar"
    NOWCAST = "nowcast"
    FORECAST = "forecast"
    WARNING = "warning"


#: Selection tiers (weather §3). Lower sorts first.
TIER_WARNING = 1
TIER_NEARTERM_LOCAL = 2
TIER_OFFICIAL_FORECAST = 3
TIER_OTHER_FORECAST = 4
TIER_GLOBAL_FALLBACK = 5

#: Transport-cache ceilings in seconds, by product type (weather §4).
CACHE_TTL_SECONDS = {
    ProductType.FORECAST: 3600,
    ProductType.OBSERVATION: 600,
    ProductType.NOWCAST: 600,
    ProductType.RADAR: 600,
    ProductType.WARNING: 300,
}

MAX_FORECAST_PRODUCTS = 3
MAX_PROVIDER_REQUESTS = 6


@dataclass
class Coverage:
    """Where and to what resolution a product actually applies."""

    jurisdiction: str = ""
    #: Bounding box as (min_lat, min_lon, max_lat, max_lon); empty means global.
    bbox: tuple[float, float, float, float] | None = None
    spatial_resolution_km: float | None = None
    elevation_range_m: tuple[float, float] | None = None

    def contains(self, latitude: float, longitude: float) -> bool:
        if self.bbox is None:
            return True
        min_lat, min_lon, max_lat, max_lon = self.bbox
        return min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon

    def suits_elevation(self, elevation_m: float | None) -> bool:
        """A product with a declared elevation band does not cover outside it.

        Proximity is not suitability: a station in the valley may be the nearest
        thing to a mountain destination and still be the wrong answer (WF10).
        """
        if self.elevation_range_m is None or elevation_m is None:
            return True
        low, high = self.elevation_range_m
        return low <= elevation_m <= high


@dataclass
class SourceDescriptor:
    """Validated adapter metadata. Never accepted from model prose (weather §3)."""

    id: str
    publisher: str
    product_type: ProductType
    authority: Authority = Authority.UNKNOWN
    transport_provider: str = ""
    product: str = ""
    coverage: Coverage = field(default_factory=Coverage)
    supported_variables: frozenset[str] = frozenset()
    supported_horizon_hours: int = 0
    model_family: str = ""
    model_run_id: str = ""
    #: Upstream products this one derives from — the basis for evidence families.
    lineage_ids: tuple[str, ...] = ()
    update_interval_seconds: int | None = None
    #: Declared from the publisher's documented cadence, never a universal hour.
    max_issue_age_seconds: int = 3600
    attribution: str = ""
    installed: bool = True

    @property
    def transport(self) -> str:
        """Who served the bytes, which may differ from who made the forecast."""
        return self.transport_provider or self.publisher

    @property
    def is_local_authority(self) -> bool:
        return self.authority is Authority.OFFICIAL and bool(
            self.coverage.jurisdiction)

    def supports(self, variable: str) -> bool:
        return not self.supported_variables or variable in self.supported_variables

    def covers_horizon(self, hours: float) -> bool:
        """Forecast horizons are hard limits; beyond them there is no coverage.

        An observation product has no horizon at all — it describes now (WF09).
        """
        if self.product_type in (ProductType.OBSERVATION, ProductType.RADAR):
            return hours <= 0
        return hours <= self.supported_horizon_hours

    def tier(self) -> int:
        if self.product_type is ProductType.WARNING:
            return TIER_WARNING
        if self.product_type in (ProductType.OBSERVATION, ProductType.RADAR,
                                 ProductType.NOWCAST):
            return TIER_NEARTERM_LOCAL
        if self.authority is Authority.OFFICIAL:
            return TIER_OFFICIAL_FORECAST
        if self.authority is Authority.QUALIFIED_OTHER:
            return TIER_OTHER_FORECAST
        return TIER_GLOBAL_FALLBACK


@dataclass
class SelectionFinding:
    """Why a source was passed over — user-visible, never silent."""

    source_id: str
    reason: str


class SourceCatalogue:
    """The registry of validated source descriptors.

    Listing an ID in policy does not install it: an unresolvable ID is a
    validation finding, not a silently missing source (WF22).
    """

    def __init__(self, descriptors: list[SourceDescriptor] | None = None) -> None:
        self._by_id: dict[str, SourceDescriptor] = {}
        for descriptor in descriptors or []:
            self.register(descriptor)

    def register(self, descriptor: SourceDescriptor) -> SourceDescriptor:
        self._by_id[descriptor.id] = descriptor
        return descriptor

    def get(self, source_id: str) -> SourceDescriptor | None:
        return self._by_id.get(source_id)

    def all(self) -> list[SourceDescriptor]:
        return sorted(self._by_id.values(), key=lambda d: d.id)

    def resolve(self, source_ids: list[str]) -> tuple[list[SourceDescriptor],
                                                      list[SelectionFinding]]:
        """Resolve policy-configured IDs, reporting the ones that do not exist."""
        resolved: list[SourceDescriptor] = []
        findings: list[SelectionFinding] = []
        for source_id in source_ids:
            descriptor = self._by_id.get(source_id)
            if descriptor is None:
                findings.append(SelectionFinding(
                    source_id, "no adapter installed for this source id"))
            elif not descriptor.installed:
                findings.append(SelectionFinding(
                    source_id, "source is catalogued but its adapter is not installed"))
            else:
                resolved.append(descriptor)
        return resolved, findings

    def describe(self, location: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """weather.sources — local metadata only, no network and no model."""
        out = []
        for descriptor in self.all():
            entry: dict[str, Any] = {
                "id": descriptor.id,
                "publisher": descriptor.publisher,
                "transport_provider": descriptor.transport,
                "product_type": descriptor.product_type.value,
                "authority": descriptor.authority.value,
                "jurisdiction": descriptor.coverage.jurisdiction,
                "lineage_ids": list(descriptor.lineage_ids),
                "attribution": descriptor.attribution,
                "installed": descriptor.installed,
            }
            if location:
                entry["covers_location"] = descriptor.coverage.contains(
                    float(location["latitude"]), float(location["longitude"]))
            out.append(entry)
        return out


def select_sources(catalogue: SourceCatalogue, *, latitude: float, longitude: float,
                   variable: str, horizon_hours: float,
                   elevation_m: float | None = None,
                   preferred_ids: list[str] | None = None,
                   fresh_ids: set[str] | None = None,
                   max_products: int = MAX_FORECAST_PRODUCTS,
                   ) -> tuple[list[SourceDescriptor], list[SelectionFinding]]:
    """Rank suitable sources for one variable and window (weather §3).

    ``fresh_ids``, when given, is the set of sources currently fresh enough to
    use. A source outside it is excluded rather than demoted: preferring a stale
    local forecast over a fresh fallback is exactly the failure WF02 describes.
    """
    findings: list[SelectionFinding] = []
    eligible: list[SourceDescriptor] = []

    for descriptor in catalogue.all():
        if not descriptor.installed:
            continue
        if not descriptor.coverage.contains(latitude, longitude):
            findings.append(SelectionFinding(
                descriptor.id, "does not cover the requested location"))
            continue
        if not descriptor.coverage.suits_elevation(elevation_m):
            findings.append(SelectionFinding(
                descriptor.id,
                "declared elevation range does not include the requested point"))
            continue
        if not descriptor.supports(variable):
            findings.append(SelectionFinding(
                descriptor.id, f"does not supply {variable}"))
            continue
        if not descriptor.covers_horizon(horizon_hours):
            findings.append(SelectionFinding(
                descriptor.id,
                f"horizon {descriptor.supported_horizon_hours}h does not reach "
                f"{horizon_hours:g}h"))
            continue
        if fresh_ids is not None and descriptor.id not in fresh_ids:
            findings.append(SelectionFinding(
                descriptor.id, "latest data is older than its declared max issue age"))
            continue
        eligible.append(descriptor)

    priority = {source_id: index
                for index, source_id in enumerate(preferred_ids or [])}

    def rank(descriptor: SourceDescriptor) -> tuple:
        # Spec order within a tier: coverage/terrain fit, then explicit approved
        # user priority, then source ID. Fit outranks preference because a
        # preferred source that resolves the location badly is still the wrong
        # answer — preference decides between sources that both fit.
        return (
            descriptor.tier(),
            descriptor.coverage.spatial_resolution_km
            if descriptor.coverage.spatial_resolution_km is not None else 1e9,
            priority.get(descriptor.id, len(priority)),
            descriptor.id,
        )

    eligible.sort(key=rank)

    forecasts = [d for d in eligible if d.product_type is ProductType.FORECAST]
    if len(forecasts) > max_products:
        for dropped in forecasts[max_products:]:
            findings.append(SelectionFinding(
                dropped.id, f"beyond the {max_products}-product comparison limit"))
        keep = {d.id for d in forecasts[:max_products]}
        eligible = [d for d in eligible
                    if d.product_type is not ProductType.FORECAST or d.id in keep]

    return eligible, findings
