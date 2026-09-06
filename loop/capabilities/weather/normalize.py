"""Samples, units, accumulation intervals and age (weather §4).

Every conversion here is deterministic and every one of them is a place where a
plausible-looking shortcut produces a confident wrong number:

* A three-hour rain probability is not three hourly probabilities, and dividing
  it by three is not a conversion — it is an invention (WF06).
* A probability is not an amount. 60% is not 60 mm, and the two must never meet
  in the same field.
* Accumulated amounts may be summed only over complete, non-overlapping
  intervals. A missing hour makes the total missing, not smaller.
* Feels-like stays null when the provider omits it. A derived value is allowed
  only from a documented formula and is then labelled derived (WF07).
* Re-fetching an unchanged model run does not make it new: age is measured from
  ``issued_at``, never from ``fetched_at`` (WF08).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class Statistic(str, Enum):
    INSTANT = "instant"
    MEAN = "mean"
    MIN = "min"
    MAX = "max"
    SUM = "sum"
    PROBABILITY = "probability"


#: Canonical units (weather §4).
CANONICAL_UNITS = {
    "temperature": "C",
    "feels_like": "C",
    "wind_speed": "km/h",
    "wind_gust": "km/h",
    "precipitation_amount": "mm",
    "precipitation_probability": "%",
}


class Freshness(str, Enum):
    """Three-valued, because "we do not know how old this is" is not fresh."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass
class WeatherSample:
    """One normalized value with its provenance and validity window."""

    source_id: str
    variable: str
    value: float | None
    unit: str
    statistic: Statistic
    valid_from: int                       # epoch seconds
    valid_to: int
    fetched_at: int
    product_type: str = "forecast"
    issued_at: int | None = None
    observed_at: int | None = None
    interval_seconds: int = 3600
    phenomenon: str = ""
    threshold: float | None = None
    elevation_m: float | None = None
    lineage_ids: tuple[str, ...] = ()
    model_run_id: str = ""
    missing_reason: str = ""
    derived: bool = False
    original_unit: str = ""
    original_value: float | None = None

    @property
    def is_missing(self) -> bool:
        return self.value is None

    def freshness(self, *, now: int, max_issue_age_seconds: int) -> Freshness:
        """Age is measured from the provider's issue time, not the fetch time.

        Polling an unchanged old model run every minute keeps ``fetched_at``
        current and changes nothing about how old the forecast is (WF08).
        """
        reference = self.issued_at if self.issued_at is not None else self.observed_at
        if reference is None:
            return Freshness.UNKNOWN
        if now - reference > max_issue_age_seconds:
            return Freshness.STALE
        return Freshness.FRESH

    def covers(self, start: int, end: int) -> bool:
        return self.valid_from <= start and self.valid_to >= end

    def comparable_key(self) -> tuple:
        """Two samples may be compared only when every one of these matches.

        Statistic and interval are part of the key: a 3-hour probability and a
        1-hour probability describe different questions (WF06).
        """
        return (self.variable, self.statistic, self.interval_seconds,
                self.phenomenon, self.threshold)


# --------------------------------------------------------------------------- #
# Unit conversion
# --------------------------------------------------------------------------- #
def to_celsius(value: float, unit: str) -> float:
    unit = unit.strip()
    if unit in ("C", "°C", "celsius"):
        return value
    if unit in ("K", "kelvin"):
        return value - 273.15
    if unit in ("F", "°F", "fahrenheit"):
        return (value - 32.0) * 5.0 / 9.0
    raise ValueError(f"unsupported temperature unit {unit!r}")


def to_kph(value: float, unit: str) -> float:
    unit = unit.strip()
    if unit in ("km/h", "kph"):
        return value
    if unit in ("m/s", "mps"):
        return value * 3.6
    if unit in ("mph",):
        return value * 1.609344
    if unit in ("kn", "knots"):
        return value * 1.852
    raise ValueError(f"unsupported wind unit {unit!r}")


