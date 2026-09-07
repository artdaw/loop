"""Travel research providers (travel §4, M6).

Real adapters against recorded payloads and an injected transport — the same
separation the weather adapters use. The payload shapes are Nominatim's
`jsonv2` and Overpass's `[out:json]`, not simplifications, because the parsing
is the part worth testing.

The recurring theme is what the research layer refuses to do: state a fare it
has no provider for, treat community data as authoritative, execute a venue
name as a query, or ignore a provider's published rate limit.
"""

from __future__ import annotations

import json

import pytest

from loop.capabilities.travel.evidence import (
    ClaimType,
    Freshness,
    SourceKind,
    claims_supported,
)
from loop.capabilities.travel.research import (
    NominatimPlaces,
    OverpassVenues,
    RateLimiter,
    TravelResearch,
    overpass_query,
)
from loop.capabilities.weather.adapters.base import (
    AdapterError,
    HttpResponse,
    HttpTransport,
)

NOW = 1_788_714_000

NOMINATIM_PAYLOAD = [
    {"place_id": 1, "osm_type": "relation", "osm_id": "62422",
     "lat": "52.5170365", "lon": "13.3888599", "name": "Berlin",
     "display_name": "Berlin, Germany", "category": "boundary"},
    {"place_id": 2, "osm_type": "node", "osm_id": "240109189",
     "lat": "52.5200066", "lon": "13.404954", "name": "Berlin Mitte",
     "display_name": "Mitte, Berlin, Germany", "category": "place"},
]

OVERPASS_PAYLOAD = {
    "version": 0.6,
    "elements": [
        {"type": "way", "id": 30809531, "tags": {
            "name": "Pergamonmuseum",
            "opening_hours": "Tu-Su 10:00-18:00; Th 10:00-20:00",
            "wheelchair": "yes",
            "website": "https://www.smb.museum/museen-einrichtungen/pergamonmuseum/",
            "tourism": "museum"}},
        {"type": "node", "id": 999, "tags": {
            "name": "Kleines Café",
            "opening_hours": "Mo-Fr 08:00-16:00",
            "amenity": "cafe"}},
        {"type": "node", "id": 1000, "tags": {"amenity": "bench"}},
    ],
}


class FakeTransport(HttpTransport):
    def __init__(self, payload, *, status: int = 200) -> None:
        super().__init__()
        self.payload = payload
        self.status = status
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, *, params=None) -> HttpResponse:
        self.calls.append((url, dict(params or {})))
        body = (self.payload if isinstance(self.payload, bytes)
                else json.dumps(self.payload).encode("utf-8"))
        return HttpResponse(self.status, body)


