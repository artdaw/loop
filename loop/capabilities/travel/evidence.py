"""Research evidence and freshness (travel §4).

Two distinctions carry the weight:

* **A search snippet is not a source.** A snippet is a search engine's summary
  of a page nobody opened; treating it as evidence means citing text that may
  not appear on the page at all. When a route adapter is missing, the fallback
  is an actually-read official page — not a model's recollection (TR11).
* **Expiry is per claim type.** A fare quote is stale in fifteen minutes; a
  published timetable lasts a day; venue hours last a week. One global TTL would
  either re-fetch timetables pointlessly or serve expired fares (TR10).

Conflicting authoritative sources stay unresolved. Picking the more convenient
one produces a confident plan built on a coin flip.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ClaimType(str, Enum):
    FARE = "fare"
    AVAILABILITY = "availability"
    SCHEDULE = "schedule"
    VENUE_HOURS = "venue_hours"
    EDITORIAL = "editorial"


#: Freshness ceilings in seconds (travel §4, §8).
FRESHNESS_SECONDS = {
    ClaimType.FARE: 900,
    ClaimType.AVAILABILITY: 900,
    ClaimType.SCHEDULE: 86_400,
    ClaimType.VENUE_HOURS: 604_800,
    ClaimType.EDITORIAL: 604_800,
}


class SourceKind(str, Enum):
    OFFICIAL_PAGE = "official_page"
    PROVIDER_ADAPTER = "provider_adapter"
    EDITORIAL = "editorial"
    SEARCH_SNIPPET = "search_snippet"

    @property
    def is_readable_evidence(self) -> bool:
        """Whether this may support a factual claim (TR11).

        A snippet is a summary produced by a search engine, not the page. It can
        point at evidence; it cannot be evidence.
        """
        return self is not SourceKind.SEARCH_SNIPPET

    @property
    def is_authoritative(self) -> bool:
        return self in (SourceKind.OFFICIAL_PAGE, SourceKind.PROVIDER_ADAPTER)


class Freshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass
class EvidenceItem:
    """One recorded piece of research (travel §4)."""

    id: str
    url: str
    kind: SourceKind
    claim_type: ClaimType
    fetched_at: int
    excerpt: str = ""
    applies_from: str = ""
    applies_to: str = ""
    expires_at: int | None = None
    provider: str = ""
    read: bool = True

    def freshness(self, *, now: int,
                  ceilings: dict[ClaimType, int] | None = None) -> Freshness:
        limits = {**FRESHNESS_SECONDS, **(ceilings or {})}
        if self.expires_at is not None:
            return Freshness.FRESH if now < self.expires_at else Freshness.STALE
        if not self.fetched_at:
            return Freshness.UNKNOWN
        age = now - self.fetched_at
        return (Freshness.FRESH if age <= limits[self.claim_type]
                else Freshness.STALE)

    @property
    def supports_claims(self) -> bool:
        return self.kind.is_readable_evidence and self.read


@dataclass
class EvidenceSet:
    items: list[EvidenceItem] = field(default_factory=list)

    def add(self, item: EvidenceItem) -> EvidenceItem:
        self.items.append(item)
        return item

    def usable(self, *, now: int) -> list[EvidenceItem]:
        return [item for item in self.items
                if item.supports_claims
                and item.freshness(now=now) is Freshness.FRESH]

    def stale(self, *, now: int) -> list[EvidenceItem]:
        return [item for item in self.items
                if item.freshness(now=now) is not Freshness.FRESH]

    def for_claim(self, claim_type: ClaimType, *,
                  now: int) -> list[EvidenceItem]:
        return [item for item in self.usable(now=now)
                if item.claim_type is claim_type]


@dataclass
class Conflict:
    claim_type: ClaimType
    source_ids: tuple[str, ...]
    detail: str


def detect_conflicts(items: list[EvidenceItem], *,
                     values: dict[str, object]) -> list[Conflict]:
    """Find authoritative sources that disagree (travel §4).

    Disagreement between two official pages is unresolved, and it downgrades
    every option that depends on it. Choosing one silently is how a plan becomes
    confidently wrong.
    """
    authoritative = [item for item in items if item.kind.is_authoritative]
    by_claim: dict[ClaimType, list[EvidenceItem]] = {}
    for item in authoritative:
        by_claim.setdefault(item.claim_type, []).append(item)

    conflicts: list[Conflict] = []
    for claim_type, group in sorted(by_claim.items(), key=lambda kv: kv[0].value):
        distinct = {values.get(item.id) for item in group
                    if item.id in values}
        if len(distinct) > 1:
            conflicts.append(Conflict(
                claim_type, tuple(sorted(item.id for item in group)),
                f"authoritative sources disagree on {claim_type.value}: "
                f"{sorted(map(str, distinct))}"))
    return conflicts


def claims_supported(claim_type: ClaimType, evidence: EvidenceSet, *,
                     now: int) -> tuple[bool, str]:
    """Whether a claim of this type may be made at all (TR10, TR11)."""
    usable = evidence.for_claim(claim_type, now=now)
    if usable:
        return True, ""

    present = [item for item in evidence.items if item.claim_type is claim_type]
    if not present:
        return False, f"no source was read for {claim_type.value}"
    if all(not item.supports_claims for item in present):
        return False, (f"only search snippets are available for "
                       f"{claim_type.value}; the pages were not read")
    return False, f"the {claim_type.value} evidence has expired and needs a recheck"
