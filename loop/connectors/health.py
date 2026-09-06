"""Connector health and independent degradation (runtime §4, interfaces §5).

Each connector's health is tracked separately, because the alternative — one
"integrations are fine" flag — turns a single provider outage into a system-wide
outage. When Google Calendar answers and Outlook does not, the correct result is
*Google's events plus an explicit gap*, never an empty diary (P10).

The distinction that matters most: **unavailable is not empty**. A calendar that
could not be read has unknown contents. Reporting "no meetings" because the
request failed is a confident false statement, and it is the one users act on.

Outage notices are per *episode*, not per failure. A provider failing every
sixty seconds must not produce a notification every sixty seconds (P11): the
first failure notifies, subsequent ones update status, and recovery closes the
episode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros

logger = logging.getLogger(__name__)


class Health(str, Enum):
    READY = "ready"
    UNCONFIGURED = "unconfigured"
    AUTH_REQUIRED = "auth_required"
    DEGRADED = "degraded"
    OFFLINE = "offline"

    @property
    def is_usable(self) -> bool:
        return self is Health.READY

    @property
    def needs_attention(self) -> bool:
        return self in (Health.AUTH_REQUIRED, Health.OFFLINE, Health.DEGRADED)


@dataclass
class ConnectorState:
    """One connector's health and outage episode."""

    connector_id: str
    health: Health = Health.UNCONFIGURED
    last_success_at: int | None = None
    last_attempt_at: int | None = None
    last_error_code: str | None = None
    consecutive_failures: int = 0
    #: Set when an outage episode began and its notice was sent.
    episode_started_at: int | None = None
    episode_notified: bool = False


@dataclass
class ProviderResult:
    """One provider's contribution to a merged read."""

    connector_id: str
    ok: bool
    items: list[Any] = field(default_factory=list)
    error_code: str | None = None


@dataclass
class MergedRead:
    """Results from several providers, with gaps stated rather than hidden."""

    items: list[Any] = field(default_factory=list)
    succeeded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.failed

    @property
    def has_gap(self) -> bool:
        """True when some provider could not be read.

        A caller must not present these results as exhaustive.
        """
        return bool(self.failed)

    def describe_gap(self) -> str:
        if not self.failed:
            return ""
        names = ", ".join(sorted(self.failed))
        return (f"Could not read {names}; results may be incomplete. "
                "This is not the same as having nothing scheduled.")


class ConnectorRegistry:
    """Persists per-connector health independently."""

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS connector_state ("
                "connector_id VARCHAR(64) PRIMARY KEY, cursor_json TEXT NOT NULL "
                "DEFAULT '{}', last_attempt_at BIGINT, last_success_at BIGINT, "
                "last_error_code VARCHAR(32), health VARCHAR(16) NOT NULL, "
                "consecutive_failures INTEGER NOT NULL DEFAULT 0, "
                "episode_started_at BIGINT, episode_notified INTEGER NOT NULL "
                "DEFAULT 0)"))
            session.commit()

    def get(self, connector_id: str) -> ConnectorState:
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT connector_id, health, last_success_at, last_attempt_at, "
                "last_error_code, consecutive_failures, episode_started_at, "
                "episode_notified FROM connector_state WHERE connector_id = :id"),
                {"id": connector_id}).first()
        if row is None:
            return ConnectorState(connector_id=connector_id)
        return ConnectorState(
            connector_id=row[0], health=Health(row[1]), last_success_at=row[2],
            last_attempt_at=row[3], last_error_code=row[4],
            consecutive_failures=row[5], episode_started_at=row[6],
            episode_notified=bool(row[7]))

    def _upsert(self, state: ConnectorState) -> None:
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO connector_state (connector_id, health, "
                "last_success_at, last_attempt_at, last_error_code, "
                "consecutive_failures, episode_started_at, episode_notified) "
                "VALUES (:id, :health, :success, :attempt, :code, :failures, "
                ":episode, :notified) ON CONFLICT(connector_id) DO UPDATE SET "
                "health = :health, last_success_at = :success, "
                "last_attempt_at = :attempt, last_error_code = :code, "
                "consecutive_failures = :failures, episode_started_at = :episode, "
                "episode_notified = :notified"),
                {"id": state.connector_id, "health": state.health.value,
                 "success": state.last_success_at, "attempt": state.last_attempt_at,
                 "code": state.last_error_code,
                 "failures": state.consecutive_failures,
                 "episode": state.episode_started_at,
                 "notified": 1 if state.episode_notified else 0})
            session.commit()

    # ------------------------------------------------------------------ #
    # Outcomes
    # ------------------------------------------------------------------ #
    def record_success(self, connector_id: str) -> ConnectorState:
        """A success closes any open outage episode."""
        now = to_micros(self._clock.now())
        state = self.get(connector_id)
        state.health = Health.READY
        state.last_success_at = now
        state.last_attempt_at = now
        state.last_error_code = None
        state.consecutive_failures = 0
        state.episode_started_at = None
        state.episode_notified = False
        self._upsert(state)
        return state

    def record_failure(self, connector_id: str, *, error_code: str,
                       health: Health = Health.OFFLINE) -> ConnectorState:
        """Record a failure, starting an episode if one is not already open."""
        now = to_micros(self._clock.now())
        state = self.get(connector_id)
        state.health = health
        state.last_attempt_at = now
        state.last_error_code = error_code
        state.consecutive_failures += 1
        if state.episode_started_at is None:
            state.episode_started_at = now
        self._upsert(state)
        return state

    def record_unconfigured(self, connector_id: str) -> ConnectorState:
        state = self.get(connector_id)
        state.health = Health.UNCONFIGURED
        self._upsert(state)
        return state

    # ------------------------------------------------------------------ #
    # Notification policy (P11)
    # ------------------------------------------------------------------ #
    def should_notify_outage(self, connector_id: str) -> bool:
        """Whether this failure warrants a standalone message.

        Only the first failure of an episode. Everything after it is a status
        update, or the user gets a notification per retry.
        """
        state = self.get(connector_id)
        if state.health is Health.READY or state.episode_started_at is None:
            return False
        return not state.episode_notified

    def mark_outage_notified(self, connector_id: str) -> None:
        state = self.get(connector_id)
        state.episode_notified = True
        self._upsert(state)

    def unhealthy(self) -> list[ConnectorState]:
        with self._sessions() as session:
            rows = session.execute(text(
                "SELECT connector_id FROM connector_state "
                "WHERE health != 'ready'")).all()
        return [self.get(r[0]) for r in rows]


def merge_provider_results(results: list[ProviderResult]) -> MergedRead:
    """Combine provider reads, keeping successes and naming failures (P10).

    Discarding everything because one provider failed loses real information;
    hiding the failure invents information. Both are kept.
    """
    merged = MergedRead()
    for result in results:
        if result.ok:
            merged.items.extend(result.items)
            merged.succeeded.append(result.connector_id)
        else:
            merged.failed[result.connector_id] = result.error_code or "unavailable"
    return merged