class ManualClock:
    """A monotonic clock and sleep that record instead of waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def nominatim(transport=None, **kw) -> NominatimPlaces:
    return NominatimPlaces(contact="ops@example.invalid",
                           transport=transport or FakeTransport(NOMINATIM_PAYLOAD),
                           limiter=RateLimiter(0), **kw)


# --------------------------------------------------------------------------- #
# Nominatim
# --------------------------------------------------------------------------- #
def test_a_place_lookup_returns_coordinates_and_its_evidence():
    transport = FakeTransport(NOMINATIM_PAYLOAD)

    results = nominatim(transport).lookup("Berlin", now=NOW)

    assert [r.value.name for r in results] == ["Berlin", "Berlin Mitte"]
    assert results[0].value.latitude == pytest.approx(52.5170365)
    assert results[0].evidence.provider == "nominatim"
    assert results[0].evidence.url.endswith("/relation/62422")
    assert results[0].evidence.fetched_at == NOW


def test_recorded_evidence_is_fresh_and_therefore_usable():
    """An item with neither expiry nor ceiling reads as UNKNOWN and is dropped."""
    results = nominatim().lookup("Berlin", now=NOW)

    assert results[0].evidence.freshness(now=NOW) is Freshness.FRESH
    assert results[0].evidence.supports_claims


def test_an_entry_without_coordinates_is_skipped_not_guessed():
    transport = FakeTransport([{"osm_type": "node", "osm_id": "1",
                                "name": "Nowhere"}])

    assert nominatim(transport).lookup("Nowhere", now=NOW) == []


def test_nominatim_refuses_to_run_without_a_contact():
    """Its usage policy asks for one; sending the default is not "free use"."""
    with pytest.raises(AdapterError) as raised:
        NominatimPlaces(contact="   ")
    assert "contact" in str(raised.value)


def test_a_non_200_response_is_a_named_adapter_error():
    transport = FakeTransport(NOMINATIM_PAYLOAD, status=429)

    with pytest.raises(AdapterError) as raised:
        nominatim(transport).lookup("Berlin", now=NOW)
    assert "429" in str(raised.value)


def test_a_response_that_is_not_a_list_is_refused():
    with pytest.raises(AdapterError):
        nominatim(FakeTransport({"error": "nope"})).lookup("Berlin", now=NOW)


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
def test_the_second_call_waits_for_the_published_interval():
    clock = ManualClock()
    limiter = RateLimiter(1.0, monotonic=clock.monotonic, sleep=clock.sleep)

    assert limiter.wait() == 0.0        # nothing to wait for yet
    waited = limiter.wait()

    assert waited == pytest.approx(1.0)
    assert clock.slept == [pytest.approx(1.0)]


def test_a_call_after_the_interval_has_already_passed_does_not_wait():
    clock = ManualClock()
    limiter = RateLimiter(1.0, monotonic=clock.monotonic, sleep=clock.sleep)
    limiter.wait()
    clock.now += 5.0

    assert limiter.wait() == 0.0
    assert clock.slept == []


def test_the_adapter_actually_uses_its_limiter():
    clock = ManualClock()
    provider = NominatimPlaces(
        contact="ops@example.invalid", transport=FakeTransport(NOMINATIM_PAYLOAD),
        limiter=RateLimiter(1.0, monotonic=clock.monotonic, sleep=clock.sleep))

    provider.lookup("Berlin", now=NOW)
    provider.lookup("Hamburg", now=NOW)

    assert clock.slept == [pytest.approx(1.0)]


# --------------------------------------------------------------------------- #
# Overpass
# --------------------------------------------------------------------------- #
def test_venue_hours_and_accessibility_are_read_from_the_record():
    results = OverpassVenues(transport=FakeTransport(OVERPASS_PAYLOAD),
                             limiter=RateLimiter(0)).lookup(
        "Pergamonmuseum", now=NOW, latitude=52.52, longitude=13.4)

    museum = results[0].value
    assert museum.name == "Pergamonmuseum"
    assert museum.opening_hours == "Tu-Su 10:00-18:00; Th 10:00-20:00"
    assert museum.wheelchair == "yes"
    assert museum.has_hours


def test_an_element_without_a_name_is_not_a_venue():
    results = OverpassVenues(transport=FakeTransport(OVERPASS_PAYLOAD),
                             limiter=RateLimiter(0)).lookup(
        "anything", now=NOW, latitude=52.52, longitude=13.4)

    assert len(results) == 2          # the unnamed bench is not one of them


def test_community_data_without_an_official_site_is_not_authoritative():
    """OSM asserting a café's hours is a good lead, not an authority (TR11)."""
    results = OverpassVenues(transport=FakeTransport(OVERPASS_PAYLOAD),
                             limiter=RateLimiter(0)).lookup(
        "anything", now=NOW, latitude=52.52, longitude=13.4)

    museum, cafe = results[0].evidence, results[1].evidence
    assert museum.kind is SourceKind.OFFICIAL_PAGE     # has its own website
    assert museum.kind.is_authoritative
    assert cafe.kind is SourceKind.EDITORIAL
    assert not cafe.kind.is_authoritative


def test_a_venue_name_is_matched_never_executed():
    """Overpass QL is a query language and a venue name is user input."""
    statement = overpass_query('Café "; out; //', latitude=52.5, longitude=13.4)

    assert '"];' not in statement.replace('"~"', "")
    assert "out;" not in statement.replace("out tags center 10;", "")
    assert "Café" in statement


