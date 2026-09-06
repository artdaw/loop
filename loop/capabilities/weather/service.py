"""The working weather.forecast path (weather §2).

Selection, fetching, normalization, comparison and rendering, wired together.
Everything below the adapters is deterministic; the adapters are the only part
that touches a network, and they are injected so the whole path is testable
without one.

The budget is enforced here rather than inside an adapter, because an adapter
that polices its own call count cannot see the other five.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from loop.capabilities.weather.adapters.base import AdapterError, HttpTransport
from loop.capabilities.weather.adapters.open_meteo import build_adapters
from loop.capabilities.weather.bundle import (
    FetchBudget,
    SourceError,
    WeatherBrief,
    WeatherBundle,
    WeatherRequest,
    build_bundle,
    render_brief,
)
from loop.capabilities.weather.sources import (
    MAX_FORECAST_PRODUCTS,
    MAX_PROVIDER_REQUESTS,
    SourceCatalogue,
    select_sources,
)
from loop.capabilities.weather.warnings import FeedRead
from loop.core.ids import new_id
from loop.core.privacy import PrivacyLabel

logger = logging.getLogger(__name__)


class WeatherService:
    """weather.forecast, weather.prepare and weather.sources."""

    def __init__(self, *, adapters: list[Any] | None = None,
                 warning_feeds: list[Any] | None = None,
                 transport: HttpTransport | None = None,
                 locations: dict[str, dict[str, Any]] | None = None,
                 preferred_ids: list[str] | None = None,
                 policy_revision: str = "") -> None:
        self.adapters = list(
            adapters if adapters is not None
            else build_adapters(transport=transport))
        self.warning_feeds = list(warning_feeds or [])
        self.locations = dict(locations or {})
        self.preferred_ids = list(preferred_ids or [])
        self.policy_revision = policy_revision
        self.catalogue = SourceCatalogue(
            [adapter.descriptor for adapter in self.adapters])
        self._by_id = {adapter.descriptor.id: adapter for adapter in self.adapters}
        self._cache: dict[str, WeatherBundle] = {}

    # ------------------------------------------------------------------ #
    # weather.sources
    # ------------------------------------------------------------------ #
    def sources(self, location_ref: str = "") -> list[dict[str, Any]]:
        """Local metadata only — no network, no model (weather §2)."""
        location = self.locations.get(location_ref) if location_ref else None
        return self.catalogue.describe(location=location)

    # ------------------------------------------------------------------ #
    # weather.forecast
    # ------------------------------------------------------------------ #
    def forecast(self, request: WeatherRequest, *, now: int | None = None,
                 privacy: PrivacyLabel | None = None,
                 max_requests: int = MAX_PROVIDER_REQUESTS,
                 use_cache: bool = True) -> WeatherBundle:
        """Resolve, fetch, reconcile and persist one bundle."""
        moment = now if now is not None else int(time.time())
        location = request.resolved_location(self.locations)
        latitude = float(location["latitude"])
        longitude = float(location["longitude"])
        start, end = request.window(now=moment)
        horizon_hours = max(0.0, (end - moment) / 3600.0)

        if use_cache:
            cached = self._cached_for(request, now=moment)
            if cached is not None:
                return cached

        selected, skipped = select_sources(
            self.catalogue, latitude=latitude, longitude=longitude,
            variable=request.fields[0], horizon_hours=horizon_hours,
            elevation_m=location.get("elevation_m"),
            preferred_ids=self.preferred_ids,
            max_products=MAX_FORECAST_PRODUCTS)

        budget = FetchBudget(max_requests=max_requests)
        samples = []
        errors: list[SourceError] = []

        for descriptor in selected:
            adapter = self._by_id.get(descriptor.id)
            if adapter is None:
                errors.append(SourceError(descriptor.id,
                                          "no adapter is installed", attempted=False))
                continue
            if not budget.take():
                errors.append(SourceError(
                    descriptor.id, "not checked: provider request budget reached",
                    attempted=False))
                continue
            try:
                samples.extend(adapter.fetch(
                    latitude=latitude, longitude=longitude, start=start, end=end,
                    variables=list(request.fields), now=moment))
            except AdapterError as exc:
                # A failing provider costs its request and is named. The others
                # are kept: an outage is not a forecast of calm weather.
                logger.warning("weather source %s failed: %s", exc.source_id,
                               exc.message)
                errors.append(SourceError(descriptor.id, exc.message))

        warning_reads = self._read_warnings(latitude=latitude,
                                            longitude=longitude, now=moment,
                                            budget=budget, errors=errors)

        bundle = build_bundle(
            request, bundle_id=new_id(), now=moment, samples=samples,
            catalogue=self.catalogue, preferred_ids=self.preferred_ids,
            source_errors=errors, skipped=skipped, warning_reads=warning_reads,
            location=location, policy_revision=self.policy_revision,
            privacy=privacy or PrivacyLabel())

        self._cache[self._cache_key(request)] = bundle
        return bundle

    # ------------------------------------------------------------------ #
    # weather.prepare
    # ------------------------------------------------------------------ #
    def prepare(self, request: WeatherRequest, *, now: int | None = None,
                bundle: WeatherBundle | None = None,
                privacy: PrivacyLabel | None = None) -> WeatherBrief:
        """Render advice, reusing a still-valid bundle when one matches."""
        moment = now if now is not None else int(time.time())
        usable = bundle
        if usable is not None and not usable.matches(
                request, now=moment, policy_revision=self.policy_revision):
            usable = None
        if usable is None:
            usable = self.forecast(request, now=moment, privacy=privacy)
        return render_brief(usable)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _read_warnings(self, *, latitude: float, longitude: float, now: int,
                       budget: FetchBudget,
                       errors: list[SourceError]) -> list[FeedRead] | None:
        """Read configured warning feeds, or report that none was checked.

        Returning ``None`` rather than an empty list matters: an empty list of
        *reads* would let `warning_state` see a checked-and-empty world, while
        `None` says the channel was never consulted (WF12).
        """
        if not self.warning_feeds:
            return None

        reads: list[FeedRead] = []
        for feed in self.warning_feeds:
            if not budget.take():
                errors.append(SourceError(
                    getattr(feed, "publisher", "warnings"),
                    "not checked: provider request budget reached",
                    attempted=False))
                continue
            try:
                reads.append(feed.read(latitude=latitude, longitude=longitude,
                                       now=now))
            except AdapterError as exc:
                reads.append(FeedRead(publisher=exc.source_id, ok=False,
                                      fetched_at=now, error=exc.message))
        return reads

    def _cache_key(self, request: WeatherRequest) -> str:
        return "|".join([
            request.location_ref or f"{request.latitude},{request.longitude}",
            ",".join(sorted(request.fields)),
            str(request.window_start), str(request.window_end),
            str(request.horizon_hours),
        ])

    def _cached_for(self, request: WeatherRequest, *,
                    now: int) -> WeatherBundle | None:
        cached = self._cache.get(self._cache_key(request))
        if cached is None:
            return None
        if not cached.matches(request, now=now,
                              policy_revision=self.policy_revision):
            return None
        return cached