def to_millimetres(value: float, unit: str) -> float:
    unit = unit.strip()
    if unit in ("mm",):
        return value
    if unit in ("cm",):
        return value * 10.0
    if unit in ("m",):
        return value * 1000.0
    if unit in ("in", "inch", "inches"):
        return value * 25.4
    raise ValueError(f"unsupported precipitation unit {unit!r}")


def to_probability_percent(value: float, unit: str) -> float:
    unit = unit.strip()
    if unit in ("%", "percent"):
        return value
    if unit in ("fraction", "ratio", "0-1"):
        return value * 100.0
    raise ValueError(f"unsupported probability unit {unit!r}")


_CONVERTERS = {
    "temperature": (to_celsius, "C"),
    "feels_like": (to_celsius, "C"),
    "wind_speed": (to_kph, "km/h"),
    "wind_gust": (to_kph, "km/h"),
    "precipitation_amount": (to_millimetres, "mm"),
    "precipitation_probability": (to_probability_percent, "%"),
}


def normalize(sample: WeatherSample) -> WeatherSample:
    """Convert one sample to canonical units, keeping the original for audit."""
    if sample.value is None:
        return sample
    converter = _CONVERTERS.get(sample.variable)
    if converter is None:
        return sample
    convert, canonical = converter
    if sample.unit == canonical:
        return sample

    converted = convert(sample.value, sample.unit)
    sample.original_value = sample.value
    sample.original_unit = sample.unit
    sample.value = converted
    sample.unit = canonical
    return sample


# --------------------------------------------------------------------------- #
# Accumulation intervals
# --------------------------------------------------------------------------- #
@dataclass
class IntervalWindow:
    """A provider accumulation window, which may be labelled by its end time."""

    start: int
    end: int
    value: float | None

    @property
    def seconds(self) -> int:
        return self.end - self.start


def window_from_end_label(end: int, interval_seconds: int,
                          value: float | None) -> IntervalWindow:
    """Many providers stamp an accumulation with the *end* of its interval.

    Reading that timestamp as the start shifts every rain total forward by the
    interval length, which is how a forecast of rain during the commute becomes
    a forecast of rain after it (WF07).
    """
    return IntervalWindow(start=end - interval_seconds, end=end, value=value)


def sum_accumulation(windows: list[IntervalWindow]) -> tuple[float | None, str]:
    """Sum only complete, non-overlapping intervals (weather §4).

    Returns ``(total, missing_reason)``. A gap or an overlap makes the total
    missing: a partial sum understates rainfall while looking like a measurement.
    """
    if not windows:
        return None, "no intervals supplied"

    ordered = sorted(windows, key=lambda w: w.start)
    total = 0.0
    for index, window in enumerate(ordered):
        if window.value is None:
            return None, "one or more intervals are missing"
        if index and ordered[index - 1].end > window.start:
            return None, "intervals overlap"
        if index and ordered[index - 1].end < window.start:
            return None, "intervals are not contiguous"
        total += window.value
    return total, ""


def split_probability(*_args: Any, **_kwargs: Any) -> None:
    """Deliberately unimplemented.

    A three-hour probability of precipitation cannot be divided into hourly
    probabilities: P(rain in 3h) is not 3 × P(rain in 1h), and the arithmetic
    that looks like it works assumes independence nobody has established. The
    spec forbids it, so the function that would do it does not exist (WF06).
    """
    raise NotImplementedError(
        "a multi-hour probability cannot be divided into hourly probabilities")


def combine_probabilities(*_args: Any, **_kwargs: Any) -> None:
    """Also deliberately unimplemented — averaging hourly probabilities into a
    whole-trip risk is the same error in the other direction (WF06)."""
    raise NotImplementedError(
        "hourly probabilities cannot be averaged into a whole-window risk")


def aligned(samples: list[WeatherSample]) -> list[list[WeatherSample]]:
    """Group samples that may legitimately be compared with one another."""
    groups: dict[tuple, list[WeatherSample]] = {}
    for sample in samples:
        groups.setdefault(sample.comparable_key(), []).append(sample)
    return [groups[key] for key in sorted(groups, key=str)]
