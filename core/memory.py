"""Memory — local SQLite state store for Loop.

Holds everything the assistant must remember between runs: tracked email
follow-ups, captured tasks, snoozed reminders, and user preferences. All state
is local; nothing goes to a remote database.

Phase 2 adds the ``tasks`` table (chat-captured tasks with due dates and
priorities) plus the CRUD helpers used by the Task Specialist and the web
dashboard. Semantic vector memory lives in :mod:`core.vector_store` (ChromaDB).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
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


class LLMUsageLog(Base):
    """One row per LLM request, recording which backend served it.

    Used by the :class:`~core.llm_router.LLMRouter` to keep an auditable trail
    of local-vs-cloud usage (Phase 3). ``prompt_hash`` is a SHA-256 digest so
    we never persist prompt content.
    """

    __tablename__ = "llm_usage_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
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
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class SessionSummary(Base):
    """A one-paragraph summary of a conversation session."""

    __tablename__ = "session_summaries"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
        return datetime.utcnow() - last > ttl

    def purge_expired(self) -> int:
        """Delete all turns belonging to expired sessions. Returns rows removed."""
        ttl = timedelta(hours=self.settings.session_ttl_hours)
        cutoff = datetime.utcnow() - ttl
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
                existing.updated_at = datetime.utcnow()
            session.commit()
        return summary

    def _get_router(self):
        if self._router is None:
            from core.llm_router import LLMRouter

            self._router = LLMRouter(self.settings, memory=self.store)
        return self._router
