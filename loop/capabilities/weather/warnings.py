"""Official warning lifecycle and state (weather §5).

The asymmetry here is the whole point. A fresh, complete, empty official feed
supports "no active warnings in this feed" — a statement about the feed. It does
not support "safe". And a feed that is missing, stale or partial supports
neither: warning state is *unknown*, and reporting no warnings because the fetch
failed is the one error a user acts on (WF12).

Cancellation needs positive evidence. Silence during an outage cannot clear an
alert (WF13), and neither can age: a storm warning issued yesterday and valid
until tomorrow is still in force, so the model-run staleness rule that governs
forecasts must not be applied to alerts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class WarningStatus(str, Enum):
    ACTUAL = "actual"
    UPDATE = "update"
    CANCEL = "cancel"
    EXPIRED = "expired"


class WarningState(str, Enum):
    """What we can honestly say about warnings for a location."""

    ACTIVE = "active"
    NONE_IN_CHECKED_FEED = "none_in_checked_feed"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    EXTREME = "extreme"
    SEVERE = "severe"
    MODERATE = "moderate"
    MINOR = "minor"
    UNKNOWN = "unknown"


SEVERITY_ORDER = {Severity.UNKNOWN: 0, Severity.MINOR: 1, Severity.MODERATE: 2,
                  Severity.SEVERE: 3, Severity.EXTREME: 4}


@dataclass
class OfficialWarning:
    """An alert as the publisher issued it. Wording is preserved, not rewritten."""

    publisher: str
    provider_alert_id: str
    message_id: str
    event_type: str
    effective_at: int
    expires_at: int | None = None
    onset_at: int | None = None
    severity: Severity = Severity.UNKNOWN
    original_severity: str = ""
    certainty: str = ""
    urgency: str = ""
    instruction: str = ""
    headline: str = ""
    source_url: str = ""
    status: WarningStatus = WarningStatus.ACTUAL
    references: tuple[str, ...] = ()
    geometry: tuple[float, float, float, float] | None = None
    fetched_at: int = 0
    issued_at: int | None = None

    @property
    def identity(self) -> tuple[str, str]:
        """Updates and cancellations share the issuer's alert identity."""
        return (self.publisher, self.provider_alert_id)

    def covers_point(self, latitude: float, longitude: float) -> bool:
        if self.geometry is None:
            return False
        min_lat, min_lon, max_lat, max_lon = self.geometry
        return min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon

    def covers_time(self, start: int, end: int) -> bool:
        """Overlap, not containment: a warning starting mid-commute applies."""
        if end < self.effective_at:
            return False
        return not (self.expires_at is not None and start > self.expires_at)

    def is_valid_at(self, now: int) -> bool:
        """Validity comes from the publisher's lifecycle, never from issue age.

        A still-effective warning survives any number of fresh feed checks; the
        forecast model-run staleness rule does not apply to alerts (WF13).
        """
        if self.status is WarningStatus.CANCEL:
            return False
        if now < self.effective_at:
            return False
        return self.expires_at is None or now < self.expires_at

    def material_revision(self) -> tuple:
        """What makes an update worth telling the user about again (WF14)."""
        return (self.status, self.severity, self.instruction, self.expires_at)


@dataclass
class FeedRead:
    """The outcome of reading one publisher's warning feed."""

    publisher: str
    ok: bool
    fetched_at: int
    warnings: list[OfficialWarning] = field(default_factory=list)
    #: True only when the publisher documents whole-snapshot semantics.
    complete_snapshot: bool = False
    partial_reason: str = ""
    error: str = ""

    @property
    def usable(self) -> bool:
        return self.ok and not self.partial_reason


def warning_state(reads: list[FeedRead], *, now: int, latitude: float,
                  longitude: float, window: tuple[int, int],
                  max_feed_age_seconds: int = 300,
                  ) -> tuple[WarningState, list[OfficialWarning], list[str]]:
    """Determine what can honestly be said about warnings (WF11, WF12).

    Returns ``(state, applicable, caveats)``. The empty-warnings answer is only
    reachable through a feed that succeeded, was complete, and was fresh.
    """
    caveats: list[str] = []
    if not reads:
        return WarningState.UNKNOWN, [], ["no warning feed was checked"]

    applicable: list[OfficialWarning] = []
    healthy = False
    start, end = window

    for read in reads:
        if not read.ok:
            caveats.append(f"{read.publisher}: {read.error or 'feed unavailable'}")
            continue
        if now - read.fetched_at > max_feed_age_seconds:
            caveats.append(f"{read.publisher}: feed data is stale")
            continue
        if read.partial_reason:
            caveats.append(f"{read.publisher}: partial feed ({read.partial_reason})")
            # A partial feed can still prove a warning exists; it can never
            # prove one does not.
            healthy = healthy or False
        else:
            healthy = True

        for warning in read.warnings:
            if not warning.is_valid_at(now):
                continue
            if not warning.covers_point(latitude, longitude):
                continue
            if not warning.covers_time(start, end):
                continue
            applicable.append(warning)

    if applicable:
        return WarningState.ACTIVE, _latest_per_identity(applicable), caveats
    if healthy:
        return WarningState.NONE_IN_CHECKED_FEED, [], caveats
    return WarningState.UNKNOWN, [], caveats or ["warning state could not be established"]


