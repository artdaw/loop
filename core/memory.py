"""Memory — local SQLite state store for Loop.

Holds everything the assistant must remember between runs: tracked email
follow-ups, captured tasks, snoozed reminders, and user preferences. All state
is local; nothing goes to a remote database.

Phase 2 adds the ``tasks`` table (chat-captured tasks with due dates and
priorities) plus the CRUD helpers used by the Task Specialist and the web
dashboard. Semantic vector memory lives in :mod:`core.vector_store` (ChromaDB).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from config.settings import Settings, get_settings


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
    status: Mapped[str] = mapped_column(String(16), default="open")      # open|done
    source: Mapped[str] = mapped_column(String(32), default="chat")      # chat|wrike|cli
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Preference(Base):
    """A single user preference key/value pair."""

    __tablename__ = "preferences"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


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
        """Create all tables if they do not yet exist."""
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        """Return a new SQLAlchemy session."""
        return Session(self.engine, future=True)

    # ------------------------------------------------------------------ #
    # Task helpers
    # ------------------------------------------------------------------ #
    def add_task(self, description: str, *, due_date: date | None = None,
                 priority: str = "normal", source: str = "chat") -> Task:
        """Persist a new task and return it (detached copy of its fields)."""
        with self.session() as session:
            task = Task(
                description=description,
                due_date=due_date,
                priority=priority,
                source=source,
                status="open",
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            session.expunge(task)
            return task

    def list_open_tasks(self) -> list[Task]:
        """Return all open tasks ordered by due date (nulls last)."""
        with self.session() as session:
            rows = session.scalars(
                select(Task).where(Task.status == "open")
            ).all()
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
            task.completed_at = datetime.utcnow()
            session.commit()
            return True

    # ------------------------------------------------------------------ #
    # Follow-up helpers (used by the web dashboard)
    # ------------------------------------------------------------------ #
    def list_open_follow_ups(self) -> list[FollowUp]:
        """Return follow-ups not yet sent or ignored."""
        with self.session() as session:
            rows = session.scalars(
                select(FollowUp).where(FollowUp.status.in_(["waiting", "drafted"]))
            ).all()
            for row in rows:
                session.expunge(row)
            return list(rows)

    # ------------------------------------------------------------------ #
    # Preference helpers
    # ------------------------------------------------------------------ #
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
