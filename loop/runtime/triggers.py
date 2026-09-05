"""Trigger definitions, occurrences and DST resolution (runtime §6–§7).

Wall-clock scheduling is where "add 24 hours" quietly breaks twice a year. Every
future occurrence of a local schedule is therefore computed with ``zoneinfo``
against the local calendar, never by adding a fixed offset to a UTC instant.

The two boundaries the contract calls out:

* **Spring gap** — 02:30 on a day where 02:00→03:00 does not exist. Shift
  forward by the gap (02:30 → 03:30), because skipping the day would silently
  drop a reminder the user asked for.
* **Autumn fold** — 02:30 on a day where it happens twice. Use the first
  occurrence (``fold=0``) *once*. Firing both would deliver the same reminder
  twice for one nominal time.

Occurrence keys make exactly-once processing possible: the key encodes the local
date, wall time, timezone and fold, so a restart, a catch-up sweep and a
duplicate poll all resolve to the same row and the unique constraint absorbs the
duplicates.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import UTC, Clock, SystemClock, from_micros, to_micros
from loop.core.errors import InvalidInput
from loop.core.ids import new_id
from loop.core.privacy import PrivacyLabel

logger = logging.getLogger(__name__)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: Explicit one-shot reminders still fire after a restart within this window,
#: labelled delayed; older ones go to a catch-up digest instead (runtime §7).
CATCH_UP_WINDOW_SECONDS = 24 * 3600

#: Discretionary routines default to skipping if more than this late.
ROUTINE_SKIP_AFTER_SECONDS = 30 * 60


@dataclass(frozen=True)
class Occurrence:
    """One resolved firing of a trigger."""

    key: str
    nominal_at: dt.datetime
    effective_at: dt.datetime
    dst_resolution: str = "normal"   # normal | gap_shifted | fold_first

    @property
    def was_adjusted(self) -> bool:
        return self.dst_resolution != "normal"


def resolve_local_time(local_date: dt.date, wall_time: dt.time,
                       timezone: str) -> tuple[dt.datetime, str]:
    """Resolve a local wall-clock time to a UTC instant, handling DST.

    Returns the instant and which rule applied, so the resolution can be stored
    and explained rather than silently applied.
    """
    zone = ZoneInfo(timezone)
    naive = dt.datetime.combine(local_date, wall_time)

    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)

    # Both a gap and a fold give fold=0/fold=1 different offsets, so the offset
    # comparison alone cannot tell them apart. The round trip can: a *fold* time
    # exists (it comes back unchanged), a *gap* time does not (it comes back
    # shifted past the discontinuity).
    round_tripped = first.astimezone(UTC).astimezone(zone)
    if round_tripped.time() != wall_time:
        # Nonexistent local time. The round trip has already landed on the
        # correct shifted wall clock (02:30 → 03:30), so use it directly rather
        # than recomputing the gap width.
        return first.astimezone(UTC), "gap_shifted"

    if first.utcoffset() != second.utcoffset():
        # Ambiguous: the wall time happens twice. Take the first (runtime §7).
        return first.astimezone(UTC), "fold_first"

    return first.astimezone(UTC), "normal"


@dataclass
class Trigger:
    """A stored trigger, detached from the session."""

    id: str
    subject_type: str
    subject_id: str
    kind: str
    definition: dict[str, Any]
    enabled: bool
    revision: int
    next_fire_at: int | None = None
    last_fire_at: int | None = None
    expires_at: int | None = None


class TriggerService:
    """Creates triggers and computes their occurrences."""

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()

    # ------------------------------------------------------------------ #
    # Creation
    # ------------------------------------------------------------------ #
    def create_at(self, *, subject_type: str, subject_id: str,
                  instant: dt.datetime, timezone: str,
                  original_local: str | None = None, catch_up: bool = True,
                  privacy: PrivacyLabel | None = None,
                  session: Session | None = None) -> Trigger:
        """Create a one-shot trigger for an exact instant."""
        definition = {
            "instant_utc": to_micros(instant),
            "timezone": timezone,
            "original_local": original_local,
            "catch_up": catch_up,
        }
        return self._insert(subject_type=subject_type, subject_id=subject_id,
                            kind="at", definition=definition,
                            next_fire_at=to_micros(instant), privacy=privacy,
                            session=session)

    def create_local_schedule(self, *, subject_type: str, subject_id: str,
                              days: list[str], at: str, timezone: str,
                              catch_up: str = "skip",
                              grace_seconds: int = 1800,
                              privacy: PrivacyLabel | None = None,
                              session: Session | None = None) -> Trigger:
        """Create a recurring local wall-clock schedule."""
        unknown = [d for d in days if d not in DAYS]
        if unknown:
            raise InvalidInput(f"unknown days: {unknown}")
        try:
            hour, minute = (int(part) for part in at.split(":"))
            wall = dt.time(hour, minute)
        except (ValueError, TypeError) as exc:
            raise InvalidInput(f"invalid time {at!r}; expected HH:MM") from exc

        definition = {"days": days, "at": at, "timezone": timezone,
                      "catch_up": catch_up, "grace_seconds": grace_seconds}
        next_occurrence = self.next_occurrence_after(
            self._clock.now(), days=days, wall_time=wall, timezone=timezone)
        return self._insert(
            subject_type=subject_type, subject_id=subject_id,
            kind="local_schedule", definition=definition,
            next_fire_at=to_micros(next_occurrence.effective_at),
            privacy=privacy, session=session)

    def _insert(self, *, subject_type: str, subject_id: str, kind: str,
                definition: dict[str, Any], next_fire_at: int | None,
                privacy: PrivacyLabel | None,
                session: Session | None) -> Trigger:
        now = to_micros(self._clock.now())
        trigger = Trigger(
            id=new_id(), subject_type=subject_type, subject_id=subject_id,
            kind=kind, definition=definition, enabled=True, revision=1,
            next_fire_at=next_fire_at,
        )
        statement = text(
            "INSERT INTO triggers (id, version, created_at, updated_at, "
            "subject_type, subject_id, kind, definition_json, enabled, revision, "
            "next_fire_at, last_fire_at, expires_at, privacy) "
            "VALUES (:id, 1, :now, :now, :subject_type, :subject_id, :kind, "
            ":definition, 1, 1, :next_fire_at, NULL, NULL, :privacy)")
        params = {
            "id": trigger.id, "now": now, "subject_type": subject_type,
            "subject_id": subject_id, "kind": kind,
            "definition": json.dumps(definition, sort_keys=True),
            "next_fire_at": next_fire_at,
            "privacy": json.dumps(
                (privacy or PrivacyLabel.for_unlabelled_import()).to_json()),
        }
        if session is not None:
            session.execute(statement, params)
        else:
            with self._sessions() as own:
                own.execute(statement, params)
                own.commit()
        return trigger

    # ------------------------------------------------------------------ #
    # Occurrences
    # ------------------------------------------------------------------ #
    def next_occurrence_after(self, moment: dt.datetime, *, days: list[str],
                              wall_time: dt.time, timezone: str) -> Occurrence:
        """The first scheduled occurrence strictly after ``moment``."""
        zone = ZoneInfo(timezone)
        local_now = moment.astimezone(zone)
        wanted = {DAYS.index(day) for day in days}

        # 8 days covers a full week plus today, whatever the starting weekday.
        for offset in range(0, 9):
            candidate_date = (local_now + dt.timedelta(days=offset)).date()
            if candidate_date.weekday() not in wanted:
                continue
            instant, resolution = resolve_local_time(candidate_date, wall_time,
                                                     timezone)
            if instant > moment:
                return Occurrence(
                    key=occurrence_key_for_schedule(candidate_date, wall_time,
                                                    timezone, resolution),
                    nominal_at=instant, effective_at=instant,
                    dst_resolution=resolution,
                )
        raise InvalidInput("no occurrence found in the next 8 days")

    def due_triggers(self, *, limit: int = 100) -> list[Trigger]:
        """Enabled triggers whose next firing is due now."""
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            rows = session.execute(text(
                "SELECT id, subject_type, subject_id, kind, definition_json, "
                "enabled, revision, next_fire_at, last_fire_at, expires_at "
                "FROM triggers WHERE enabled = 1 AND next_fire_at IS NOT NULL "
                "AND next_fire_at <= :now ORDER BY next_fire_at LIMIT :limit"
            ), {"now": now, "limit": limit}).all()
        return [
            Trigger(id=r[0], subject_type=r[1], subject_id=r[2], kind=r[3],
                    definition=json.loads(r[4]), enabled=bool(r[5]),
                    revision=r[6], next_fire_at=r[7], last_fire_at=r[8],
                    expires_at=r[9])
            for r in rows
        ]

    def claim_occurrence(self, trigger: Trigger, occurrence_key: str, *,
                         nominal_at: dt.datetime,
                         effective_at: dt.datetime | None = None) -> str | None:
        """Record a firing, or return ``None`` if it already exists.

        The unique constraint is the exactly-once mechanism: a concurrent worker,
        a restart replay and a duplicate poll all collide here rather than
        producing a second reminder.
        """
        from sqlalchemy.exc import IntegrityError

        firing_id = new_id()
        effective = effective_at or nominal_at
        try:
            with self._sessions() as session:
                session.execute(text(
                    "INSERT INTO trigger_firings (id, trigger_id, "
                    "trigger_revision, occurrence_key, nominal_at, effective_at, "
                    "status, created_at) VALUES (:id, :trigger_id, :revision, "
                    ":key, :nominal, :effective, 'queued', :now)"
                ), {
                    "id": firing_id, "trigger_id": trigger.id,
                    "revision": trigger.revision, "key": occurrence_key,
                    "nominal": to_micros(nominal_at),
                    "effective": to_micros(effective),
                    "now": to_micros(self._clock.now()),
                })
                session.commit()
        except IntegrityError:
            # Only a uniqueness collision means "already claimed". A foreign-key
            # or constraint failure is a real error and must not be swallowed as
            # a benign duplicate, which would hide the firing entirely.
            with self._sessions() as session:
                existing = session.execute(text(
                    "SELECT id FROM trigger_firings WHERE trigger_id = :tid "
                    "AND trigger_revision = :rev AND occurrence_key = :key"
                ), {"tid": trigger.id, "rev": trigger.revision,
                    "key": occurrence_key}).first()
            if existing is None:
                raise
            logger.info("Occurrence %s of trigger %s already claimed",
                        occurrence_key, trigger.id)
            return None
        return firing_id

    # ------------------------------------------------------------------ #
    # Catch-up
    # ------------------------------------------------------------------ #
    def catch_up_decision(self, trigger: Trigger, *,
                          now: dt.datetime | None = None) -> str:
        """Decide what to do with an occurrence that is already late.

        Returns ``fire``, ``fire_delayed``, ``digest`` or ``skip``.
        """
        moment = now or self._clock.now()
        if trigger.next_fire_at is None:
            return "skip"
        lateness = (moment - from_micros(trigger.next_fire_at)).total_seconds()
        if lateness <= 0:
            return "fire"

        if trigger.kind == "at":
            if not trigger.definition.get("catch_up", True):
                return "skip"
            # An explicit reminder the user asked for still fires within a day,
            # labelled delayed; beyond that it becomes digest material rather
            # than a surprise alert about something long past (D09).
            return "fire_delayed" if lateness <= CATCH_UP_WINDOW_SECONDS else "digest"

        policy = str(trigger.definition.get("catch_up", "skip"))
        if policy == "skip":
            # Discretionary routines are useless once stale: a weather forecast
            # for a departure time that has passed is noise (D10).
            return "skip" if lateness > ROUTINE_SKIP_AFTER_SECONDS else "fire"
        return "fire_delayed" if lateness <= CATCH_UP_WINDOW_SECONDS else "skip"


# --------------------------------------------------------------------------- #
# Occurrence keys
# --------------------------------------------------------------------------- #
def occurrence_key_for_schedule(local_date: dt.date, wall_time: dt.time,
                                timezone: str, resolution: str = "normal") -> str:
    """Key for a recurring local schedule occurrence.

    Includes the fold resolution so the two halves of an ambiguous hour cannot
    collapse into one key by accident — and so the *same* nominal time resolved
    the same way always produces the same key.
    """
    fold = "1" if resolution == "fold_second" else "0"
    return f"{local_date.isoformat()}T{wall_time.strftime('%H:%M')}@{timezone}#{fold}"


def occurrence_key_for_one_shot(trigger_id: str) -> str:
    """One-shot triggers have exactly one occurrence: the trigger itself."""
    return trigger_id
