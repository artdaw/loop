"""Evidence families, disagreement and coverage states (weather §4).

The question this module answers is not "what is the average forecast" but
"how much independent evidence is there, and do the sources actually agree".

**Independence is about lineage, not about hostnames.** Two websites serving the
same DWD ICON run are one piece of evidence displayed twice. Counting them as
agreement manufactures confidence out of a copy (WF03). Open-Meteo re-serves DWD
products, so a "DWD vs Open-Meteo/DWD" comparison is correlated, not independent.

**No invented consensus.** When aligned sources disagree, the answer is the
primary value *plus* the range and the disagreement — never their mean, which is
a number no source forecast and nobody can attribute (WF05).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from loop.capabilities.weather.normalize import Statistic, WeatherSample

logger = logging.getLogger(__name__)


class Confidence(str, Enum):
    """Evidence states — not calibrated probabilities of correctness (§4)."""

    CORROBORATED = "corroborated"
    SINGLE_SOURCE = "single_source"
    CORRELATED_SOURCES = "correlated_sources"
    DISAGREEMENT = "disagreement"
    UNKNOWN_ORIGIN = "unknown_origin"


class Coverage(str, Enum):
    COMPARED = "compared"
    PARTIAL = "partial"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


#: Default disagreement thresholds (weather §4), configurable in vault policy.
DISAGREEMENT_TEMPERATURE_C = 3.0
DISAGREEMENT_RAIN_PROBABILITY_POINTS = 30.0
DISAGREEMENT_WIND_KPH = 15.0

THRESHOLDS = {
    "temperature": DISAGREEMENT_TEMPERATURE_C,
    "feels_like": DISAGREEMENT_TEMPERATURE_C,
    "precipitation_probability": DISAGREEMENT_RAIN_PROBABILITY_POINTS,
    "wind_speed": DISAGREEMENT_WIND_KPH,
    "wind_gust": DISAGREEMENT_WIND_KPH,
}


def evidence_family(sample: WeatherSample) -> str:
    """The identity of the underlying evidence, regardless of who served it.

    Same model, product and run reached through two transports is one family.
    A sample with no declared lineage gets its own family keyed by source, which
    keeps it comparable but stops it claiming independence it has not shown.
    """
    if sample.lineage_ids:
        return "|".join(sorted(sample.lineage_ids)) + f"@{sample.model_run_id}"
    return f"source:{sample.source_id}@{sample.model_run_id}"


def independent_families(samples: list[WeatherSample]) -> list[str]:
    return sorted({evidence_family(s) for s in samples})


@dataclass
class FieldComparison:
    """One variable compared across every source that supplies it."""

    variable: str
    statistic: Statistic
    primary_source_id: str
    primary_value: float | None
    unit: str
    confidence: Confidence
    values_by_source: dict[str, float | None] = field(default_factory=dict)
    families: list[str] = field(default_factory=list)
    spread: float | None = None
    threshold: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def disagrees(self) -> bool:
        return self.confidence is Confidence.DISAGREEMENT

    @property
    def value_range(self) -> tuple[float, float] | None:
        present = [v for v in self.values_by_source.values() if v is not None]
        if len(present) < 2:
            return None
        return (min(present), max(present))

    def alternatives(self) -> dict[str, float | None]:
        return {k: v for k, v in self.values_by_source.items()
                if k != self.primary_source_id}


def compare_field(samples: list[WeatherSample], *, primary_source_id: str,
                  thresholds: dict[str, float] | None = None,
                  unknown_lineage_ids: set[str] | None = None) -> FieldComparison:
    """Compare aligned samples for one field (weather §4).

    ``samples`` must already be aligned — same variable, statistic, interval,
    phenomenon and threshold. Comparing across those is the WF06 failure, so
    this asserts rather than quietly coping.
    """
    if not samples:
        raise ValueError("compare_field needs at least one sample")
    keys = {s.comparable_key() for s in samples}
    if len(keys) != 1:
        raise ValueError(f"cannot compare unaligned samples: {sorted(map(str, keys))}")

    limits = {**THRESHOLDS, **(thresholds or {})}
    first = samples[0]
    by_source = {s.source_id: s.value for s in samples}

    primary = next((s for s in samples if s.source_id == primary_source_id), first)
    families = independent_families(samples)
    present = [s.value for s in samples if s.value is not None]
    spread = (max(present) - min(present)) if len(present) >= 2 else None

    notes: list[str] = []
    limit = limits.get(first.variable)
    disagrees = spread is not None and limit is not None and spread >= limit

    unknown = unknown_lineage_ids or set()
    has_unknown_origin = any(
        s.source_id in unknown or (not s.lineage_ids and not s.model_run_id)
        for s in samples)

    if disagrees:
        confidence = Confidence.DISAGREEMENT
        notes.append(
            f"sources differ by {spread:.1f} {first.unit}, at or above the "
            f"{limit:g} {first.unit} threshold")
    elif len(samples) == 1:
        confidence = Confidence.SINGLE_SOURCE
        notes.append("only one suitable source; this is not corroboration")
    elif len(families) == 1:
        confidence = Confidence.CORRELATED_SOURCES
        notes.append(
            "these feeds share one underlying model run, so agreement between "
            "them is not independent confirmation")
    elif has_unknown_origin:
        confidence = Confidence.UNKNOWN_ORIGIN
        notes.append(
            "at least one product has undisclosed lineage, so independence "
            "cannot be established")
    else:
        confidence = Confidence.CORROBORATED

    return FieldComparison(
        variable=first.variable, statistic=first.statistic,
        primary_source_id=primary.source_id, primary_value=primary.value,
        unit=first.unit, confidence=confidence, values_by_source=by_source,
        families=families, spread=spread, threshold=limit, notes=notes)


def consensus(_samples: list[WeatherSample]) -> None:
    """Deliberately unimplemented.

    Averaging several forecasts produces a number no source issued, that cannot
    be attributed, and that hides the disagreement the user needs to see. The
    spec forbids it, so there is nothing here to call by accident (WF05).
    """
    raise NotImplementedError(
        "averaging sources into a consensus probability is not permitted; "
        "show the primary value with its range instead")


def coverage_state(samples: list[WeatherSample], *, requested_fields: list[str],
                   stale_source_ids: set[str] | None = None) -> Coverage:
    """Summarise how well the request was actually covered."""
    stale = stale_source_ids or set()
    usable = [s for s in samples
              if s.value is not None and s.source_id not in stale]
    if not usable:
        if samples and all(s.source_id in stale for s in samples):
            return Coverage.STALE
        return Coverage.UNAVAILABLE

    covered = {s.variable for s in usable}
    if not set(requested_fields) <= covered:
        return Coverage.PARTIAL
    return Coverage.COMPARED


def crosses_threshold_only_in_comparator(comparison: FieldComparison, *,
                                         action_threshold: float) -> list[str]:
    """Sources whose value crosses an action threshold when the primary does not.

    This is the WF17 case: the preferred source is dry, a qualified comparator
    is not. The answer is conditional advice naming that comparator — never a
    quiet edit of the primary's number.
    """
    primary = comparison.primary_value
    if primary is None or primary >= action_threshold:
        return []
    return sorted(source_id
                  for source_id, value in comparison.alternatives().items()
                  if value is not None and value >= action_threshold)