def test_a_name_with_nothing_searchable_is_refused():
    with pytest.raises(AdapterError):
        overpass_query("!!!", latitude=52.5, longitude=13.4)


def test_the_overpass_query_is_bounded_by_radius_and_timeout():
    statement = overpass_query("Museum", latitude=52.5, longitude=13.4,
                               radius_m=1500)

    assert "timeout:20" in statement
    assert "around:1500" in statement
    assert statement.endswith("out tags center 10;")


# --------------------------------------------------------------------------- #
# The research pass: what it found, and what it could not look for
# --------------------------------------------------------------------------- #
def test_an_unconfigured_claim_type_is_named_rather_than_silently_empty():
    """A missing fare must be visibly a missing *provider* (travel §4)."""
    research = TravelResearch([nominatim()])

    report = research.research("Berlin", now=NOW)

    assert "fare" in report.unavailable_claims
    assert "availability" in report.unavailable_claims
    assert "schedule" in report.unavailable_claims
    assert "account" in report.unavailable_claims["fare"]
    assert report.ok is False


def test_a_configured_provider_removes_its_claim_from_the_gap_list():
    research = TravelResearch([
        nominatim(),
        OverpassVenues(transport=FakeTransport(OVERPASS_PAYLOAD),
                       limiter=RateLimiter(0))])

    report = research.research("Pergamonmuseum", now=NOW, latitude=52.5,
                               longitude=13.4)

    assert "venue_hours" not in report.unavailable_claims
    assert report.values_for(ClaimType.VENUE_HOURS)


def test_one_failing_provider_does_not_lose_the_others_results():
    research = TravelResearch([
        nominatim(),
        OverpassVenues(transport=FakeTransport(OVERPASS_PAYLOAD, status=504),
                       limiter=RateLimiter(0))])

    report = research.research("Berlin", now=NOW, latitude=52.5, longitude=13.4)

    assert "overpass" in report.failures
    assert "504" in report.failures["overpass"]
    assert report.values_for(ClaimType.EDITORIAL)       # the place still resolved


def test_a_claim_with_no_evidence_may_not_be_made_at_all():
    """The gate the whole evidence model exists for (TR10)."""
    research = TravelResearch([nominatim()])
    report = research.research("Berlin", now=NOW)

    supported, reason = claims_supported(ClaimType.FARE, report.evidence, now=NOW)

    assert supported is False
    assert reason


def test_a_venue_hours_claim_is_supported_once_it_has_been_researched():
    research = TravelResearch([
        OverpassVenues(transport=FakeTransport(OVERPASS_PAYLOAD),
                       limiter=RateLimiter(0))])
    report = research.research("Pergamonmuseum", now=NOW, latitude=52.5,
                               longitude=13.4)

    supported, _ = claims_supported(ClaimType.VENUE_HOURS, report.evidence,
                                    now=NOW)

    assert supported is True


def test_evidence_goes_stale_at_the_contracts_own_ceiling():
    """Venue hours: seven days (travel §4)."""
    research = TravelResearch([
        OverpassVenues(transport=FakeTransport(OVERPASS_PAYLOAD),
                       limiter=RateLimiter(0))])
    report = research.research("Pergamonmuseum", now=NOW, latitude=52.5,
                               longitude=13.4)

    later = NOW + 8 * 86_400
    supported, _ = claims_supported(ClaimType.VENUE_HOURS, report.evidence,
                                    now=later)

    assert supported is False
    assert report.evidence.stale(now=later)


def test_only_the_requested_claim_types_are_researched():
    places = nominatim()
    venues = OverpassVenues(transport=FakeTransport(OVERPASS_PAYLOAD),
                            limiter=RateLimiter(0))
    research = TravelResearch([places, venues])

    report = research.research("Berlin", now=NOW,
                               claim_types=[ClaimType.VENUE_HOURS],
                               latitude=52.5, longitude=13.4)

    assert report.values_for(ClaimType.EDITORIAL) == []
    assert report.values_for(ClaimType.VENUE_HOURS)
