"""Synthetic weather sources. No network, no credentials, no live provider.

The lineage here is the point: `open_meteo_dwd` re-serves the same DWD ICON run
as `dwd`, so the two are one evidence family however different their hostnames
look. `meteoswiss` is genuinely independent.
"""

from __future__ import annotations

from loop.capabilities.weather.normalize import Statistic, WeatherSample
from loop.capabilities.weather.sources import (
    Authority,
    Coverage,
    ProductType,
    SourceCatalogue,
    SourceDescriptor,
)

HOUR = 3600
NOW = 1_780_000_000
BERLIN = {"latitude": 52.52, "longitude": 13.405, "timezone": "Europe/Berlin",
          "elevation_m": 34.0}
ZERMATT = {"latitude": 46.02, "longitude": 7.75, "timezone": "Europe/Zurich",
           "elevation_m": 1608.0}

DE_BOX = (47.0, 5.5, 55.2, 15.5)
CH_BOX = (45.8, 5.9, 47.9, 10.5)
GLOBAL_BOX = None

FORECAST_FIELDS = frozenset({"temperature", "feels_like", "wind_speed",
                             "wind_gust", "precipitation_probability",
                             "precipitation_amount"})


def dwd() -> SourceDescriptor:
    return SourceDescriptor(
        id="dwd", publisher="DWD", product_type=ProductType.FORECAST,
        authority=Authority.OFFICIAL, product="MOSMIX",
        coverage=Coverage(jurisdiction="DE", bbox=DE_BOX,
                          spatial_resolution_km=2.2),
        supported_variables=FORECAST_FIELDS, supported_horizon_hours=240,
        model_family="ICON", model_run_id="2026090512",
        lineage_ids=("icon-d2",), max_issue_age_seconds=6 * HOUR,
        attribution="Deutscher Wetterdienst")


def open_meteo_dwd() -> SourceDescriptor:
    """A different transport for the *same* DWD ICON product (WF03)."""
    return SourceDescriptor(
        id="open_meteo_dwd", publisher="DWD",
        transport_provider="Open-Meteo", product_type=ProductType.FORECAST,
        authority=Authority.QUALIFIED_OTHER, product="ICON via Open-Meteo",
        coverage=Coverage(jurisdiction="DE", bbox=DE_BOX,
                          spatial_resolution_km=11.0),
        supported_variables=FORECAST_FIELDS, supported_horizon_hours=168,
        model_family="ICON", model_run_id="2026090512",
        lineage_ids=("icon-d2",), max_issue_age_seconds=6 * HOUR,
        attribution="Open-Meteo / DWD ICON")


def meteoswiss() -> SourceDescriptor:
    return SourceDescriptor(
        id="meteoswiss", publisher="MeteoSwiss", product_type=ProductType.FORECAST,
        authority=Authority.OFFICIAL, product="local forecast",
        coverage=Coverage(jurisdiction="CH", bbox=CH_BOX,
                          spatial_resolution_km=1.0,
                          elevation_range_m=(200.0, 4000.0)),
        supported_variables=FORECAST_FIELDS, supported_horizon_hours=120,
        model_family="ICON-CH", model_run_id="2026090512",
        lineage_ids=("icon-ch1",), max_issue_age_seconds=3 * HOUR,
        attribution="MeteoSwiss")


def global_model() -> SourceDescriptor:
    return SourceDescriptor(
        id="global_gfs", publisher="NOAA", transport_provider="Open-Meteo",
        product_type=ProductType.FORECAST, authority=Authority.UNKNOWN,
        coverage=Coverage(jurisdiction="", bbox=GLOBAL_BOX,
                          spatial_resolution_km=25.0),
        supported_variables=FORECAST_FIELDS, supported_horizon_hours=384,
        model_family="GFS", model_run_id="2026090512",
        lineage_ids=("gfs",), max_issue_age_seconds=6 * HOUR,
        attribution="NOAA GFS")


def valley_station() -> SourceDescriptor:
    """Physically near a mountain point, meteorologically not it (WF10)."""
    return SourceDescriptor(
        id="valley_station", publisher="MeteoSwiss",
        product_type=ProductType.OBSERVATION, authority=Authority.OFFICIAL,
        coverage=Coverage(jurisdiction="CH", bbox=CH_BOX,
                          spatial_resolution_km=0.1,
                          elevation_range_m=(400.0, 900.0)),
        supported_variables=frozenset({"temperature", "wind_speed"}),
        supported_horizon_hours=0, max_issue_age_seconds=HOUR,
        attribution="MeteoSwiss station")


def nowcast() -> SourceDescriptor:
    return SourceDescriptor(
        id="dwd_nowcast", publisher="DWD", product_type=ProductType.NOWCAST,
        authority=Authority.OFFICIAL,
        coverage=Coverage(jurisdiction="DE", bbox=DE_BOX),
        supported_variables=frozenset({"precipitation_probability"}),
        supported_horizon_hours=2, max_issue_age_seconds=900,
        attribution="DWD radar nowcast")


def catalogue(*descriptors: SourceDescriptor) -> SourceCatalogue:
    return SourceCatalogue(list(descriptors) or
                           [dwd(), open_meteo_dwd(), meteoswiss(), global_model()])


def sample(source_id: str, variable: str, value: float | None, *,
           unit: str = "", statistic: Statistic = Statistic.INSTANT,
           now: int = NOW, issued_offset: int = -1800,
           valid_from_offset: int = 0, duration: int = HOUR,
           lineage: tuple[str, ...] = (), run: str = "2026090512",
           product_type: str = "forecast", interval: int = HOUR,
           threshold: float | None = None) -> WeatherSample:
    canonical = {"temperature": "C", "feels_like": "C", "wind_speed": "km/h",
                 "wind_gust": "km/h", "precipitation_amount": "mm",
                 "precipitation_probability": "%"}
    return WeatherSample(
        source_id=source_id, variable=variable, value=value,
        unit=unit or canonical.get(variable, ""),
        statistic=statistic, valid_from=now + valid_from_offset,
        valid_to=now + valid_from_offset + duration, fetched_at=now,
        issued_at=now + issued_offset, interval_seconds=interval,
        product_type=product_type, lineage_ids=lineage, model_run_id=run,
        threshold=threshold)


LINEAGE = {"dwd": ("icon-d2",), "open_meteo_dwd": ("icon-d2",),
           "meteoswiss": ("icon-ch1",), "global_gfs": ("gfs",)}


def forecast(source_id: str, variable: str, value: float | None, **kw) -> WeatherSample:
    kw.setdefault("lineage", LINEAGE.get(source_id, ()))
    return sample(source_id, variable, value, **kw)


def wetteronline() -> SourceDescriptor:
    """A second comparison product for Germany, same resolution class."""
    return SourceDescriptor(
        id="wetteronline", publisher="WetterOnline",
        product_type=ProductType.FORECAST, authority=Authority.QUALIFIED_OTHER,
        coverage=Coverage(jurisdiction="DE", bbox=DE_BOX,
                          spatial_resolution_km=11.0),
        supported_variables=FORECAST_FIELDS, supported_horizon_hours=168,
        model_family="ECMWF", model_run_id="2026090512",
        lineage_ids=("ecmwf-hres",), max_issue_age_seconds=6 * HOUR,
        attribution="WetterOnline")
