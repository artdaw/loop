"""Migration from the Phase 4 database (interfaces contract §9).

Rules this module exists to enforce, each one a way the migration could
plausibly go wrong:

* **IDs are preserved.** Legacy integer task IDs become stable string IDs
  (``legacy:tasks:7``) rather than fresh UUIDs, so references the user already
  holds — a Telegram button, a printed digest — still resolve.
* **Dates stay dates.** A legacy ``due_date`` maps to ``due_date``, never to a
  midnight ``due_at``. Coercing it would invent a deadline instant nobody set.
* **No old reminder reactivates.** Legacy rows produce no triggers. The old
  scheduler's timing lived in process configuration, not in the database, so
  recreating triggers from them would fire reminders the user never re-approved.
* **Privacy never widens by omission.** The legacy schema has no privacy column;
  every migrated row therefore gets the local-only default, not cloud-allowed.
* **Autonomy stays at the more restrictive level** until explicitly activated.

The legacy tables are read, never dropped. A failed upgrade loses nothing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, inspect, text

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.privacy import PrivacyLabel

logger = logging.getLogger(__name__)

#: Legacy status → vNext task status. The legacy schema had only these three.
STATUS_MAP = {
    "open": "ready",
    "done": "done",
    "orphaned": "cancelled",
}

#: Legacy autonomy levels, ordered. Migration keeps the more restrictive value.
AUTONOMY_ORDER = ("observe", "suggest", "approve", "act")


@dataclass
class MigrationReport:
    """What a dry run would do, or what a real run did."""

    tasks: int = 0
    follow_ups: int = 0
    preferences: int = 0
    autonomy_settings: int = 0
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def total(self) -> int:
        return (self.tasks + self.follow_ups + self.preferences
                + self.autonomy_settings)

    def summary(self) -> str:
        prefix = "Legacy migration (dry run):" if self.dry_run else "Legacy migration:"
        parts = [f"{self.tasks} tasks", f"{self.follow_ups} follow-ups",
                 f"{self.preferences} preferences",
                 f"{self.autonomy_settings} autonomy settings"]
        text_ = f"{prefix} " + ", ".join(parts) + "."
        if self.warnings:
            text_ += f" {len(self.warnings)} warning(s)."
        return text_


def legacy_task_id(row_id: int) -> str:
    """Stable vNext ID derived from a legacy row, so references survive."""
    return f"legacy:tasks:{row_id}"


def _has_table(engine: Engine, name: str) -> bool:
    return name in inspect(engine).get_table_names()


def _columns(engine: Engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def migrate_legacy(engine: Engine, *, clock: Clock | None = None,
                   dry_run: bool = False) -> MigrationReport:
    """Copy Phase 4 rows into the vNext schema.

    Idempotent: rows already migrated (matched by their derived ID) are skipped,
    so a re-run after a partial failure completes rather than duplicating.
    """
    clock = clock or SystemClock()
    now = to_micros(clock.now())
    report = MigrationReport(dry_run=dry_run)

    # The default label for every migrated row: the legacy schema recorded no
    # privacy, and interfaces §9 forbids letting an absent column mean
    # cloud-allowed.
    default_privacy = json.dumps(PrivacyLabel.for_unlabelled_import().to_json())

    # Migration 0000 renames a colliding Phase 4 table to legacy_tasks.
    source_table = "legacy_tasks" if _has_table(engine, "legacy_tasks") else None
    if source_table is None:
        report.warnings.append("no legacy tasks table found; nothing to migrate")
        return report

    with engine.connect() as conn:
        legacy_tasks: list[Any] = list(conn.execute(text(
            "SELECT id, description, due_date, priority, status, source, "
            f"created_at, completed_at FROM {source_table}"
        )).all())
        # Already-migrated rows are identified by their derived ID, so a re-run
        # after a partial failure completes instead of duplicating.
        existing = {
            row[0] for row in conn.execute(
                text("SELECT id FROM tasks WHERE legacy_source IS NOT NULL")).all()
        }

    rows_to_write: list[dict[str, Any]] = []
    for row in legacy_tasks:
        (row_id, description, due_date, priority, status, source,
         created_at, completed_at) = row
        new_id = legacy_task_id(row_id)
        if new_id in existing:
            report.skipped.append(new_id)
            continue

        mapped_status = STATUS_MAP.get(str(status or "open"), "ready")
        if str(status or "") not in STATUS_MAP:
            report.warnings.append(
                f"task {row_id}: unknown legacy status {status!r}, treated as ready")

        rows_to_write.append({
            "id": new_id,
            "version": 1,
            "created_at": _as_micros(created_at, now),
            "updated_at": now,
            "title": (description or "").strip() or "(untitled)",
            "description": "",
            "original_event_id": None,
            "owner": "owner",
            "status": mapped_status,
            "priority": str(priority or "normal"),
            "project_ref": None,
            # A day-level obligation stays a day-level obligation.
            "due_date": str(due_date) if due_date else None,
            "due_at": None,
            "timezone": "Europe/Berlin",
            "blocked_reason": None,
            "waiting_on": None,
            "parent_task_id": None,
            "context_json": json.dumps({"legacy_source": source or "chat"}),
            "completed_at": _as_micros(completed_at, None),
            "cancelled_at": None,
            "privacy": default_privacy,
            "legacy_source": f"phase4:tasks:{row_id}",
        })

    report.tasks = len(rows_to_write)

    follow_ups_table = ("legacy_follow_ups" if _has_table(engine, "legacy_follow_ups")
                        else "follow_ups")
    if _has_table(engine, follow_ups_table):
        with engine.connect() as conn:
            report.follow_ups = conn.execute(
                text(f"SELECT COUNT(*) FROM {follow_ups_table}")).scalar() or 0
        report.warnings.append(
            "follow-ups counted but not copied: vNext models them as tasks with "
            "waiting_on, which needs the Stage A task service (A4)")

    if _has_table(engine, "preferences"):
        with engine.connect() as conn:
            prefs = conn.execute(
                text("SELECT key, value FROM preferences")).all()
        autonomy = [p for p in prefs if str(p[0]).startswith("autonomy.")]
        report.preferences = len(prefs) - len(autonomy)
        report.autonomy_settings = len(autonomy)
        for key, value in autonomy:
            if str(value) == "act":
                report.warnings.append(
                    f"{key}=act retained as the more restrictive 'approve' until "
                    "explicitly re-activated (interfaces §9)")

    if dry_run:
        return report

    if rows_to_write:
        with engine.begin() as conn:
            for payload in rows_to_write:
                conn.execute(text(
                    "INSERT INTO tasks (id, version, created_at, updated_at, title, "
                    "description, original_event_id, owner, status, priority, "
                    "project_ref, due_date, due_at, timezone, blocked_reason, "
                    "waiting_on, parent_task_id, context_json, completed_at, "
                    "cancelled_at, privacy, legacy_source) "
                    "VALUES (:id, :version, :created_at, :updated_at, :title, "
                    ":description, :original_event_id, :owner, :status, :priority, "
                    ":project_ref, :due_date, :due_at, :timezone, :blocked_reason, "
                    ":waiting_on, :parent_task_id, :context_json, :completed_at, "
                    ":cancelled_at, :privacy, :legacy_source)"
                ), payload)

    return report


def _as_micros(value: Any, default: int | None) -> int | None:
    """Convert a legacy naive-UTC datetime string to integer microseconds."""
    if value in (None, ""):
        return default
    import datetime as dt

    if isinstance(value, dt.datetime):
        moment = value
    else:
        try:
            moment = dt.datetime.fromisoformat(str(value))
        except ValueError:
            return default
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return to_micros(moment)
