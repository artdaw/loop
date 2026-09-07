"""Open-Meteo adapters (weather §3).

Open-Meteo serves several national models through one API shape, which makes it
a real multi-source comparator without a credential: `dwd-icon` is DWD's ICON,
`ecmwf` is ECMWF's IFS, `gfs` is NOAA's GFS. Those are genuinely different
model families, so comparing them is comparing evidence rather than comparing a
model with a copy of itself.

The lineage matters more than the hostname. All three arrive from the same
transport, so `transport_provider` is Open-Meteo for all of them while
`publisher` and `lineage_ids` stay distinct — that is exactly the distinction
WF03 turns on, and it is why a "DWD vs Open-Meteo" comparison is not two
sources.

**Issue time is not reported by this API.** The forecast payload carries no
model run timestamp, so `issued_at` stays `None` and freshness is *unknown*
rather than being backfilled from the fetch time. Backfilling would make every
response look freshly issued, which is the WF08 failure exactly.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from loop.capabilities.weather.adapters.base import (
    AdapterError,
    HttpTransport,
)
from loop.capabilities.weather.normalize import Statistic, WeatherSample, normalize
from loop.capabilities.weather.sources import (
    Authority,
    Coverage,
    ProductType,
    SourceDescriptor,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.open-meteo.com/v1"

#: Canonical variable -> (Open-Meteo hourly field, statistic).
VARIABLE_FIELDS = {
    "temperature": ("temperature_2m", Statistic.INSTANT),
    "feels_like": ("apparent_temperature", Statistic.INSTANT),
    "wind_speed": ("wind_speed_10m", Statistic.INSTANT),
    "wind_gust": ("wind_gusts_10m", Statistic.INSTANT),
    "precipitation_probability": ("precipitation_probability",
                                  Statistic.PROBABILITY),
    "precipitation_amount": ("precipitation", Statistic.SUM),
}

#: Units Open-Meteo returns by default, mapped to the names our converters know.
UNIT_ALIASES = {"°C": "C", "°F": "F", "km/h": "km/h", "m/s": "m/s",
                "mm": "mm", "inch": "in", "%": "%"}


@dataclass(frozen=True)
class OpenMeteoModel:
    """One model served through the Open-Meteo API."""

    source_id: str
    endpoint: str
    publisher: str
    model_family: str
    lineage_id: str
    authority: Authority
    horizon_hours: int
    resolution_km: float
    #: How often the publisher issues a new run, per their documentation.
    update_interval_seconds: int
    attribution: str


#: The three model families worth comparing. Each is a distinct lineage.
OPEN_METEO_MODELS = (
    OpenMeteoModel(
        source_id="open_meteo_dwd_icon", endpoint="dwd-icon", publisher="DWD",
        model_family="ICON", lineage_id="icon", authority=Authority.OFFICIAL,
        horizon_hours=180, resolution_km=2.2, update_interval_seconds=3 * 3600,
        attribution="Deutscher Wetterdienst (ICON) via Open-Meteo"),
    OpenMeteoModel(
        source_id="open_meteo_ecmwf", endpoint="ecmwf", publisher="ECMWF",
        model_family="IFS", lineage_id="ecmwf-ifs",
        authority=Authority.QUALIFIED_OTHER, horizon_hours=240,
        resolution_km=25.0, update_interval_seconds=6 * 3600,
        attribution="ECMWF IFS via Open-Meteo"),
    OpenMeteoModel(
        source_id="open_meteo_gfs", endpoint="gfs", publisher="NOAA",
        model_family="GFS", lineage_id="gfs",
        authority=Authority.QUALIFIED_OTHER, horizon_hours=384,
        resolution_km=11.0, update_interval_seconds=6 * 3600,
        attribution="NOAA GFS via Open-Meteo"),
)


def _descriptor(model: OpenMeteoModel) -> SourceDescriptor:
    return SourceDescriptor(
        id=model.source_id,
        publisher=model.publisher,
        transport_provider="Open-Meteo",
        product_type=ProductType.FORECAST,
        authority=model.authority,
        product=f"{model.model_family} hourly forecast",
        coverage=Coverage(jurisdiction="", bbox=None,
                          spatial_resolution_km=model.resolution_km),
        supported_variables=frozenset(VARIABLE_FIELDS),
        supported_horizon_hours=model.horizon_hours,
        model_family=model.model_family,
        model_run_id="",
        lineage_ids=(model.lineage_id,),
        update_interval_seconds=model.update_interval_seconds,
        # Two run intervals: a run one interval old is normal, two is late.
        # Never the universal one-hour rule the spec warns against.
        max_issue_age_seconds=model.update_interval_seconds * 2,
        attribution=model.attribution,
    )


def open_meteo_descriptors() -> list[SourceDescriptor]:
    """Descriptors for every configured Open-Meteo model."""
    return [_descriptor(model) for model in OPEN_METEO_MODELS]


class OpenMeteoAdapter:
    """Fetches and normalizes one Open-Meteo model."""

    def __init__(self, model: OpenMeteoModel, *,
                 transport: HttpTransport | None = None,
                 base_url: str = BASE_URL) -> None:
        self.model = model
        self._transport = transport or HttpTransport()
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> SourceDescriptor:
        return _descriptor(self.model)

    # ------------------------------------------------------------------ #
    # Fetch
    # ------------------------------------------------------------------ #
    def fetch(self, *, latitude: float, longitude: float, start: int, end: int,
              variables: list[str], now: int) -> list[WeatherSample]:
        """Fetch one model's hourly forecast for a window."""
        requested = [name for name in variables if name in VARIABLE_FIELDS]
        if not requested:
            return []

        fields = sorted({VARIABLE_FIELDS[name][0] for name in requested})
        params = {
            "latitude": f"{latitude:.4f}",
            "longitude": f"{longitude:.4f}",
            "hourly": ",".join(fields),
            # UTC throughout; local presentation happens far from here, and a
            # provider-localised timestamp is one more thing to get wrong.
            "timezone": "UTC",
            "start_hour": _hour_stamp(start),
            "end_hour": _hour_stamp(end),
        }

        response = self._transport.get(f"{self._base_url}/{self.model.endpoint}",
                                       params=params)
        if response.status_code != 200:
            raise AdapterError(
                self.model.source_id,
                f"HTTP {response.status_code} from Open-Meteo")

        try:
            payload = response.json()
        except Exception as exc:                       # noqa: BLE001 — reported
            raise AdapterError(self.model.source_id,
                               f"unreadable response: {exc}") from exc

        return self.parse(payload, requested=requested, now=now,
                          latitude=latitude, longitude=longitude)

    # ------------------------------------------------------------------ #
    # Parse — pure, and tested against recorded payloads
    # ------------------------------------------------------------------ #
    def parse(self, payload: Any, *, requested: list[str], now: int,
              latitude: float, longitude: float) -> list[WeatherSample]:
        """Turn one Open-Meteo response into normalized samples."""
        if not isinstance(payload, dict):
            raise AdapterError(self.model.source_id,
                               "response was not a JSON object")
        if "error" in payload:
            raise AdapterError(self.model.source_id,
                               str(payload.get("reason", "provider error")))

        hourly = payload.get("hourly")
        units = payload.get("hourly_units") or {}
        if not isinstance(hourly, dict) or "time" not in hourly:
            raise AdapterError(self.model.source_id,
                               "response contained no hourly series")

        stamps = hourly["time"]
        if not isinstance(stamps, list):
            raise AdapterError(self.model.source_id, "hourly.time was not a list")

        elevation = payload.get("elevation")
        samples: list[WeatherSample] = []

        for variable in requested:
            field_name, statistic = VARIABLE_FIELDS[variable]
            series = hourly.get(field_name)
            if not isinstance(series, list):
                # The model does not supply this field. Missing, not zero.
                continue

            unit = UNIT_ALIASES.get(str(units.get(field_name, "")).strip(),
                                    str(units.get(field_name, "")).strip())

            for index, stamp in enumerate(stamps):
                if index >= len(series):
                    break
                value = series[index]
                valid_from = _parse_utc(stamp)
                if valid_from is None:
                    continue

                sample = WeatherSample(
                    source_id=self.model.source_id,
                    variable=variable,
                    value=None if value is None else float(value),
                    unit=unit or _default_unit(variable),
                    statistic=statistic,
                    valid_from=valid_from,
                    # Open-Meteo stamps an hourly value with the START of its
                    # hour; a precipitation sum covers the hour that follows.
                    valid_to=valid_from + 3600,
                    fetched_at=now,
                    product_type="forecast",
                    # No model run time is published on this endpoint, so the
                    # honest value is None: freshness is unknown, not fresh.
                    issued_at=None,
                    interval_seconds=3600,
                    elevation_m=float(elevation) if elevation is not None else None,
                    lineage_ids=(self.model.lineage_id,),
                    model_run_id="",
                    missing_reason="" if value is not None
                    else "not supplied by this model",
                )
                samples.append(normalize(sample))

        return samples


