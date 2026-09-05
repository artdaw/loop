"""Schema, versioned migrations, and the legacy upgrade (runtime §4, interfaces §9).

Acceptance §1 requires migrations to be tested against a *populated* snapshot of
the legacy schema, not an empty one — an empty database hides exactly the
mapping bugs that matter.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from loop.core.clock import UTC, FrozenClock, from_micros
from loop.db.legacy import legacy_task_id, migrate_legacy
from loop.db.migrations import (
    HEAD,
    REVISIONS,
    applied_revisions,
    current_revision,
    migrate,
    pending_revisions,
)
from loop.db.session import create_db_engine, pragma

NOW = dt.datetime(2026, 9, 5, 7, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def engine(tmp_path):
    return create_db_engine(f"sqlite:///{tmp_path / 'loop.db'}")


# --------------------------------------------------------------------------- #
# Pragmas
# --------------------------------------------------------------------------- #
def test_foreign_keys_are_enforced(engine):
    """Without this the schema's reference constraints are decorative."""
    assert pragma(engine, "foreign_keys") == 1


def test_wal_and_full_sync_are_set(engine):
    assert str(pragma(engine, "journal_mode")).lower() == "wal"
    assert pragma(engine, "synchronous") == 2  # FULL


# --------------------------------------------------------------------------- #
# Migration mechanics
# --------------------------------------------------------------------------- #
def test_fresh_database_applies_every_revision(engine, clock):
    applied = migrate(engine, clock=clock)
    assert applied == [rev.id for rev in REVISIONS]
    assert current_revision(engine) == HEAD


def test_migration_is_idempotent(engine, clock):
    migrate(engine, clock=clock)
    assert migrate(engine, clock=clock) == []


def test_pending_revisions_shrink_as_they_apply(engine, clock):
    assert len(pending_revisions(engine)) == len(REVISIONS)
    migrate(engine, clock=clock)
    assert pending_revisions(engine) == []


def test_dry_run_reports_without_changing_anything(engine, clock):
    planned = migrate(engine, clock=clock, dry_run=True)

    assert planned == [rev.id for rev in REVISIONS]
    assert applied_revisions(engine) == set()
    assert "schema_version" not in inspect(engine).get_table_names()


def test_applied_revisions_are_recorded_with_a_timestamp(engine, clock):
    migrate(engine, clock=clock)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT revision, applied_at FROM schema_version")).all()
    assert len(rows) == len(REVISIONS)
    assert all(from_micros(row[1]) == NOW for row in rows)


def test_required_tables_exist(engine, clock):
    migrate(engine, clock=clock)
    names = set(inspect(engine).get_table_names())

    assert {"events", "plans", "work_items", "artifacts", "tasks", "triggers",
            "trigger_firings", "jobs", "notifications", "outbox", "operations",
            "approvals", "feedback", "audit"} <= names


def test_required_indexes_exist(engine, clock):
    """runtime §4 names these explicitly; they carry the hot-path queries."""
    migrate(engine, clock=clock)
    inspector = inspect(engine)

    def index_names(table):
        return {i["name"] for i in inspector.get_indexes(table)}

    assert "ix_jobs_state_run_after" in index_names("jobs")
    assert "ix_triggers_enabled_next" in index_names("triggers")
    assert "ix_outbox_state_retry" in index_names("outbox")
    assert "ix_tasks_status_due" in index_names("tasks")


# --------------------------------------------------------------------------- #
# Uniqueness invariants
# --------------------------------------------------------------------------- #
def _insert_event(conn, *, event_id, origin, key):
    conn.execute(text(
        "INSERT INTO events (id, version, created_at, updated_at, kind, "
        "schema_version, origin, origin_id, idempotency_key, occurred_at, "
        "received_at, root_id, hop_count, payload_json, state, privacy) "
        "VALUES (:id, 1, 0, 0, 'message.received', 1, :origin, :key, :key, "
        "0, 0, :id, 0, '{}', 'accepted', '{}')"
    ), {"id": event_id, "origin": origin, "key": key})


