"""Trips, immutable revisions and explicit selection (travel §6).

Three pointers, kept apart on purpose:

* `current_revision_id` — the newest completed proposal.
* `selected_revision_id` / `selected_option_id` — what the *user* accepted.

A new proposal marks older ones stale; it never moves the selection. That is the
whole guarantee: the plan the user is travelling on cannot change because a
background recheck produced something the system liked better. Selection is an
explicit act (TR13).

`expected_version` is required on every mutation. An async result that finishes
against a brief the user has since edited is a conflict, not a winner — and the
losing writer must be told, because a silently discarded revision looks exactly
like a completed one.

Nothing here books, pays, messages anyone, or writes a calendar event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import text

from loop.capabilities.travel.brief import TripBrief
from loop.capabilities.travel.options import Option, PlanResult
from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.errors import Conflict, InvalidInput

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


class TripStatus(str, Enum):
    PLANNING = "planning"
    NEEDS_INPUT = "needs_input"
    READY = "ready"
    NO_FEASIBLE_PLAN = "no_feasible_plan"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

    @property
    def means_booked(self) -> bool:
        """Never. `ready` means a proposal exists, not that anything is held."""
        return False


@dataclass(frozen=True)
class Revision:
    """An immutable proposal. Superseded, never edited (travel §6)."""

    id: str
    trip_id: str
    brief_version: int
    result: PlanResult
    created_at: int
    stale: bool = False

    def option(self, option_id: str) -> Option | None:
        return next((o for o in self.result.options if o.id == option_id), None)


@dataclass
class Trip:
    id: str
    title: str
    brief: TripBrief
    status: TripStatus = TripStatus.PLANNING
    version: int = 1
    current_revision_id: str = ""
    selected_revision_id: str = ""
    selected_option_id: str = ""
    monitoring_routine_id: str = ""
    project_ref: str = ""
    needs_review: bool = False

    @property
    def is_monitored(self) -> bool:
        return bool(self.monitoring_routine_id)

    @property
    def has_selection(self) -> bool:
        return bool(self.selected_revision_id and self.selected_option_id)


class TripStore:
    """Trips and their immutable revisions.

    Optionally persistent via `sessions`, for the **trip pointer record**
    only: id, brief, status, version and the current/selected revision and
    option ids. That is the state with real concurrency and authority
    stakes — which option the owner actually selected must survive a
    restart, and `version` is what `expected_version` checks against.

    Revisions themselves stay in-memory only, deliberately: see
    `_r0004_trips` in the migrations module for why a partial or lossy
    round trip of the full itinerary content would be worse than an honest
    gap. A restarted process still knows *which* option was selected; it
    does not yet know the full plan behind it without re-running the plan
    or (in a future pass) reading it back from an artifact store.
    """

    def __init__(self, *, sessions: sessionmaker[Session] | None = None,
                clock: Clock | None = None) -> None:
        self._trips: dict[str, Trip] = {}
        self._revisions: dict[str, Revision] = {}
        self._by_trip: dict[str, list[str]] = {}
        self._sessions = sessions
        self._clock = clock or SystemClock()
        if self._sessions is not None:
            self._ensure_table()
            self._load()

    # ------------------------------------------------------------------ #
    # Persistence (optional; trip pointers only — see class docstring)
    # ------------------------------------------------------------------ #
    def _ensure_table(self) -> None:
        assert self._sessions is not None
        with self._sessions() as session:
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS trips (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    brief_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    current_revision_id TEXT NOT NULL DEFAULT '',
                    selected_revision_id TEXT NOT NULL DEFAULT '',
                    selected_option_id TEXT NOT NULL DEFAULT '',
                    monitoring_routine_id TEXT NOT NULL DEFAULT '',
                    project_ref TEXT NOT NULL DEFAULT '',
                    needs_review INTEGER NOT NULL DEFAULT 0,
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL
                )"""))
            session.commit()

    def _load(self) -> None:
        assert self._sessions is not None
        with self._sessions() as session:
            rows = session.execute(text(
                "SELECT id, title, brief_json, status, version, "
                "current_revision_id, selected_revision_id, "
                "selected_option_id, monitoring_routine_id, project_ref, "
                "needs_review FROM trips")).all()
        for row in rows:
            import json

            self._trips[row[0]] = Trip(
                id=row[0], title=row[1],
                brief=TripBrief.from_json(json.loads(row[2])),
                status=TripStatus(row[3]), version=row[4],
                current_revision_id=row[5], selected_revision_id=row[6],
                selected_option_id=row[7], monitoring_routine_id=row[8],
                project_ref=row[9], needs_review=bool(row[10]))
            self._by_trip.setdefault(row[0], [])

    def _persist_trip(self, trip: Trip) -> None:
        if self._sessions is None:
            return
        import json

        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO trips (id, title, brief_json, status, version, "
                "current_revision_id, selected_revision_id, "
                "selected_option_id, monitoring_routine_id, project_ref, "
                "needs_review, created_at, updated_at) VALUES (:id, :title, "
                ":brief, :status, :version, :current_rev, :selected_rev, "
                ":selected_opt, :monitoring, :project, :needs_review, :now, "
                ":now) "
                "ON CONFLICT(id) DO UPDATE SET "
                "title = excluded.title, brief_json = excluded.brief_json, "
                "status = excluded.status, version = excluded.version, "
                "current_revision_id = excluded.current_revision_id, "
                "selected_revision_id = excluded.selected_revision_id, "
                "selected_option_id = excluded.selected_option_id, "
                "monitoring_routine_id = excluded.monitoring_routine_id, "
                "project_ref = excluded.project_ref, "
                "needs_review = excluded.needs_review, "
                "updated_at = excluded.updated_at"),
                {"id": trip.id, "title": trip.title,
                 "brief": json.dumps(trip.brief.to_json()),
                 "status": trip.status.value, "version": trip.version,
                 "current_rev": trip.current_revision_id,
                 "selected_rev": trip.selected_revision_id,
                 "selected_opt": trip.selected_option_id,
                 "monitoring": trip.monitoring_routine_id,
                 "project": trip.project_ref,
                 "needs_review": int(trip.needs_review), "now": now})
            session.commit()

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def get(self, trip_id: str) -> Trip | None:
        return self._trips.get(trip_id)

    def revision(self, revision_id: str) -> Revision | None:
        return self._revisions.get(revision_id)

    def revisions_for(self, trip_id: str) -> list[Revision]:
        return [self._revisions[rid] for rid in self._by_trip.get(trip_id, [])]

    def selected(self, trip_id: str) -> tuple[Revision | None, Option | None]:
        trip = self._require(trip_id)
        if not trip.has_selection:
            return None, None
        revision = self._revisions.get(trip.selected_revision_id)
        if revision is None:
            return None, None
        return revision, revision.option(trip.selected_option_id)

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #
    def create(self, *, trip_id: str, title: str, brief: TripBrief) -> Trip:
        trip = Trip(id=trip_id, title=title, brief=brief)
        self._trips[trip_id] = trip
        self._by_trip[trip_id] = []
        self._persist_trip(trip)
        return trip

    def add_revision(self, *, revision_id: str, trip_id: str, result: PlanResult,
                     created_at: int, expected_version: int) -> Revision:
        """Commit a proposal against the version it was planned from (TR13)."""
        trip = self._check_version(trip_id, expected_version)

        if result.brief_version != trip.brief.version:
            raise Conflict(
                "This result was planned from an earlier version of the brief "
                "and cannot be committed.",
                details={"trip_id": trip_id,
                         "result_brief_version": result.brief_version,
                         "current_brief_version": trip.brief.version})

        for existing_id in self._by_trip[trip_id]:
            existing = self._revisions[existing_id]
            self._revisions[existing_id] = Revision(
                id=existing.id, trip_id=existing.trip_id,
                brief_version=existing.brief_version, result=existing.result,
                created_at=existing.created_at, stale=True)

        revision = Revision(id=revision_id, trip_id=trip_id,
                            brief_version=result.brief_version, result=result,
                            created_at=created_at)
        self._revisions[revision_id] = revision
        self._by_trip[trip_id].append(revision_id)

        trip.current_revision_id = revision_id
        trip.version += 1
        trip.status = _status_for(result)
        if trip.has_selection and trip.selected_revision_id != revision_id:
            # The accepted plan stays visible and stays selected. It is simply
            # flagged, so the user decides whether the newer proposal is better.
            trip.needs_review = True
        self._persist_trip(trip)
        return revision

    def revise_brief(self, trip_id: str, *, expected_version: int,
                     changes: dict[str, object]) -> Trip:
        """Apply a partial brief change, bumping the brief version (TR13)."""
        trip = self._check_version(trip_id, expected_version)

        for key, value in changes.items():
            if not hasattr(trip.brief, key):
                raise InvalidInput(f"unknown brief field {key!r}")
            setattr(trip.brief, key, value)

        trip.brief.version += 1
        trip.version += 1
        for revision_id in self._by_trip[trip_id]:
            existing = self._revisions[revision_id]
            self._revisions[revision_id] = Revision(
                id=existing.id, trip_id=existing.trip_id,
                brief_version=existing.brief_version, result=existing.result,
                created_at=existing.created_at, stale=True)
        if trip.has_selection:
            trip.needs_review = True
        self._persist_trip(trip)
        return trip

    def select(self, trip_id: str, *, expected_version: int, revision_id: str,
               option_id: str) -> Trip:
        """Accept one option. Does not book and does not start monitoring."""
        trip = self._check_version(trip_id, expected_version)
        revision = self._revisions.get(revision_id)
        if revision is None or revision.trip_id != trip_id:
            raise InvalidInput("that revision does not belong to this trip",
                               details={"trip_id": trip_id,
                                        "revision_id": revision_id})
        if revision.option(option_id) is None:
            raise InvalidInput("that option does not belong to this revision",
                               details={"revision_id": revision_id,
                                        "option_id": option_id})

        trip.selected_revision_id = revision_id
        trip.selected_option_id = option_id
        trip.needs_review = False
        trip.version += 1
        self._persist_trip(trip)
        return trip

    def cancel(self, trip_id: str, *, expected_version: int) -> Trip:
        """Cancel locally. External bookings are untouched (TR19)."""
        trip = self._check_version(trip_id, expected_version)
        trip.status = TripStatus.CANCELLED
        trip.monitoring_routine_id = ""
        trip.version += 1
        self._persist_trip(trip)
        return trip

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _require(self, trip_id: str) -> Trip:
        trip = self._trips.get(trip_id)
        if trip is None:
            raise InvalidInput(f"no trip {trip_id!r}")
        return trip

    def _check_version(self, trip_id: str, expected_version: int) -> Trip:
        trip = self._require(trip_id)
        if expected_version != trip.version:
            raise Conflict(
                "This trip changed since you read it.",
                details={"trip_id": trip_id, "expected_version": expected_version,
                         "current_version": trip.version})
        return trip


