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


def _rename_if_shape_differs(conn: Connection, name: str, *,
                             required_column: str) -> None:
    """Move a colliding pre-existing table aside, keeping its rows.

    A name collision with an older schema is invisible to `CREATE TABLE IF NOT
    EXISTS`: it succeeds, changes nothing, and every later statement written
    against the new columns fails or — worse — writes into the old ones.
    """
    inspector = inspect(conn)
    if name not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns(name)}
    if required_column in columns:
        return                      # already the vNext shape
    legacy_name = f"legacy_{name}"
    if legacy_name in set(inspector.get_table_names()):
        return                      # a previous run already moved it
    conn.execute(text(f"ALTER TABLE {name} RENAME TO {legacy_name}"))
    logger.info("Renamed colliding table %s to %s", name, legacy_name)


def _r0003_extension_state(conn: Connection) -> None:
    """Tables for state that previously lived only in process memory.

    Each of these services passed its tests and lost everything on restart,
    which for three of them is a correctness problem rather than an
    inconvenience: an unremembered spend reservation lets the daily cloud
    budget be spent twice, a forgotten notification count resets the daily cap
    at every restart, and a routine whose activation is not recorded is either
    silently inactive or silently running without recorded authority.
    """
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS routines (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            activation_event_id TEXT,
            document TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            missing_json TEXT NOT NULL DEFAULT '[]',
            privacy TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL
        )"""))

    # Phase 4 shipped its own `preferences` table with a different shape, and
    # `CREATE TABLE IF NOT EXISTS` would silently keep it — then the index below
    # fails on a column that does not exist. Same trap as revision 0000: move
    # the old table aside rather than dropping it, so its rows stay inspectable.
    _rename_if_shape_differs(conn, "preferences", required_column="state")

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS preferences (
            id TEXT PRIMARY KEY,
            key TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'global',
            value_json TEXT NOT NULL,
            state TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            observed_from BIGINT,
            observed_to BIGINT,
            sample_count INTEGER NOT NULL DEFAULT 0,
            review_after BIGINT,
            supersedes TEXT,
            rationale TEXT NOT NULL DEFAULT '',
            derivative_refs_json TEXT NOT NULL DEFAULT '[]',
            privacy TEXT,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL
        )"""))
    # One active explicit/confirmed value per key and scope (runtime §data).
    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ix_preferences_active
        ON preferences (key, scope)
        WHERE state IN ('explicit', 'confirmed')"""))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS preference_rejections (
            id TEXT PRIMARY KEY,
            subject_ref TEXT NOT NULL,
            shift_minutes INTEGER NOT NULL,
            rejected_at BIGINT NOT NULL
        )"""))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS forgotten_preferences (
            key TEXT NOT NULL,
            scope TEXT NOT NULL,
            forgotten_at BIGINT NOT NULL,
            PRIMARY KEY (key, scope)
        )"""))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS capability_schemas (
            pack_id TEXT NOT NULL,
            object_type TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            required_json TEXT NOT NULL DEFAULT '[]',
            properties_json TEXT NOT NULL DEFAULT '{}',
            effects_json TEXT NOT NULL DEFAULT '[]',
            is_latest INTEGER NOT NULL DEFAULT 1,
            migration TEXT NOT NULL DEFAULT '',
            authority_event_id TEXT NOT NULL DEFAULT '',
            created_at BIGINT NOT NULL,
            PRIMARY KEY (pack_id, object_type, schema_version)
        )"""))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS capability_objects (
            id TEXT PRIMARY KEY,
            pack_id TEXT NOT NULL,
            object_type TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            privacy TEXT,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL
        )"""))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_capability_objects_type
        ON capability_objects (pack_id, object_type)"""))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS spend_reservations (
            id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            model_id TEXT NOT NULL,
            estimated_usd REAL NOT NULL,
            actual_usd REAL,
            state TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL
        )"""))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_spend_reservations_day
        ON spend_reservations (day)"""))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS notification_deliveries (
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            subject_ref TEXT NOT NULL,
            occurrence_key TEXT NOT NULL DEFAULT '',
            local_date TEXT NOT NULL,
            sent_at BIGINT NOT NULL
        )"""))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_notification_deliveries_day
        ON notification_deliveries (local_date, category)"""))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS remote_links (
            provider TEXT NOT NULL,
            account_id TEXT NOT NULL,
            remote_id TEXT NOT NULL,
            object_type TEXT NOT NULL,
            local_id TEXT NOT NULL,
            remote_version TEXT NOT NULL DEFAULT '',
            sync_base_json TEXT NOT NULL DEFAULT '{}',
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL,
            PRIMARY KEY (provider, account_id, remote_id)
        )"""))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_remote_links_local
        ON remote_links (provider, local_id)"""))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS export_scopes (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            project_ref TEXT NOT NULL,
            object_types_json TEXT NOT NULL DEFAULT '[]',
            created_at BIGINT NOT NULL
        )"""))


def _r0004_trips(conn: Connection) -> None:
    """The trip record's authority pointers (travel §6).

    Only the pointers and the brief are stored here — `current_revision_id`,
    `selected_revision_id`/`selected_option_id`, `status`, `version`. These
    are the facts with real concurrency-control stakes: which option the
    owner actually selected must survive a restart, and `version` is what
    `expected_version` checks against.

    A revision's full itinerary content (segments, costs, schedule detail) is
    deliberately **not** given a matching table here. It has no existing
    serializer, the nested types include raw datetimes, and inventing a lossy
    round trip would be worse than an honest gap: a `Revision` read back
    missing fields other code expects is a silent wrong-shape bug, not a
    missing feature. This mirrors the coordinator's own choice not to
    checkpoint its parsed plan object (agent-stack §4) — recompute or
    re-supply the rich object; persist only the pointer that must not be
    forgotten.
    """
    conn.execute(text("""
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


def _r0005_calendar_and_job_results(conn: Connection) -> None:
    """Persist calendar reconciliation state and fenced worker outcomes."""
    inspector = inspect(conn)
    job_columns = {c["name"] for c in inspector.get_columns("jobs")}
    if "result_json" not in job_columns:
        conn.execute(text("ALTER TABLE jobs ADD COLUMN result_json TEXT"))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS calendar_occurrences (
            provider TEXT NOT NULL,
            event_id TEXT NOT NULL,
            revision TEXT NOT NULL,
            starts_at BIGINT NOT NULL,
            title TEXT NOT NULL,
            cancelled INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (provider, event_id)
        )"""))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS calendar_outages (
            provider TEXT PRIMARY KEY,
            first_seen_at BIGINT NOT NULL,
            last_notified_at BIGINT NOT NULL,
            detail TEXT NOT NULL
        )"""))


#: Ordered revisions. Append only; never edit a shipped entry.
REVISIONS: tuple[Revision, ...] = (
    Revision("0000_rename_legacy", "move colliding Phase 4 tables aside",
             _r0000_rename_legacy),
    Revision("0001_initial", "vNext core schema", _r0001_initial),
    Revision("0002_legacy_provenance", "explicit legacy provenance marker",
             _r0002_legacy_provenance),
    Revision("0003_extension_state", "persist routines, preferences, capability "
             "objects, spend and notification state", _r0003_extension_state),
    Revision("0004_trips", "trip authority pointers", _r0004_trips),
    Revision("0005_calendar_job_results", "calendar reconciliation state and "
             "durable job results", _r0005_calendar_and_job_results),
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
