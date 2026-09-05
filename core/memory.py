"""Memory — local SQLite state store for Loop.

Holds everything the assistant must remember between runs: tracked email
follow-ups, captured tasks, snoozed reminders, user preferences, and (later)
conversation history. All state is local; nothing goes to a remote database.

Phase 1 scope:
    - Define the SQLAlchemy models for follow-ups, reminders, and preferences.
    - Provide a MemoryStore with a create_all() bootstrap and basic CRUD helpers.

ChromaDB (semantic vector memory) is added in Phase 2 and lives alongside this.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, create_engine
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
    last_sent_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="waiting")  # waiting|drafted|sent|ignored
    # TODO(phase1): add recipient, draft_text, snooze_until columns.


class Reminder(Base):
    """A scheduled reminder (meeting alert, task deadline, snoozed item)."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # meeting|task|custom
    fire_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")  # JSON blob
    delivered: Mapped[bool] = mapped_column(default=False)


class Preference(Base):
    """A single user preference key/value pair."""

    __tablename__ = "preferences"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class MemoryStore:
    """Thin wrapper around a SQLite engine + session factory."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # TODO(phase1): honour settings.database_url; ensure the data/ dir exists.
        self.engine = create_engine(self.settings.database_url, future=True)

    def bootstrap(self) -> None:
        """Create all tables if they do not yet exist."""
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        """Return a new SQLAlchemy session."""
        return Session(self.engine, future=True)

    # TODO(phase1): add_follow_up(), list_open_follow_ups(), mark_sent().
    # TODO(phase1): add_reminder(), due_reminders(now), mark_delivered().
    # TODO(phase1): get_preference(key, default), set_preference(key, value).
