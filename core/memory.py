"""Memory — local SQLite state store for Loop.

Holds everything the assistant must remember between runs: tracked email
follow-ups, captured tasks, snoozed reminders, and user preferences. All state
is local; nothing goes to a remote database.

Phase 2 adds the ``tasks`` table (chat-captured tasks with due dates and
priorities) plus the CRUD helpers used by the Task Specialist and the web
dashboard. Semantic vector memory lives in :mod:`core.vector_store` (ChromaDB).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Naive UTC timestamp.

    Columns store naive UTC (``datetime.utcnow`` semantics) so existing rows
    stay comparable; this is the non-deprecated way to produce the same value.
    """
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Declarative base for all Loop ORM models."""


class FollowUp(Base):
    """An email thread awaiting a reply, tracked for follow-up."""

    __tablename__ = "follow_ups"

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(255), index=True)
    subject: Mapped[str] = mapped_column(Text, default="")
    sender: Mapped[str] = mapped_column(String(255), default="")
    last_sent_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="waiting")  # waiting|drafted|sent|ignored
    draft_text: Mapped[str] = mapped_column(Text, default="")
    # Composite triage score (urgency * importance, 1..25). Higher = sort first.
    triage_score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    # When snoozed, don't resurface before this time.
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Project slug this thread belongs to (Phase 4), or NULL when unmatched.
    project: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class Reminder(Base):
    """A scheduled reminder (meeting alert, task deadline, snoozed item)."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # meeting|task|custom
    fire_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")  # JSON blob
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)


