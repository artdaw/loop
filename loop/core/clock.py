"""Time sources (runtime contract §3, acceptance §1).

Every component takes a :class:`Clock`. Nothing calls ``datetime.now`` directly,
because DST behaviour, catch-up windows, lease expiry and cooldowns are all
specified in terms of exact instants that tests must control.

Two representations coexist deliberately:

* **Instants** are UTC, persisted as integer microseconds. Integers avoid the
  float-rounding and string-parsing differences that make time comparisons
  flaky at boundaries.
* **Local dates** stay ``YYYY-MM-DD`` strings. Runtime §3 forbids coercing a
  day-level obligation into a UTC-midnight timestamp: "due Friday" is not an
  instant, and turning it into one silently invents a deadline.
"""

from __future__ import annotations

import datetime as _dt
from typing import Protocol
from zoneinfo import ZoneInfo

UTC = _dt.UTC

#: Microseconds per second, for the integer instant representation.
MICROS = 1_000_000


class Clock(Protocol):
    """A source of the current instant."""

    def now(self) -> _dt.datetime:
        """Timezone-aware current time in UTC."""
        ...


class SystemClock:
    """The real clock. Used in production only."""

    def now(self) -> _dt.datetime:
        return _dt.datetime.now(UTC)


class FrozenClock:
    """A controllable clock for tests.

    Time advances only when a test says so, which is what makes lease expiry,
    DST boundaries and catch-up windows testable without sleeping.
    """

    def __init__(self, start: _dt.datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FrozenClock requires an aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> _dt.datetime:
        return self._now

    def advance(self, seconds: float = 0, *, minutes: float = 0,
                hours: float = 0, days: float = 0) -> _dt.datetime:
        """Move time forward and return the new instant."""
        delta = _dt.timedelta(seconds=seconds, minutes=minutes, hours=hours,
                              days=days)
        if delta < _dt.timedelta(0):
            raise ValueError("FrozenClock only moves forward")
        self._now = self._now + delta
        return self._now

    def set(self, moment: _dt.datetime) -> _dt.datetime:
        """Jump to an explicit instant (for restart and skew scenarios)."""
        if moment.tzinfo is None:
            raise ValueError("FrozenClock requires an aware datetime")
        self._now = moment.astimezone(UTC)
        return self._now


# --------------------------------------------------------------------------- #
# Instant <-> storage conversions
# --------------------------------------------------------------------------- #
def to_micros(moment: _dt.datetime) -> int:
    """Convert an aware datetime to integer UTC microseconds for storage."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime cannot be stored as a UTC instant")
    return int(moment.astimezone(UTC).timestamp() * MICROS)


def from_micros(value: int) -> _dt.datetime:
    """Restore an aware UTC datetime from integer microseconds."""
    return _dt.datetime.fromtimestamp(value / MICROS, tz=UTC)


def to_rfc3339(moment: _dt.datetime) -> str:
    """Render an instant as RFC3339 ending in Z, for public JSON."""
    text = moment.astimezone(UTC).isoformat(timespec="seconds")
    return text.replace("+00:00", "Z")


def parse_rfc3339(text: str) -> _dt.datetime:
    """Parse an RFC3339 timestamp, accepting the Z suffix."""
    return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def local_date(moment: _dt.datetime, timezone: str) -> str:
    """The local calendar date of an instant, as an ISO string."""
    return moment.astimezone(ZoneInfo(timezone)).date().isoformat()
