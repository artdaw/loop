"""Travel research: real providers, recorded evidence, honest gaps (travel §4).

The evidence model in `evidence.py` was complete and had nothing to record.
`EvidenceItem` knew about freshness ceilings, authority and conflicts;
`claims_supported` knew which claims may be made at all — but no code ever
fetched anything, so every trip plan was built from whatever a caller passed
in by hand.

What this module adds is the fetching half, under three rules the contract is
specific about:

* **Transport is separate from normalisation**, as it is for the weather
  adapters, so parsing is pure and tested against recorded payloads.
* **An unavailable source is reported, never omitted.** A claim type with no
  configured provider is named in the result. Silence would be
  indistinguishable from "we looked and found nothing", and the difference is
  exactly whether the plan may claim the fact.
* **Documented access is honoured, including its rate limits.** Nominatim's
  usage policy requires at most one request per second and an identifying
  User-Agent with a contact. A provider that ignores that is not "configured",
  it is misused — so the contact is required configuration and the limiter is
  part of the adapter rather than an optional courtesy.

Fares and availability deliberately have **no shipped provider**. Every source
worth trusting for them needs an account, and travel §4 is explicit that
adapters incurring provider charges require configured account and budget
authority. Shipping a scraper instead would produce exactly the confidently
wrong prices the contract is written to prevent, so the honest output is
`unavailable_claims` naming `fare` and `availability`.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from loop.capabilities.travel.evidence import (
    ClaimType,
    EvidenceItem,
    EvidenceSet,
    SourceKind,
)
from loop.capabilities.weather.adapters.base import AdapterError, HttpTransport
from loop.core.ids import new_id

logger = logging.getLogger(__name__)

NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
OVERPASS_INTERPRETER = "https://overpass-api.de/api/interpreter"

#: Nominatim's published usage policy: at most one request per second from a
#: single application, with an identifying User-Agent.
NOMINATIM_MIN_INTERVAL_SECONDS = 1.0
OVERPASS_MIN_INTERVAL_SECONDS = 1.0


class RateLimiter:
    """Enforces a minimum interval between calls.

    The clock and the sleep are injected so a test can prove the limiter waits
    without actually waiting — a rate-limit test that really sleeps is a test
    that gets deleted the first time the suite feels slow.
    """

    def __init__(self, min_interval_seconds: float, *,
                 monotonic: Any = time.monotonic, sleep: Any = time.sleep) -> None:
        self.min_interval = float(min_interval_seconds)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> float:
        """Block until the next call is allowed. Returns how long it waited."""
        now = self._monotonic()
        if self._last is None:
            self._last = now
            return 0.0
        elapsed = now - self._last
        delay = max(0.0, self.min_interval - elapsed)
        if delay > 0:
            self._sleep(delay)
        self._last = self._monotonic()
        return delay


@dataclass
class Place:
    """A resolved location, with the evidence that resolved it."""

    name: str
    latitude: float
    longitude: float
    display_name: str = ""
    osm_type: str = ""
    osm_id: str = ""


@dataclass
class VenueFacts:
    """What an official/community record says about a venue."""

    name: str
    opening_hours: str = ""
    wheelchair: str = ""
    website: str = ""
    phone: str = ""
    osm_id: str = ""

    @property
    def has_hours(self) -> bool:
        return bool(self.opening_hours.strip())


@dataclass
class ResearchResult:
    """One provider's answer plus the evidence item recording it."""

    provider: str
    claim_type: ClaimType
    value: Any
    evidence: EvidenceItem


@dataclass
class ResearchReport:
    """What a research pass found, and what it could not look for at all."""

    results: list[ResearchResult] = field(default_factory=list)
    evidence: EvidenceSet = field(default_factory=EvidenceSet)
    #: Claim types with no configured provider. Named, never omitted.
    unavailable_claims: dict[str, str] = field(default_factory=dict)
    #: Providers that were configured and failed, with their reason.
    failures: dict[str, str] = field(default_factory=dict)

    def values_for(self, claim_type: ClaimType) -> list[Any]:
        return [r.value for r in self.results if r.claim_type is claim_type]

    @property
    def ok(self) -> bool:
        return not self.failures and not self.unavailable_claims


class ResearchProvider(Protocol):
    """What every travel research adapter provides."""

    @property
    def name(self) -> str:
        """Stable provider identifier, recorded on every evidence item."""

    @property
    def claim_type(self) -> ClaimType:
        """The single claim type this provider can support."""

    def lookup(self, query: str, *, now: int, **kwargs: Any) -> list[ResearchResult]:
        """Fetch and normalise. Raises AdapterError on any provider failure."""