class Task(Base):
    """A task/todo item captured from chat (or, later, synced from Wrike)."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    priority: Mapped[str] = mapped_column(String(16), default="normal")  # low|normal|high
    status: Mapped[str] = mapped_column(String(16), default="open")      # open|done|orphaned
    source: Mapped[str] = mapped_column(String(32), default="chat")      # chat|wrike|cli
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # --- Phase 4 --------------------------------------------------------- #
    # Project slug this task belongs to, or NULL when unmatched.
    project: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Wrike linkage. ``wrike_id`` is set once a task exists on both sides.
    wrike_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    remote_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Preference(Base):
    """A single user preference key/value pair."""

    __tablename__ = "preferences"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class LLMUsageLog(Base):
    """One row per LLM request, recording which backend served it.

    Used by the :class:`~core.llm_router.LLMRouter` to keep an auditable trail
    of local-vs-cloud usage (Phase 3). ``prompt_hash`` is a SHA-256 digest so
    we never persist prompt content.
    """

    __tablename__ = "llm_usage_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    backend: Mapped[str] = mapped_column(String(16))            # local | cloud
    prompt_hash: Mapped[str] = mapped_column(String(64), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    local_only: Mapped[bool] = mapped_column(Boolean, default=False)


class ConversationTurn(Base):
    """A single turn (user or assistant message) in a conversation session."""

    __tablename__ = "conversation_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SessionSummary(Base):
    """A one-paragraph summary of a conversation session."""

    __tablename__ = "session_summaries"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AutonomyAudit(Base):
    """One row per gated action, recording under what authority it happened.

    Mirrors the auditability rule already applied to LLM calls: we record *that*
    an action occurred and at which autonomy level, never the content of the
    email, note, or task involved. ``detail`` is a short human-readable label
    (a task id, a filename), not a payload.
    """

    __tablename__ = "autonomy_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    level: Mapped[str] = mapped_column(String(16), default="approve")
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text, default="")


class MemoryStore:
    """Thin wrapper around a SQLite engine + session factory."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._ensure_sqlite_dir()
        self.engine = create_engine(self.settings.database_url, future=True)

    def _ensure_sqlite_dir(self) -> None:
        """Create the parent directory for a file-based SQLite database."""
        url = self.settings.database_url
        prefix = "sqlite:///"
        if url.startswith(prefix):
            db_path = Path(url[len(prefix):])
            if db_path.parent and not db_path.parent.exists():
                db_path.parent.mkdir(parents=True, exist_ok=True)

    def bootstrap(self) -> None:
        """Create missing tables, then additively migrate the existing ones."""
        Base.metadata.create_all(self.engine)
        self._migrate()

    def _migrate(self) -> None:
        """Add mapped columns that are missing from already-existing tables.

        ``create_all`` creates missing *tables* but never alters existing ones,
        so a database written by an earlier version of Loop keeps its old
        columns and silently lacks the new ones. This walks every mapped table
        that already exists and adds whatever columns the ORM expects.

        Additive only: it never drops, renames, or retypes a column, which is
        what makes it safe to run unconditionally on every boot. That
        constraint is also why a full migration tool (Alembic) is not warranted
        for a single-user SQLite file.
        """
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())

        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                if table.name not in existing_tables:
                    continue  # create_all just made it, with every column
                present = {col["name"] for col in inspector.get_columns(table.name)}
                for column in table.columns:
                    if column.name in present:
                        continue
                    ddl_type = column.type.compile(dialect=self.engine.dialect)
                    # ALTER TABLE ADD COLUMN cannot add a NOT NULL column without
                    # a default, so added columns are always nullable here.
                    conn.execute(
                        text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl_type}")
                    )
                    logger.info("Schema migration: added %s.%s", table.name, column.name)

    def session(self) -> Session:
        """Return a new SQLAlchemy session."""
        return Session(self.engine, future=True)

    # ------------------------------------------------------------------ #
    # Task helpers
    # ------------------------------------------------------------------ #
    def add_task(self, description: str, *, due_date: date | None = None,
                 priority: str = "normal", source: str = "chat",
                 project: str | None = None,
                 wrike_id: str | None = None) -> Task:
        """Persist a new task and return it (detached copy of its fields)."""
        with self.session() as session:
            task = Task(
                description=description,
                due_date=due_date,
                priority=priority,
                source=source,
                status="open",
                project=project,
                wrike_id=wrike_id,
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            session.expunge(task)
            return task

    def list_open_tasks(self, *, project: str | None = None) -> list[Task]:
        """Return all open tasks ordered by due date (nulls last).

        ``project`` filters to one project slug; ``None`` returns every task.
        """
        with self.session() as session:
            statement = select(Task).where(Task.status == "open")
            if project:
                statement = statement.where(Task.project == project)
            rows = session.scalars(statement).all()
            ordered = sorted(rows, key=lambda t: (t.due_date is None, t.due_date or date.max))
            for row in ordered:
                session.expunge(row)
            return list(ordered)

    def get_due_today(self, *, today: date | None = None) -> list[Task]:
        """Return open tasks due exactly today."""
        target = today or date.today()
        with self.session() as session:
            rows = session.scalars(
                select(Task).where(Task.status == "open", Task.due_date == target)
            ).all()
            for row in rows:
                session.expunge(row)
            return list(rows)

    def get_overdue(self, *, today: date | None = None) -> list[Task]:
        """Return open tasks whose due date is before today."""
        target = today or date.today()
        with self.session() as session:
            rows = session.scalars(
                select(Task).where(Task.status == "open", Task.due_date < target)
            ).all()
            for row in rows:
                session.expunge(row)
            return list(rows)

    def get_completed_today(self, *, today: date | None = None) -> list[Task]:
        """Return tasks marked done today."""
        target = today or date.today()
        with self.session() as session:
            rows = session.scalars(
                select(Task).where(Task.status == "done")
            ).all()
            done_today = [
                r for r in rows
                if r.completed_at and r.completed_at.date() == target
            ]
            for row in done_today:
                session.expunge(row)
            return done_today

    def complete_task(self, task_id: int) -> bool:
        """Mark a task done; returns True if it existed."""
        with self.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                return False
            task.status = "done"
            task.completed_at = utcnow()
            session.commit()
            return True

    # ------------------------------------------------------------------ #
    # Follow-up helpers (used by the web dashboard)
    # ------------------------------------------------------------------ #
    def list_open_follow_ups(self, *, include_snoozed: bool = False) -> list[FollowUp]:
        """Return open follow-ups ordered by triage score (highest first).

        Snoozed follow-ups whose snooze window has not elapsed are hidden unless
        ``include_snoozed`` is True.
        """
        now = utcnow()
        with self.session() as session:
            rows = session.scalars(
                select(FollowUp).where(FollowUp.status.in_(["waiting", "drafted"]))
            ).all()
            if not include_snoozed:
                rows = [
                    r for r in rows
                    if not (r.snooze_until and r.snooze_until > now)
                ]
            ordered = sorted(rows, key=lambda f: f.triage_score or 0, reverse=True)
            for row in ordered:
                session.expunge(row)
            return list(ordered)

    def add_follow_up(self, *, thread_id: str, subject: str = "", sender: str = "",
                      last_sent_at: datetime | None = None, draft_text: str = "",
                      status: str = "waiting", triage_score: int = 0) -> FollowUp:
        """Create and persist a follow-up row; returns a detached copy."""
        with self.session() as session:
            fu = FollowUp(
                thread_id=thread_id,
                subject=subject,
                sender=sender,
                last_sent_at=last_sent_at or utcnow(),
                draft_text=draft_text,
                status=status,
                triage_score=triage_score,
            )
            session.add(fu)
            session.commit()
            session.refresh(fu)
            session.expunge(fu)
            return fu

    def get_follow_up(self, follow_up_id: int) -> FollowUp | None:
        """Return a single follow-up (detached) or None."""
        with self.session() as session:
            fu = session.get(FollowUp, follow_up_id)
            if fu is not None:
                session.expunge(fu)
            return fu

    def set_follow_up_status(self, follow_up_id: int, status: str) -> bool:
        """Update a follow-up's status (e.g. 'sent', 'ignored'). Returns True if found."""
        with self.session() as session:
            fu = session.get(FollowUp, follow_up_id)
            if fu is None:
                return False
            fu.status = status
            session.commit()
            return True

    def snooze_follow_up(self, follow_up_id: int, hours: int) -> bool:
        """Hide a follow-up for ``hours`` hours. Returns True if found."""
        with self.session() as session:
            fu = session.get(FollowUp, follow_up_id)
            if fu is None:
                return False
            fu.snooze_until = utcnow() + timedelta(hours=hours)
            session.commit()
            return True

    def set_follow_up_triage(self, follow_up_id: int, score: int) -> bool:
        """Persist a computed triage score on a follow-up. Returns True if found."""
        with self.session() as session:
            fu = session.get(FollowUp, follow_up_id)
            if fu is None:
                return False
            fu.triage_score = score
            session.commit()
            return True

    # ------------------------------------------------------------------ #
    # LLM usage logging (Phase 3)
    # ------------------------------------------------------------------ #
    def log_llm_usage(self, *, backend: str, prompt_hash: str, latency_ms: int,
                      local_only: bool) -> None:
        """Append a row to ``llm_usage_log`` recording a single LLM request."""
        with self.session() as session:
            session.add(
                LLMUsageLog(
                    backend=backend,
                    prompt_hash=prompt_hash,
                    latency_ms=latency_ms,
                    local_only=local_only,
                )
            )
            session.commit()

    # ------------------------------------------------------------------ #
    # Preference helpers
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # Task helpers (Phase 4)
    # ------------------------------------------------------------------ #
    def get_task(self, task_id: int) -> Task | None:
        """Return one task by id (detached copy), or ``None``."""
        with self.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                return None
            session.expunge(task)
            return task

    def set_task_project(self, task_id: int, project: str | None) -> bool:
        """Tag a task with a project slug. Returns False if it does not exist."""
        with self.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                return False
            task.project = project
            session.commit()
            return True

    def set_task_status(self, task_id: int, status: str) -> bool:
        """Set a task's status directly (used by Wrike sync for 'orphaned')."""
        with self.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                return False
            task.status = status
            session.commit()
            return True

    def update_task_fields(self, task_id: int, *, description: str | None = None,
                           due_date: date | None = None,
                           priority: str | None = None) -> bool:
        """Update mutable task fields, skipping any left as ``None``."""
        with self.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                return False
            if description is not None:
                task.description = description
            if due_date is not None or description is not None:
                # due_date is nullable upstream, so an explicit None from Wrike
                # is applied alongside a title change rather than ignored.
                task.due_date = due_date
            if priority is not None:
                task.priority = priority
            session.commit()
            return True

    def link_wrike(self, task_id: int, wrike_id: str) -> bool:
        """Record the Wrike id for a locally-created task."""
        with self.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                return False
            task.wrike_id = wrike_id
            task.last_synced_at = utcnow()
            session.commit()
            return True

    def mark_task_synced(self, task_id: int,
                         remote_updated_at: datetime | None = None) -> bool:
        """Stamp a task as reconciled with Wrike."""
        with self.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                return False
            task.last_synced_at = utcnow()
            if remote_updated_at is not None:
                task.remote_updated_at = remote_updated_at
            session.commit()
            return True

    def list_tasks_for_sync(self) -> list[Task]:
        """Every task regardless of status (detached copies).

        Sync needs completed and orphaned rows too, so this deliberately does
        not filter on ``status`` the way the other listing helpers do.
        """
        with self.session() as session:
            rows = list(session.scalars(select(Task).order_by(Task.id)))
            for row in rows:
                session.expunge(row)
            return rows

    def known_task_projects(self) -> list[str]:
        """Return the distinct project slugs currently in use, sorted."""
        with self.session() as session:
            rows = session.scalars(
                select(Task.project).where(Task.project.is_not(None)).distinct()
            ).all()
        return sorted({row for row in rows if row})

    # ------------------------------------------------------------------ #
    # Aggregation (Phase 4) — weekly review and metrics
    # ------------------------------------------------------------------ #
    def activity_counts(self, window_start: datetime,
                        window_end: datetime) -> dict:
        """Aggregate activity within a datetime window.

        One query pass used by both the weekly review and the metrics
        dashboard, so the two always agree on what a number means.

        ``tasks_overdue`` is deliberately *current* state rather than windowed:
        what matters is what is late now, not what was late during the window.
        """
        from sqlalchemy import func

        with self.session() as session:
            tasks_created = session.scalar(
                select(func.count(Task.id)).where(
                    Task.created_at >= window_start, Task.created_at <= window_end
                )
            ) or 0

            tasks_completed = session.scalar(
                select(func.count(Task.id)).where(
                    Task.completed_at.is_not(None),
                    Task.completed_at >= window_start,
                    Task.completed_at <= window_end,
                )
            ) or 0

            tasks_overdue = session.scalar(
                select(func.count(Task.id)).where(
                    Task.status == "open",
                    Task.due_date.is_not(None),
                    Task.due_date < date.today(),
                )
            ) or 0

            follow_up_rows = session.execute(
                select(FollowUp.status, func.count(FollowUp.id)).group_by(FollowUp.status)
            ).all()
            by_status = {status: count for status, count in follow_up_rows}

            llm_rows = session.execute(
                select(LLMUsageLog.backend, func.count(LLMUsageLog.id))
                .where(
                    LLMUsageLog.timestamp >= window_start,
                    LLMUsageLog.timestamp <= window_end,
                )
                .group_by(LLMUsageLog.backend)
            ).all()
            by_backend = {backend: count for backend, count in llm_rows}

            autonomy_actions = session.scalar(
                select(func.count(AutonomyAudit.id)).where(
                    AutonomyAudit.timestamp >= window_start,
                    AutonomyAudit.timestamp <= window_end,
                )
            ) or 0

            project_rows = session.execute(
                select(Task.project, func.count(Task.id))
                .where(
                    Task.project.is_not(None),
                    Task.created_at >= window_start,
                    Task.created_at <= window_end,
                )
                .group_by(Task.project)
            ).all()
            top_projects = sorted(
                ((slug, count) for slug, count in project_rows if slug),
                key=lambda pair: (-pair[1], pair[0]),
            )[:5]

        return {
            "tasks_created": tasks_created,
            "tasks_completed": tasks_completed,
            "tasks_overdue": tasks_overdue,
            "follow_ups_resolved": by_status.get("sent", 0),
            "follow_ups_ignored": by_status.get("ignored", 0),
            "follow_ups_pending": by_status.get("waiting", 0) + by_status.get("drafted", 0),
            "llm_local": by_backend.get("local", 0),
            "llm_cloud": by_backend.get("cloud", 0),
            "autonomy_actions": autonomy_actions,
            "top_projects": top_projects,
        }

    def backdate_task(self, task_id: int, created: date) -> bool:
        """Move a task's creation date. Used by tests and data repair."""
        with self.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                return False
            task.created_at = datetime.combine(created, datetime.min.time())
            session.commit()
            return True

    # ------------------------------------------------------------------ #
    # Autonomy audit (Phase 4)
    # ------------------------------------------------------------------ #
    def log_autonomy_action(self, *, action: str, level: str, executed: bool,
                            approved: bool = False, detail: str = "") -> None:
        """Append a row to the autonomy audit trail.

        Records that an action happened and under what authority — never its
        content, mirroring the prompt-hash rule for ``llm_usage_log``.
        """
        with self.session() as session:
            session.add(AutonomyAudit(
                action=action,
                level=level,
                executed=executed,
                approved=approved,
                detail=detail,
            ))
            session.commit()

    def recent_autonomy_audit(self, *, limit: int = 50) -> list[AutonomyAudit]:
        """Return the most recent audit rows, newest first (detached copies)."""
        with self.session() as session:
            rows = list(session.scalars(
                select(AutonomyAudit)
                .order_by(AutonomyAudit.timestamp.desc(), AutonomyAudit.id.desc())
                .limit(limit)
            ))
            for row in rows:
                session.expunge(row)
            return rows

    def get_preference(self, key: str, default: str = "") -> str:
        with self.session() as session:
            pref = session.get(Preference, key)
            return pref.value if pref else default

    def set_preference(self, key: str, value: str) -> None:
        with self.session() as session:
            pref = session.get(Preference, key)
            if pref is None:
                session.add(Preference(key=key, value=value))
            else:
                pref.value = value
            session.commit()



class ConversationMemory:
    """Persistent conversation context for the orchestrator (Phase 3).

    Stores every turn of a conversation in SQLite so the assistant can carry
    context across CLI/chat interactions. Sessions expire after
    ``settings.session_ttl_hours`` of inactivity: once expired, ``get_recent``
    returns nothing so the orchestrator naturally starts a fresh context.

    ``summarise_session`` uses the LOCAL LLM only (privacy-safe) to produce a
    one-paragraph recap stored in ``session_summaries``.
    """

    def __init__(self, settings: Settings | None = None,
                 memory: "MemoryStore | None" = None,
                 router: "object | None" = None) -> None:
        self.settings = settings or get_settings()
        self.store = memory or MemoryStore(self.settings)
        self.store.bootstrap()
        self._router = router  # optional LLMRouter; lazily created when summarising

    # ------------------------------------------------------------------ #
    # Turn persistence
    # ------------------------------------------------------------------ #
    async def save_turn(self, role: str, content: str, session_id: str) -> None:
        """Persist a single conversation turn."""
        with self.store.session() as session:
            session.add(
                ConversationTurn(session_id=session_id, role=role, content=content)
            )
            session.commit()

    async def get_recent(self, session_id: str, n: int = 10) -> list[dict[str, str]]:
        """Return the last ``n`` turns for a session as ``{role, content}`` dicts.

        Returns an empty list when the session has expired (no activity within
        ``session_ttl_hours``), so callers transparently start fresh.
        """
        if self.is_expired(session_id):
            return []
        with self.store.session() as session:
            rows = session.scalars(
                select(ConversationTurn)
                .where(ConversationTurn.session_id == session_id)
                .order_by(ConversationTurn.id.desc())
                .limit(n)
            ).all()
        turns = [{"role": r.role, "content": r.content} for r in reversed(rows)]
        return turns

    # ------------------------------------------------------------------ #
    # Expiry
    # ------------------------------------------------------------------ #
    def _last_activity(self, session_id: str) -> datetime | None:
        with self.store.session() as session:
            row = session.scalars(
                select(ConversationTurn)
                .where(ConversationTurn.session_id == session_id)
                .order_by(ConversationTurn.id.desc())
                .limit(1)
            ).first()
            return row.timestamp if row else None

    def is_expired(self, session_id: str) -> bool:
        """True when the session's last turn is older than the configured TTL."""
        last = self._last_activity(session_id)
        if last is None:
            return False  # brand-new session, not "expired"
        ttl = timedelta(hours=self.settings.session_ttl_hours)
        return utcnow() - last > ttl

    def purge_expired(self) -> int:
        """Delete all turns belonging to expired sessions. Returns rows removed."""
        ttl = timedelta(hours=self.settings.session_ttl_hours)
        cutoff = utcnow() - ttl
        removed = 0
        with self.store.session() as session:
            # Find sessions whose most recent turn is before the cutoff.
            all_sessions = session.scalars(
                select(ConversationTurn.session_id).distinct()
            ).all()
            for sid in all_sessions:
                latest = session.scalars(
                    select(ConversationTurn)
                    .where(ConversationTurn.session_id == sid)
                    .order_by(ConversationTurn.id.desc())
                    .limit(1)
                ).first()
                if latest and latest.timestamp < cutoff:
                    stale = session.scalars(
                        select(ConversationTurn).where(
                            ConversationTurn.session_id == sid
                        )
                    ).all()
                    for row in stale:
                        session.delete(row)
                        removed += 1
            session.commit()
        return removed

    # ------------------------------------------------------------------ #
    # Summarisation (local LLM only)
    # ------------------------------------------------------------------ #
    async def summarise_session(self, session_id: str) -> str:
        """Summarise a session into one paragraph using the LOCAL LLM.

        The summary is stored in ``session_summaries`` and returned. Uses the
        local model only (``local_only=True``) so conversation content never
        reaches a cloud provider.
        """
        with self.store.session() as session:
            rows = session.scalars(
                select(ConversationTurn)
                .where(ConversationTurn.session_id == session_id)
                .order_by(ConversationTurn.id.asc())
            ).all()
        if not rows:
            return ""

        transcript = "\n".join(f"{r.role}: {r.content}" for r in rows)
        prompt = (
            "Summarise the following conversation in a single concise paragraph. "
            "Capture the key topics, decisions, and any open action items.\n\n"
            f"{transcript}\n\nSummary:"
        )
        router = self._get_router()
        # Local-only: conversation history stays on the machine.
        result = await router.route_async(prompt, {"local_only": True, "source": "conversation"})
        summary = result.text.strip()

        with self.store.session() as session:
            existing = session.get(SessionSummary, session_id)
            if existing is None:
                session.add(SessionSummary(session_id=session_id, summary=summary))
            else:
                existing.summary = summary
                existing.updated_at = utcnow()
            session.commit()
        return summary

    def _get_router(self):
        if self._router is None:
            from core.llm_router import LLMRouter

            self._router = LLMRouter(self.settings, memory=self.store)
        return self._router
