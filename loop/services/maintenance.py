"""Operational maintenance that runs against real stored state (O10–O12).

The logic for these three lived in `loop/ops/` with no caller at all:
`rebuild_index`, `plan_retention` and `LoadReport` were imported by nothing
outside their own tests. Tests against a disconnected helper prove the helper,
not the system — R0 found exactly that, and this module is the other half.

Each operation here touches the same stores the running service uses, and each
is reachable from `loop-next maintenance`.
"""

from __future__ import annotations

import logging
import platform
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.ids import new_id
from loop.ops.perf import LoadReport, measure
from loop.ops.retention import Category, Record, RetentionPlan, plan_retention

logger = logging.getLogger(__name__)


@dataclass
class RebuildResult:
    """What a search-index rebuild recovered."""

    documents_indexed: int = 0
    before: int = 0
    after: int = 0
    sources_modified: list[str] = field(default_factory=list)

    @property
    def sources_untouched(self) -> bool:
        return not self.sources_modified


def rebuild_search_index(knowledge: Any) -> RebuildResult:
    """Rebuild the real FTS index from the vault (O10).

    The vault is the authority; the index is derived. So this reads every
    authorised document and rewrites the index, and never writes back — a
    rebuild that repaired a *source* from an index would be inventing content
    from a summary of it.
    """
    before = knowledge.search.count()
    fingerprint = _vault_fingerprint(knowledge)

    indexed = knowledge.reindex()

    modified = [path for path, digest in _vault_fingerprint(knowledge).items()
                if fingerprint.get(path) != digest]
    return RebuildResult(documents_indexed=indexed, before=before,
                         after=knowledge.search.count(),
                         sources_modified=sorted(modified))


def _vault_fingerprint(knowledge: Any) -> dict[str, str]:
    from loop.core.ids import content_hash

    root = knowledge.gateway.root
    return {path.relative_to(root).as_posix(): content_hash(path.read_bytes())
            for path in sorted(root.rglob("*.md"))}


# --------------------------------------------------------------------------- #
# Retention (O11)
# --------------------------------------------------------------------------- #
#: Where expirable operational state actually lives: table, primary key, the
#: column that says when it expires, and its category. Verified against the
#: live schema before use — a column named here that does not exist is a bug
#: in this table, not a reason to crash maintenance.
RETENTION_SOURCES = (
    ("observations", "id", "expires_at", Category.OPERATIONAL),
    ("callback_actions", "id", "expires_at", Category.OPERATIONAL),
    ("trigger_firings", "id", None, Category.OPERATIONAL),
)


@dataclass
class RetentionResult:
    plan: RetentionPlan
    removed: dict[str, int] = field(default_factory=dict)

    @property
    def total_removed(self) -> int:
        return sum(self.removed.values())