def test_replayed_event_cannot_be_stored_twice(engine, clock):
    """The database-level guarantee behind T02."""
    migrate(engine, clock=clock)
    with engine.begin() as conn:
        _insert_event(conn, event_id="e1", origin="telegram", key="update:1")

    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_event(conn, event_id="e2", origin="telegram", key="update:1")


def test_the_same_key_from_a_different_origin_is_allowed(engine, clock):
    migrate(engine, clock=clock)
    with engine.begin() as conn:
        _insert_event(conn, event_id="e1", origin="telegram", key="update:1")
        _insert_event(conn, event_id="e2", origin="cli", key="update:1")

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM events")).scalar() == 2


def test_duplicate_job_dedupe_key_is_rejected(engine, clock):
    migrate(engine, clock=clock)

    def insert(job_id):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO jobs (id, version, created_at, updated_at, kind, "
                "payload_json, dedupe_key, state, run_after, attempts, "
                "max_attempts, fencing_token) VALUES (:id, 1, 0, 0, 'x', '{}', "
                "'same-key', 'queued', 0, 0, 5, 0)"
            ), {"id": job_id})

    insert("j1")
    with pytest.raises(IntegrityError):
        insert("j2")


def test_foreign_key_violation_is_rejected(engine, clock):
    """Proves foreign_keys=ON is actually in force, not just requested."""
    migrate(engine, clock=clock)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO plans (id, version, created_at, updated_at, "
            "root_event_id, intent_summary, status, budget_json, usage_json) "
            "VALUES ('p1', 1, 0, 0, 'no-such-event', '', 'queued', '{}', '{}')"
        ))


# --------------------------------------------------------------------------- #
# Legacy migration against a populated snapshot
# --------------------------------------------------------------------------- #
LEGACY_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER NOT NULL PRIMARY KEY, description TEXT NOT NULL, due_date DATE,
    priority VARCHAR(16), status VARCHAR(16), source VARCHAR(32),
    created_at DATETIME, completed_at DATETIME, project VARCHAR(64),
    wrike_id VARCHAR(64), last_synced_at DATETIME, remote_updated_at DATETIME
);
CREATE TABLE follow_ups (
    id INTEGER NOT NULL PRIMARY KEY, thread_id VARCHAR(255), subject TEXT,
    sender VARCHAR(255), last_sent_at DATETIME, status VARCHAR(32),
    draft_text TEXT, triage_score INTEGER, snooze_until DATETIME, project VARCHAR(64)
);
CREATE TABLE preferences (key VARCHAR(128) PRIMARY KEY, value TEXT);
"""


@pytest.fixture
def legacy_engine(tmp_path):
    """A populated snapshot of the Phase 4 database."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        for statement in LEGACY_SCHEMA.strip().split(";"):
            if statement.strip():
                conn.execute(text(statement))
        conn.execute(text(
            "INSERT INTO tasks (id, description, due_date, priority, status, "
            "source, created_at, completed_at) VALUES "
            "(1, 'Call the repair shop', '2026-09-10', 'high', 'open', 'chat', "
            "'2026-09-01T10:00:00', NULL),"
            "(2, 'Finished thing', NULL, 'normal', 'done', 'web', "
            "'2026-08-01T09:00:00', '2026-08-02T09:00:00'),"
            "(3, 'Gone from wrike', NULL, 'low', 'orphaned', 'wrike', "
            "'2026-08-15T09:00:00', NULL)"
        ))
        conn.execute(text(
            "INSERT INTO follow_ups (id, thread_id, subject, status, triage_score) "
            "VALUES (1, 't1', 'Contract review', 'waiting', 12)"
        ))
        conn.execute(text(
            "INSERT INTO preferences (key, value) VALUES "
            "('autonomy.email_send', 'act'), ('autonomy.default', 'approve'), "
            "('something.else', 'value')"
        ))
    return engine


def _upgrade(engine, clock):
    migrate(engine, clock=clock)
    return migrate_legacy(engine, clock=clock)


