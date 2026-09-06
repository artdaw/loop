"""Open-Meteo adapters — parsing, provenance and failure handling.

The payload below is the shape the live API returns, recorded rather than
invented. Nothing here touches a network: the transport is injected.
"""

from __future__ import annotations

import json

import pytest

from loop.capabilities.weather.adapters.base import (
    AdapterError,
    HttpResponse,
    HttpTransport,
)
from loop.capabilities.weather.adapters.open_meteo import (
    OPEN_METEO_MODELS,
    OpenMeteoAdapter,
    build_adapters,
    open_meteo_descriptors,
)
from loop.capabilities.weather.compare import evidence_family
from loop.capabilities.weather.normalize import Freshness, Statistic

NOW = 1_788_714_000

RECORDED = {
    "latitude": 52.52, "longitude": 13.41, "elevation": 38.0,
    "utc_offset_seconds": 0, "timezone": "GMT",
    "hourly_units": {"time": "iso8601", "temperature_2m": "°C",
                     "precipitation_probability": "%",
                     "wind_speed_10m": "km/h", "precipitation": "mm"},
    "hourly": {
        "time": ["2026-09-06T09:00", "2026-09-06T10:00", "2026-09-06T11:00"],
        "temperature_2m": [19.5, 18.9, None],
        "precipitation_probability": [0, 10, 20],
        "wind_speed_10m": [6.9, 7.4, 8.1],
        "precipitation": [0.0, 0.1, 0.3],
    },
}


class FakeTransport(HttpTransport):
    """Returns a recorded payload and records what was requested."""

    def __init__(self, payload=None, *, status: int = 200) -> None:
        super().__init__()
        self.payload = RECORDED if payload is None else payload
        self.status = status
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, *, params=None) -> HttpResponse:
        self.calls.append((url, dict(params or {})))
        return HttpResponse(self.status,
                            json.dumps(self.payload).encode("utf-8"))


def _adapter(transport=None) -> OpenMeteoAdapter:
    return OpenMeteoAdapter(OPEN_METEO_MODELS[0],
                            transport=transport or FakeTransport())


def _fetch(adapter, variables=("temperature",)):
    return adapter.fetch(latitude=52.52, longitude=13.405, start=NOW,
                         end=NOW + 3 * 3600, variables=list(variables), now=NOW)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_the_hourly_series_becomes_one_sample_per_hour():
    samples = _fetch(_adapter())
    assert len(samples) == 3


def test_values_and_units_are_canonical():
    sample = _fetch(_adapter())[0]

    assert sample.value == 19.5
    assert sample.unit == "C"


def test_a_null_value_stays_missing_not_zero():
    samples = _fetch(_adapter())

    assert samples[2].value is None
    assert samples[2].is_missing


def test_the_hour_is_the_interval_start():
    samples = _fetch(_adapter())
    assert samples[0].valid_to - samples[0].valid_from == 3600


def test_probability_and_amount_get_different_statistics():
    samples = _fetch(_adapter(), ("precipitation_probability",
                                  "precipitation_amount"))
    by_variable = {s.variable: s for s in samples}

    assert by_variable["precipitation_probability"].statistic \
        is Statistic.PROBABILITY
    assert by_variable["precipitation_amount"].statistic is Statistic.SUM


def test_an_unsupported_variable_is_simply_absent():
    samples = _fetch(_adapter(), ("temperature", "visibility"))
    assert {s.variable for s in samples} == {"temperature"}


def test_the_elevation_is_carried_through():
    assert _fetch(_adapter())[0].elevation_m == 38.0


def test_the_window_is_passed_to_the_provider():
    transport = FakeTransport()
    _fetch(_adapter(transport))
    _, params = transport.calls[0]

    assert params["timezone"] == "UTC"
    assert params["start_hour"].endswith(":00")


# --------------------------------------------------------------------------- #
# Provenance and freshness
# --------------------------------------------------------------------------- #
def test_the_issue_time_is_not_invented_from_the_fetch_time():
    """This endpoint publishes no model run, so freshness is unknown."""
    sample = _fetch(_adapter())[0]

    assert sample.issued_at is None
    assert sample.freshness(now=NOW, max_issue_age_seconds=3600) \
        is Freshness.UNKNOWN


def test_the_publisher_and_transport_are_recorded_separately():
    descriptor = _adapter().descriptor

    assert descriptor.publisher == "DWD"
    assert descriptor.transport == "Open-Meteo"


def test_the_three_models_are_distinct_evidence_families():
    """One transport, three model families — the WF03 distinction."""
    families = set()
    for model in OPEN_METEO_MODELS:
        sample = OpenMeteoAdapter(model, transport=FakeTransport()).fetch(
            latitude=52.52, longitude=13.405, start=NOW, end=NOW + 3600,
            variables=["temperature"], now=NOW)[0]
        families.add(evidence_family(sample))

    assert len(families) == 3


def test_every_model_shares_the_same_transport_name():
    assert {d.transport for d in open_meteo_descriptors()} == {"Open-Meteo"}


def test_max_issue_age_comes_from_the_publication_cadence():
    """Two run intervals, never a universal one-hour rule."""
    for model, descriptor in zip(OPEN_METEO_MODELS, open_meteo_descriptors(),
                                 strict=True):
        assert descriptor.max_issue_age_seconds == \
            model.update_interval_seconds * 2


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #
def test_a_non_200_response_is_an_adapter_error():
    with pytest.raises(AdapterError, match="HTTP 503"):
        _fetch(_adapter(FakeTransport(status=503)))


def test_a_provider_error_payload_is_reported():
    payload = {"error": True, "reason": "latitude must be in range"}
    with pytest.raises(AdapterError, match="latitude"):
        _fetch(_adapter(FakeTransport(payload)))


def test_a_response_without_an_hourly_series_is_an_error():
    with pytest.raises(AdapterError, match="no hourly series"):
        _fetch(_adapter(FakeTransport({"latitude": 52.5})))


def test_the_error_names_the_source_so_the_bundle_can_report_it():
    with pytest.raises(AdapterError) as excinfo:
        _fetch(_adapter(FakeTransport(status=500)))

    assert excinfo.value.source_id == "open_meteo_dwd_icon"


def test_build_adapters_can_be_narrowed_to_configured_models():
    adapters = build_adapters(model_ids=["open_meteo_gfs"])
    assert [a.model.source_id for a in adapters] == ["open_meteo_gfs"]