def _status_for(result: PlanResult) -> TripStatus:
    from loop.capabilities.travel.options import ResultStatus

    return {
        ResultStatus.READY: TripStatus.READY,
        ResultStatus.PARTIAL: TripStatus.READY,
        ResultStatus.NEEDS_INPUT: TripStatus.NEEDS_INPUT,
        ResultStatus.NO_FEASIBLE_PLAN: TripStatus.NO_FEASIBLE_PLAN,
    }[result.status]


# --------------------------------------------------------------------------- #
# Saving to the vault (TR20)
# --------------------------------------------------------------------------- #
def projection_path(project_slug: str, *, existing: set[str],
                    suffix_limit: int = 50) -> str:
    """Where a saved itinerary goes, never over a human file (travel §7).

    A colliding human-written `Itinerary.md` keeps its name; the projection takes
    a suffixed path instead. Overwriting would destroy work that was never
    Loop's to replace, and no later version history brings back what the user
    had open in another editor.
    """
    base = f"2-projects/{project_slug}/Process/Itinerary.md"
    if base not in existing:
        return base
    for index in range(2, suffix_limit + 2):
        candidate = (f"2-projects/{project_slug}/Process/"
                     f"Itinerary-loop-{index}.md")
        if candidate not in existing:
            return candidate
    raise InvalidInput("too many colliding itinerary projections")


@dataclass
class SaveManifest:
    """What a save writes, including the provenance it must carry (§7)."""

    path: str
    trip_id: str
    revision_id: str
    option_id: str
    generated_at: int
    source_timestamps: tuple[int, ...] = ()
    assumptions: tuple[str, ...] = ()
    unbooked_note: str = "No booking has been made. All segments are unbooked "\
                         "unless marked confirmed."
    creates_project: bool = False
    project_fields: dict[str, str] = field(default_factory=dict)

    def frontmatter(self) -> dict[str, object]:
        return {"loop_trip_id": self.trip_id, "revision": self.revision_id,
                "option": self.option_id, "generated_at": self.generated_at,
                "assumptions": list(self.assumptions)}