def test_dry_run_reports_counts_and_writes_nothing(legacy_engine, clock):
    migrate(legacy_engine, clock=clock)
    report = migrate_legacy(legacy_engine, clock=clock, dry_run=True)

    assert report.dry_run is True
    assert report.tasks == 3
    with legacy_engine.connect() as conn:
        migrated = conn.execute(text(
            "SELECT COUNT(*) FROM tasks WHERE legacy_source IS NOT NULL")).scalar()
    assert migrated == 0


def test_tasks_are_migrated_preserving_identity(legacy_engine, clock):
    report = _upgrade(legacy_engine, clock)

    assert report.tasks == 3
    with legacy_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT title, status, priority, due_date, due_at, legacy_source "
            "FROM tasks WHERE id = :id"), {"id": legacy_task_id(1)}).one()

    assert row[0] == "Call the repair shop"
    assert row[1] == "ready"
    assert row[2] == "high"
    assert row[5] == "phase4:tasks:1"


def test_legacy_due_dates_stay_dates(legacy_engine, clock):
    """interfaces §9: never silently turn a due date into a midnight deadline."""
    _upgrade(legacy_engine, clock)
    with legacy_engine.connect() as conn:
        due_date, due_at = conn.execute(text(
            "SELECT due_date, due_at FROM tasks WHERE id = :id"),
            {"id": legacy_task_id(1)}).one()

    assert due_date == "2026-09-10"
    assert due_at is None


def test_completed_tasks_are_preserved_as_done(legacy_engine, clock):
    _upgrade(legacy_engine, clock)
    with legacy_engine.connect() as conn:
        status, completed = conn.execute(text(
            "SELECT status, completed_at FROM tasks WHERE id = :id"),
            {"id": legacy_task_id(2)}).one()

    assert status == "done"
    assert completed is not None


def test_orphaned_tasks_map_to_cancelled(legacy_engine, clock):
    _upgrade(legacy_engine, clock)
    with legacy_engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM tasks WHERE id = :id"),
                              {"id": legacy_task_id(3)}).scalar()
    assert status == "cancelled"


def test_no_triggers_are_created_from_legacy_rows(legacy_engine, clock):
    """Old reminders must not reactivate on upgrade (interfaces §9)."""
    _upgrade(legacy_engine, clock)
    with legacy_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM triggers")).scalar() == 0


def test_migrated_rows_default_to_local_only_privacy(legacy_engine, clock):
    """Privacy can never widen through an absent legacy column."""
    import json

    _upgrade(legacy_engine, clock)
    with legacy_engine.connect() as conn:
        privacy = conn.execute(text("SELECT privacy FROM tasks WHERE id = :id"),
                               {"id": legacy_task_id(1)}).scalar()

    label = json.loads(privacy)
    assert label["model_scope"] == "local_only"
    assert label["allowed_destinations"] == []


def test_autonomy_act_is_retained_more_restrictively(legacy_engine, clock):
    report = _upgrade(legacy_engine, clock)

    assert report.autonomy_settings == 2
    assert any("email_send" in w and "approve" in w for w in report.warnings)


def test_follow_ups_are_counted_and_flagged_not_silently_dropped(legacy_engine, clock):
    report = _upgrade(legacy_engine, clock)

    assert report.follow_ups == 1
    assert any("follow-up" in w.lower() for w in report.warnings)


def test_rerunning_the_legacy_migration_does_not_duplicate(legacy_engine, clock):
    _upgrade(legacy_engine, clock)
    with legacy_engine.connect() as conn:
        before = conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar()

    migrate_legacy(legacy_engine, clock=clock)

    with legacy_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar() == before


def test_migration_report_summarises_readably(legacy_engine, clock):
    report = _upgrade(legacy_engine, clock)
    assert "3 tasks" in report.summary()


def test_missing_legacy_tables_warn_rather_than_crash(engine, clock):
    migrate(engine, clock=clock)
    report = migrate_legacy(engine, clock=clock)

    assert report.total == 0
    assert report.warnings