def _evidence(*, url: str, kind: SourceKind, claim_type: ClaimType,
              now: int, provider: str, excerpt: str) -> EvidenceItem:
    """Build an evidence item for something this run actually fetched.

    Deliberately **no** `expires_at`. `EvidenceItem.freshness` already applies
    the contract's per-claim ceiling to `fetched_at`, so stamping the same
    ceiling onto each item duplicates the rule into every row that was ever
    recorded — and a later change to the ceiling would then apply to new
    evidence and not to old, for no reason anyone could see. `expires_at` is
    for a provider that publishes its *own* expiry ("use the earlier provider
    expiry when supplied", travel §4); neither shipped provider does, so
    nothing sets it yet.
    """
    return EvidenceItem(
        id=new_id(), url=url, kind=kind, claim_type=claim_type,
        fetched_at=now, provider=provider, excerpt=excerpt[:500], read=True)


class NominatimPlaces:
    """Place resolution through OpenStreetMap Nominatim.

    The contact is required, not optional: Nominatim's usage policy asks for an
    identifying User-Agent so an abusive client can be contacted rather than
    blocked wholesale. Sending the library default would be using the service
    outside its documented terms, which is not the same as it being free.
    """

    def __init__(self, *, contact: str, transport: HttpTransport | None = None,
                 limiter: RateLimiter | None = None,
                 base_url: str = NOMINATIM_SEARCH) -> None:
        if not contact.strip():
            raise AdapterError(
                "nominatim",
                "Nominatim requires a contact (email or URL) in its User-Agent; "
                "set it in the vault manifest before enabling this provider.")
        self.contact = contact.strip()
        self.base_url = base_url
        self._transport = transport or HttpTransport(
            user_agent=f"loop/0.1 (personal assistant; {self.contact})")
        self._limiter = limiter or RateLimiter(NOMINATIM_MIN_INTERVAL_SECONDS)

    @property
    def name(self) -> str:
        return "nominatim"

    @property
    def claim_type(self) -> ClaimType:
        # A place's coordinates are a durable published record, not a schedule
        # — but the enum has no "place" member and inventing one here would put
        # a freshness rule in two places. `EDITORIAL`'s seven-day ceiling is
        # the closest honest fit for a geocode, and it is labelled as an
        # official page so its authority is not understated.
        return ClaimType.EDITORIAL

    def lookup(self, query: str, *, now: int, limit: int = 5,
               **kwargs: Any) -> list[ResearchResult]:
        del kwargs
        self._limiter.wait()
        response = self._transport.get(self.base_url, params={
            "q": query, "format": "jsonv2", "limit": limit, "addressdetails": 0})
        if response.status_code != 200:
            raise AdapterError(self.name, f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except Exception as exc:                       # noqa: BLE001 — reported
            raise AdapterError(self.name, f"unreadable response: {exc}") from exc
        return self.parse(payload, query=query, now=now)

    def parse(self, payload: Any, *, query: str, now: int) -> list[ResearchResult]:
        if not isinstance(payload, list):
            raise AdapterError(self.name, "response was not a JSON array")
        results: list[ResearchResult] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            try:
                place = Place(
                    name=str(entry.get("name") or query),
                    latitude=float(entry["lat"]), longitude=float(entry["lon"]),
                    display_name=str(entry.get("display_name", "")),
                    osm_type=str(entry.get("osm_type", "")),
                    osm_id=str(entry.get("osm_id", "")))
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping Nominatim entry without coordinates")
                continue
            results.append(ResearchResult(
                provider=self.name, claim_type=self.claim_type, value=place,
                evidence=_evidence(
                    url=f"https://www.openstreetmap.org/{place.osm_type}/{place.osm_id}",
                    kind=SourceKind.OFFICIAL_PAGE, claim_type=self.claim_type,
                    now=now, provider=self.name,
                    excerpt=place.display_name or place.name)))
        return results


#: Overpass QL is a query language, and a venue name is user input. Anything
#: outside this set is stripped rather than escaped: a name is being matched,
#: not executed, so there is no legitimate reason for a quote or a bracket in
#: it, and stripping cannot be got wrong the way escaping can.
_OVERPASS_SAFE = re.compile(r"[^\w\s\-'.&äöüßàâçéèêëîïôùûÿñæœÄÖÜÀÂÇÉÈÊËÎÏÔÙÛŸÑÆŒ]")


def overpass_query(name: str, *, latitude: float, longitude: float,
                   radius_m: int = 2000) -> str:
    """Build a bounded Overpass query for one named venue near a point."""
    safe = _OVERPASS_SAFE.sub(" ", name).replace('"', "").strip()
    if not safe:
        raise AdapterError("overpass", "venue name contained nothing searchable")
    return (
        "[out:json][timeout:20];"
        f'nwr(around:{int(radius_m)},{latitude:.5f},{longitude:.5f})'
        f'["name"~"{safe}",i];'
        "out tags center 10;")


class OverpassVenues:
    """Venue opening hours and accessibility from OpenStreetMap via Overpass.

    OSM is community-maintained, so this is `OFFICIAL_PAGE` only when the venue
    record carries its own `website`; otherwise it is `EDITORIAL`. That
    distinction is not cosmetic — `SourceKind.is_authoritative` decides whether
    a claim may be stated as fact, and community data asserting a museum's
    opening hours is a good lead, not an authority.
    """

    def __init__(self, *, transport: HttpTransport | None = None,
                 limiter: RateLimiter | None = None,
                 base_url: str = OVERPASS_INTERPRETER) -> None:
        self.base_url = base_url
        self._transport = transport or HttpTransport()
        self._limiter = limiter or RateLimiter(OVERPASS_MIN_INTERVAL_SECONDS)

    @property
    def name(self) -> str:
        return "overpass"

    @property
    def claim_type(self) -> ClaimType:
        return ClaimType.VENUE_HOURS

    def lookup(self, query: str, *, now: int, latitude: float = 0.0,
               longitude: float = 0.0, radius_m: int = 2000,
               **kwargs: Any) -> list[ResearchResult]:
        del kwargs
        statement = overpass_query(query, latitude=latitude,
                                   longitude=longitude, radius_m=radius_m)
        self._limiter.wait()
        response = self._transport.get(self.base_url, params={"data": statement})
        if response.status_code != 200:
            raise AdapterError(self.name, f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except Exception as exc:                       # noqa: BLE001 — reported
            raise AdapterError(self.name, f"unreadable response: {exc}") from exc
        return self.parse(payload, now=now)

    def parse(self, payload: Any, *, now: int) -> list[ResearchResult]:
        if not isinstance(payload, dict) or "elements" not in payload:
            raise AdapterError(self.name, "response contained no elements")
        results: list[ResearchResult] = []
        for element in payload["elements"]:
            if not isinstance(element, dict):
                continue
            tags = element.get("tags") or {}
            if not isinstance(tags, dict) or not tags.get("name"):
                continue
            facts = VenueFacts(
                name=str(tags["name"]),
                opening_hours=str(tags.get("opening_hours", "")),
                wheelchair=str(tags.get("wheelchair", "")),
                website=str(tags.get("website") or tags.get("contact:website", "")),
                phone=str(tags.get("phone", "")),
                osm_id=f"{element.get('type', 'node')}/{element.get('id', '')}")
            kind = (SourceKind.OFFICIAL_PAGE if facts.website
                    else SourceKind.EDITORIAL)
            results.append(ResearchResult(
                provider=self.name, claim_type=self.claim_type, value=facts,
                evidence=_evidence(
                    url=facts.website or
                        f"https://www.openstreetmap.org/{facts.osm_id}",
                    kind=kind, claim_type=self.claim_type, now=now,
                    provider=self.name,
                    excerpt=json.dumps({k: v for k, v in tags.items()
                                        if k in ("name", "opening_hours",
                                                 "wheelchair", "website")},
                                       sort_keys=True, ensure_ascii=False))))
        return results


#: Claim types nothing shipped can answer, and why. Reported on every pass so a
#: missing fare is visibly a missing *provider*, not a missing fare.
UNSHIPPED_CLAIMS = {
    ClaimType.FARE: ("no fare provider is configured; every source worth "
                     "trusting for fares needs an account, and travel §4 "
                     "requires configured account and budget authority first"),
    ClaimType.AVAILABILITY: ("no availability provider is configured; dated "
                             "quotes require a provider account"),
    ClaimType.SCHEDULE: ("no transport schedule provider is configured"),
}


class TravelResearch:
    """Runs configured providers and reports precisely what it could not do."""

    def __init__(self, providers: list[Any] | None = None) -> None:
        self.providers = list(providers or [])

    @property
    def configured_claims(self) -> set[ClaimType]:
        return {provider.claim_type for provider in self.providers}

    def unavailable(self) -> dict[str, str]:
        """Claim types with no provider behind them, with the reason."""
        gaps: dict[str, str] = {}
        for claim_type, reason in UNSHIPPED_CLAIMS.items():
            if claim_type not in self.configured_claims:
                gaps[claim_type.value] = reason
        return gaps

    def research(self, query: str, *, now: int,
                 claim_types: list[ClaimType] | None = None,
                 **kwargs: Any) -> ResearchReport:
        """Ask every provider that can answer, and record what happened.

        A provider raising is recorded as a failure and does not stop the
        others: one dead venue lookup must not lose a resolved place.
        """
        report = ResearchReport(unavailable_claims=self.unavailable())
        wanted = set(claim_types) if claim_types else None

        for provider in self.providers:
            if wanted is not None and provider.claim_type not in wanted:
                continue
            try:
                found = provider.lookup(query, now=now, **kwargs)
            except AdapterError as exc:
                logger.warning("Research provider %s failed: %s",
                               provider.name, exc.message)
                report.failures[provider.name] = exc.message
                continue
            for result in found:
                report.results.append(result)
                report.evidence.add(result.evidence)
        return report