class MaintenanceService:
    """Retention, index rebuild and load measurement over real state."""

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()

    # ------------------------------------------------------------------ #
    # Retention
    # ------------------------------------------------------------------ #
    def collect_records(self) -> list[Record]:
        """Every expirable operational row, with what pins it."""
        now = to_micros(self._clock.now())
        del now
        records: list[Record] = []
        with self._sessions() as session:
            for table, key, expires, category in RETENTION_SOURCES:
                if not self._table_exists(session, table):
                    continue
                columns_present = self._columns(session, table)
                if key not in columns_present:
                    logger.warning("Retention skipped %s: no %s column",
                                   table, key)
                    continue
                if expires and expires not in columns_present:
                    logger.warning("Retention skipped %s: no %s column",
                                   table, expires)
                    continue
                columns = f"{key}" + (f", {expires}" if expires else "")
                rows = session.execute(
                    text(f"SELECT {columns} FROM {table}")).all()
                for row in rows:
                    expires_at = (int(row[1]) // 1_000_000
                                  if expires and row[1] is not None else None)
                    records.append(Record(
                        id=f"{table}:{row[0]}", category=category,
                        expires_at=expires_at))
        return records

    def active_references(self) -> set[str]:
        """Anything live work still depends on, so expiry does not remove it.

        Expiry is a hint about age; a reference is a fact about use. A queued
        job's evidence must outlive its nominal expiry or the job wakes to
        nothing.
        """
        active: set[str] = set()
        with self._sessions() as session:
            if self._table_exists(session, "jobs"):
                rows = session.execute(text(
                    "SELECT payload_json FROM jobs WHERE state IN "
                    "('queued','running','retry_wait')")).all()
                for row in rows:
                    active.add(str(row[0]))
        return active

    def run_retention(self, *, apply: bool = False) -> RetentionResult:
        """Plan, and optionally remove. Planning never deletes anything."""
        now = int(self._clock.now().timestamp())
        plan = plan_retention(self.collect_records(), now=now,
                              active_refs=self.active_references())
        result = RetentionResult(plan=plan)
        if not apply:
            return result

        with self._sessions() as session:
            for identifier in plan.remove:
                table, _, key = identifier.partition(":")
                column = next((k for t, k, _e, _c in RETENTION_SOURCES
                               if t == table), None)
                if column is None:
                    continue
                session.execute(
                    text(f"DELETE FROM {table} WHERE {column} = :key"),
                    {"key": key})
                result.removed[table] = result.removed.get(table, 0) + 1
            session.commit()
        return result

    @staticmethod
    def _columns(session: Session, table: str) -> set[str]:
        return {row[1] for row in session.execute(
            text(f"PRAGMA table_info({table})")).all()}

    @staticmethod
    def _table_exists(session: Session, table: str) -> bool:
        return session.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = :name"),
            {"name": table}).first() is not None

    # ------------------------------------------------------------------ #
    # Load measurement (O12)
    # ------------------------------------------------------------------ #
    def measure_load(self, *, sources: int = 500,
                     jobs: int = 200) -> LoadReport:
        """Persist and claim real rows, timing each operation (O12).

        Every sample is an actual database round trip. A report constructed
        with `source_count=500` and no writes measures nothing; this one is
        slower for exactly the reason that makes it worth having.
        """
        from loop.runtime.jobs import JobQueue
        from loop.services.observations import ObservationStore

        report = LoadReport()
        report.hardware = (f"{platform.platform()} / {platform.machine()} / "
                           f"python {platform.python_version()}")

        observations = ObservationStore(sessions=self._sessions,
                                        clock=self._clock)
        queue = JobQueue(sessions=self._sessions, clock=self._clock)
        run = new_id()[:8]

        def persist(index: int) -> Callable[[], object]:
            return lambda: observations.record(
                key=f"load-{run}-{index}", subject_ref="load-test",
                value=index, ttl_seconds=3600)

        def enqueue(index: int) -> Callable[[], object]:
            return lambda: queue.enqueue(
                "load.probe", dedupe_key=f"load-{run}-{index}",
                payload={"n": index})

        def claim() -> object:
            return queue.claim(f"load-worker-{run}", kinds=["load.probe"])

        measure(report, "persist", [persist(i) for i in range(sources)])
        measure(report, "enqueue", [enqueue(i) for i in range(jobs)])
        measure(report, "claim", [claim for _ in range(jobs)])

        report.queue_depth = self._queued_jobs()
        report.source_count = sources
        return report

    def _queued_jobs(self) -> int:
        with self._sessions() as session:
            return int(session.execute(text(
                "SELECT COUNT(*) FROM jobs WHERE state IN "
                "('queued','retry_wait')")).scalar() or 0)

    def cleanup_load_rows(self) -> int:
        """Remove what a load run wrote. A benchmark is not user data."""
        removed = 0
        with self._sessions() as session:
            for statement in (
                    "DELETE FROM observations WHERE subject_ref = 'load-test'",
                    "DELETE FROM jobs WHERE kind = 'load.probe'"):
                # DML always yields a CursorResult; the annotation matches
                # what the other stores do.
                result: CursorResult[Any] = session.execute(  # type: ignore[assignment]
                    text(statement))
                removed += int(result.rowcount or 0)
            session.commit()
        return removed


def measured_seconds(callable_: Any) -> float:
    start = time.perf_counter()
    callable_()
    return time.perf_counter() - start