def _latest_per_identity(warnings: list[OfficialWarning]) -> list[OfficialWarning]:
    """One entry per alert identity, keeping the newest message."""
    newest: dict[tuple[str, str], OfficialWarning] = {}
    for warning in warnings:
        current = newest.get(warning.identity)
        if current is None or (warning.issued_at or 0) >= (current.issued_at or 0):
            newest[warning.identity] = warning
    return [newest[key] for key in sorted(newest)]


def apply_lifecycle(known: dict[tuple[str, str], OfficialWarning],
                    read: FeedRead, *, now: int
                    ) -> dict[tuple[str, str], OfficialWarning]:
    """Fold one feed read into known alert state (WF13).

    An alert is removed only by an explicit cancellation, by its own expiry, or
    by absence from a feed the publisher documents as a complete snapshot. A
    failed or partial read changes nothing — it is not evidence of anything.
    """
    updated = dict(known)

    if not read.usable:
        return updated

    for warning in read.warnings:
        existing = updated.get(warning.identity)
        if warning.status is WarningStatus.CANCEL:
            updated.pop(warning.identity, None)
            continue
        if existing is not None and (warning.issued_at or 0) < (existing.issued_at or 0):
            continue                      # an older message never overwrites a newer
        updated[warning.identity] = warning

    if read.complete_snapshot:
        seen = {w.identity for w in read.warnings}
        for identity in list(updated):
            if identity[0] == read.publisher and identity not in seen:
                updated.pop(identity)

    for identity, warning in list(updated.items()):
        if not warning.is_valid_at(now) and warning.expires_at is not None \
                and now >= warning.expires_at:
            updated.pop(identity)

    return updated


@dataclass
class WarningDelivery:
    """What has already been delivered, per issuer/alert/destination (WF14)."""

    delivered: dict[tuple[str, str, str], tuple] = field(default_factory=dict)

    def should_deliver(self, warning: OfficialWarning, *, destination: str) -> bool:
        """Deduplicate the same warning, but let real changes through.

        Weather and travel both process the same alert; the user should hear it
        once. A severity increase, a changed instruction or a cancellation is a
        different message, not a repeat.
        """
        key = (warning.publisher, warning.provider_alert_id, destination)
        previous = self.delivered.get(key)
        if previous is None:
            return True
        return warning.material_revision() != previous

    def record(self, warning: OfficialWarning, *, destination: str) -> None:
        key = (warning.publisher, warning.provider_alert_id, destination)
        self.delivered[key] = warning.material_revision()


@dataclass
class WarningSubscription:
    """An immediate-warning subscription. Inactive until explicitly activated."""

    location_ref: str
    categories: tuple[str, ...]
    expires_at: int | None
    activation_event_id: str | None = None
    quiet_hours_exception: bool = False

    @property
    def is_active(self) -> bool:
        return bool(self.activation_event_id)


def notification_fields(subscription: WarningSubscription,
                        warning: OfficialWarning) -> dict[str, Any]:
    """How a warning should be offered to the Notification Manager (WF20).

    Activation grants *delivery* authority: the alert is not a discretionary
    suggestion competing for a daily slot. It does not grant *timing* authority.
    The user chose the trigger ("tell me about severe weather"), not the moment,
    so ordinary quiet-hours policy still applies unless they separately asked
    to be woken. "Official" describes the issuer, not the user's consent.
    """
    if not subscription.is_active:
        return {"category": "discretionary", "timing_chosen_by_user": False,
                "reason": "warning subscription is not activated"}
    return {
        "category": "requested_routine",
        "timing_chosen_by_user": subscription.quiet_hours_exception,
        "reason": f"{warning.publisher} {warning.event_type} warning",
    }


def honours_quiet_hours(subscription: WarningSubscription) -> bool:
    """True when ordinary quiet-hours policy still applies to this subscription."""
    return not subscription.quiet_hours_exception
