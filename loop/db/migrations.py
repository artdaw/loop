"""Versioned schema migrations (runtime contract §4).

``create_all`` alone is explicitly not migration. It materialises a brand-new
database; every change to an *existing* one runs through the ordered revision
list here, recorded in ``schema_version`` so re-running is a no-op.

Design notes:

* Each revision is a named function taking a live connection. SQLite supports
  transactional DDL, so a failing revision rolls back rather than leaving the
  database half-migrated.
* Revisions are **append-only**. Editing a shipped revision would silently
  diverge databases that already applied it.
* Destructive steps take a backup first (runtime §4). The legacy migration in
  ``0002`` is additive-plus-copy for exactly this reason: the old tables are
  left in place so a failed upgrade loses nothing and can be inspected.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, Engine, inspect, text

from loop.core.clock import Clock, SystemClock, to_micros
from loop.db.models import Base

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Revision:
    """One ordered, named schema change."""

    id: str
    description: str
    upgrade: Callable[[Connection], None]


#: Legacy tables whose names collide with vNext tables. They are renamed, not
#: dropped, so a failed upgrade can still be inspected and re-run.
_COLLIDING_LEGACY_TABLES = ("tasks",)


def _r0000_rename_legacy(conn: Connection) -> None:
    """Move Phase 4 tables out of the way before creating the vNext schema.

    ``create_all`` only creates *missing* tables. A Phase 4 database already has
    a ``tasks`` table, so without this step the vNext ``tasks`` table is silently
    skipped and every migrated row is written against the old columns. Renaming
    first keeps the original data intact under ``legacy_*`` while freeing the
    name for the new schema.
    """
    inspector = inspect(conn)
    existing = set(inspector.get_table_names())
    for name in _COLLIDING_LEGACY_TABLES:
        if name not in existing:
            continue
        columns = {c["name"] for c in inspector.get_columns(name)}
        if "version" in columns:
            continue  # already the vNext shape; nothing to move
        legacy_name = f"legacy_{name}"
        if legacy_name in existing:
            continue  # a previous run already moved it
        conn.execute(text(f"ALTER TABLE {name} RENAME TO {legacy_name}"))
        logger.info("Renamed legacy table %s to %s", name, legacy_name)


def _r0001_initial(conn: Connection) -> None:
    """Create the vNext core schema."""
    Base.metadata.create_all(conn)


def _r0002_legacy_provenance(conn: Connection) -> None:
    """Record where migrated rows came from.

    Legacy rows carry no vNext provenance. Runtime §4 forbids inventing a
    package version for them, so they get an explicit marker instead of a
    plausible-looking lie.
    """
    inspector = inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("tasks")}
    if "legacy_source" not in columns:
        conn.execute(text("ALTER TABLE tasks ADD COLUMN legacy_source VARCHAR(64)"))


#: Ordered revisions. Append only; never edit a shipped entry.
REVISIONS: tuple[Revision, ...] = (
    Revision("0000_rename_legacy", "move colliding Phase 4 tables aside",
             _r0000_rename_legacy),
    Revision("0001_initial", "vNext core schema", _r0001_initial),
    Revision("0002_legacy_provenance", "explicit legacy provenance marker",
             _r0002_legacy_provenance),
)

HEAD = REVISIONS[-1].id


def applied_revisions(engine: Engine) -> set[str]:
    """Which revisions this database has already applied."""
    inspector = inspect(engine)
    if "schema_version" not in inspector.get_table_names():
        return set()
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT revision FROM schema_version")).all()
    return {row[0] for row in rows}


def pending_revisions(engine: Engine) -> list[Revision]:
    """Revisions not yet applied, in order."""
    done = applied_revisions(engine)
    return [rev for rev in REVISIONS if rev.id not in done]


def backup_database(engine: Engine, *, suffix: str = ".pre-migration") -> Path | None:
    """Copy a file-backed SQLite database before a destructive change.

    Returns the backup path, or ``None`` for in-memory databases where there is
    nothing to copy.
    """
    url = engine.url
    if url.database in (None, ":memory:"):
        return None
    source = Path(url.database)
    if not source.exists():
        return None
    target = source.with_name(source.name + suffix)
    shutil.copy2(source, target)
    logger.info("Backed up %s to %s before migrating", source, target)
    return target


def migrate(engine: Engine, *, clock: Clock | None = None,
            dry_run: bool = False, backup: bool = True) -> list[str]:
    """Apply pending revisions in order. Returns the revision IDs applied.

    ``dry_run`` reports what would run and changes nothing — the default for the
    CLI's ``loop migrate`` (interfaces §3).
    """
    clock = clock or SystemClock()
    pending = pending_revisions(engine)
    if not pending:
        return []
    if dry_run:
        return [rev.id for rev in pending]

    # The bookkeeping table must exist before the first revision can record
    # itself, and it is not owned by any single revision.
    _ensure_schema_version_table(engine)

    if backup and applied_revisions(engine):
        # Only back up an already-initialised database; a fresh file has
        # nothing to lose.
        backup_database(engine)

    applied: list[str] = []
    for revision in pending:
        # One transaction per revision: a failure rolls that revision back
        # rather than leaving the schema half-changed.
        with engine.begin() as conn:
            logger.info("Applying migration %s (%s)", revision.id,
                        revision.description)
            revision.upgrade(conn)
            conn.execute(
                text("INSERT INTO schema_version (revision, applied_at) "
                     "VALUES (:rev, :at)"),
                {"rev": revision.id, "at": to_micros(clock.now())},
            )
        applied.append(revision.id)
    return applied


def _ensure_schema_version_table(engine: Engine) -> None:
    """Create the revision bookkeeping table if it is missing."""
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "revision VARCHAR(64) PRIMARY KEY, applied_at BIGINT NOT NULL)"
        ))


def current_revision(engine: Engine) -> str | None:
    """The latest applied revision, or ``None`` on an unmigrated database."""
    done = applied_revisions(engine)
    for revision in reversed(REVISIONS):
        if revision.id in done:
            return revision.id
    return None
