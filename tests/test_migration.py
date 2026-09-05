"""The additive schema migration.

``create_all`` creates missing tables but never alters existing ones, so a
database written by an earlier Loop version lacks the Phase 4 columns. These
tests pin the migration that closes that gap.
"""

from __future__ import annotations

import sqlalchemy as sa

from core.memory import MemoryStore

_LEGACY_TASKS_DDL = """
CREATE TABLE tasks (
    id INTEGER NOT NULL PRIMARY KEY,
    description VARCHAR(500) NOT NULL,
    due_date DATE,
    priority VARCHAR(16),
    status VARCHAR(16),
    source VARCHAR(16),
    created_at DATETIME,
    completed_at DATETIME
)
"""


def _columns(store: MemoryStore, table: str) -> set[str]:
    with store.session() as session:
        rows = session.execute(sa.text(f"PRAGMA table_info({table})")).all()
    return {row[1] for row in rows}


def test_bootstrap_creates_phase4_columns(settings):
    store = MemoryStore(settings)
    store.bootstrap()
    assert {"project", "wrike_id", "last_synced_at", "remote_updated_at"} <= _columns(
        store, "tasks"
    )
    assert "project" in _columns(store, "follow_ups")


def test_bootstrap_creates_autonomy_audit_table(settings):
    store = MemoryStore(settings)
    store.bootstrap()
    with store.session() as session:
        names = {
            row[0]
            for row in session.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='table'")
            ).all()
        }
    assert "autonomy_audit" in names


def test_bootstrap_is_idempotent(settings):
    store = MemoryStore(settings)
    store.bootstrap()
    before = _columns(store, "tasks")
    store.bootstrap()
    store.bootstrap()
    assert _columns(store, "tasks") == before


def test_migration_upgrades_a_legacy_table_preserving_rows(settings):
    """A pre-Phase-4 tasks table gains the new columns without losing data."""
    store = MemoryStore(settings)
    with store.session() as session:
        session.execute(sa.text(_LEGACY_TASKS_DDL))
        session.execute(
            sa.text(
                "INSERT INTO tasks (id, description, status, priority, source) "
                "VALUES (1, 'legacy row', 'open', 'normal', 'chat')"
            )
        )
        session.commit()

    store.bootstrap()

    assert {"project", "wrike_id"} <= _columns(store, "tasks")
    with store.session() as session:
        row = session.execute(
            sa.text("SELECT description, wrike_id FROM tasks WHERE id = 1")
        ).one()
    assert row[0] == "legacy row"
    assert row[1] is None