def _default_unit(variable: str) -> str:
    return {"temperature": "C", "feels_like": "C", "wind_speed": "km/h",
            "wind_gust": "km/h", "precipitation_amount": "mm",
            "precipitation_probability": "%"}[variable]


def _hour_stamp(epoch_seconds: int) -> str:
    """Open-Meteo's `start_hour`/`end_hour` format: `YYYY-MM-DDTHH:MM`."""
    moment = dt.datetime.fromtimestamp(epoch_seconds, tz=dt.UTC)
    return moment.strftime("%Y-%m-%dT%H:00")


def _parse_utc(stamp: Any) -> int | None:
    """Parse an Open-Meteo timestamp, which is naive UTC when timezone=UTC."""
    if not isinstance(stamp, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return int(parsed.timestamp())


def build_adapters(*, transport: HttpTransport | None = None,
                   model_ids: list[str] | None = None,
                   include_current: bool = True) -> list[Any]:
    """Construct adapters for the configured models.

    `include_current` adds the observation adapter, so a bundle can say what
    the weather *is* and not only what a model expects it to be (WF09).
    """
    shared = transport or HttpTransport()
    wanted = set(model_ids) if model_ids else None
    adapters: list[Any] = [
        OpenMeteoAdapter(model, transport=shared)
        for model in OPEN_METEO_MODELS
        if wanted is None or model.source_id in wanted]
    if include_current and (wanted is None or "open_meteo_current" in wanted):
        adapters.append(OpenMeteoCurrentAdapter(transport=shared))
    return adapters


#: Open-Meteo's `current` block: present conditions, not a forecast. Distinct
#: fields from the hourly ones, because "what it is doing now" and "what the
#: model expects at 15:00" are different measurements with different meanings
#: (WF09) — and only one of them can answer "is it raining?".
CURRENT_FIELDS = {
    "temperature": ("temperature_2m", Statistic.INSTANT),
    "feels_like": ("apparent_temperature", Statistic.INSTANT),
    "wind_speed": ("wind_speed_10m", Statistic.INSTANT),
    "wind_gust": ("wind_gusts_10m", Statistic.INSTANT),
    "precipitation_amount": ("precipitation", Statistic.SUM),
}

#: An observation describes now. Ten minutes old is current; an hour is not.
CURRENT_MAX_AGE_SECONDS = 600


def current_descriptor() -> SourceDescriptor:
    return SourceDescriptor(
        id="open_meteo_current", publisher="Open-Meteo",
        transport_provider="Open-Meteo",
        # An OBSERVATION, so `covers_horizon` reports it cannot answer for the
        # future at all rather than being treated as a very short forecast.
        product_type=ProductType.OBSERVATION,
        authority=Authority.QUALIFIED_OTHER,
        product="current conditions",
        coverage=Coverage(jurisdiction="", bbox=None,
                          spatial_resolution_km=11.0),
        supported_variables=frozenset(CURRENT_FIELDS),
        # Documentation rather than behaviour: `covers_horizon`
        # decides from the *product type* for observations, so this
        # number is never consulted. Stated as zero anyway, because
        # a reader should not have to check which one wins.
        supported_horizon_hours=0,
        model_family="", model_run_id="", lineage_ids=("open-meteo-current",),
        update_interval_seconds=900,
        max_issue_age_seconds=CURRENT_MAX_AGE_SECONDS,
        attribution="Open-Meteo current conditions")


class OpenMeteoCurrentAdapter:
    """Present conditions from Open-Meteo's `current` block.

    Deliberately a separate adapter rather than an extra field on the forecast
    one. Its product type, freshness rule and horizon are all different, and
    merging them would let a ten-minute-old reading and a three-hour-old model
    run be compared as though they meant the same thing.
    """

    def __init__(self, *, transport: HttpTransport | None = None,
                 base_url: str = BASE_URL) -> None:
        self._transport = transport or HttpTransport()
        self._base_url = base_url

    @property
    def descriptor(self) -> SourceDescriptor:
        return current_descriptor()

    def fetch(self, *, latitude: float, longitude: float, start: int, end: int,
              variables: list[str], now: int) -> list[WeatherSample]:
        del start, end          # an observation has no window but "now"
        requested = [name for name in variables if name in CURRENT_FIELDS]
        if not requested:
            return []

        fields = sorted({CURRENT_FIELDS[name][0] for name in requested})
        response = self._transport.get(f"{self._base_url}/forecast", params={
            "latitude": f"{latitude:.4f}", "longitude": f"{longitude:.4f}",
            "current": ",".join(fields), "timezone": "UTC"})
        if response.status_code != 200:
            raise AdapterError("open_meteo_current",
                               f"HTTP {response.status_code} from Open-Meteo")
        try:
            payload = response.json()
        except Exception as exc:                       # noqa: BLE001 — reported
            raise AdapterError("open_meteo_current",
                               f"unreadable response: {exc}") from exc
        return self.parse(payload, requested=requested, now=now)

    def parse(self, payload: Any, *, requested: list[str],
              now: int) -> list[WeatherSample]:
        if not isinstance(payload, dict):
            raise AdapterError("open_meteo_current",
                               "response was not a JSON object")
        current = payload.get("current")
        units = payload.get("current_units") or {}
        if not isinstance(current, dict):
            raise AdapterError("open_meteo_current",
                               "response contained no current conditions")

        observed = _parse_utc(current.get("time"))
        samples: list[WeatherSample] = []
        for variable in requested:
            field_name, statistic = CURRENT_FIELDS[variable]
            value = current.get(field_name)
            unit = UNIT_ALIASES.get(str(units.get(field_name, "")),
                                    _default_unit(variable))
            samples.append(WeatherSample(
                source_id="open_meteo_current", variable=variable,
                value=None if value is None else float(value),
                unit=unit, statistic=statistic,
                # An observation's window is the moment it describes, not a
                # span: giving it an hour would let it answer for a future it
                # says nothing about.
                valid_from=observed or now, valid_to=observed or now,
                fetched_at=now, product_type=ProductType.OBSERVATION.value,
                observed_at=observed, interval_seconds=0,
                lineage_ids=("open-meteo-current",),
                missing_reason="" if value is not None else "not reported",
                original_unit=str(units.get(field_name, ""))))
        return samples
