"""Coordinated snapshots: prove consistency, or refuse (O09, LG11, R1).

Loop keeps three stores that must agree: domain SQLite, the LangGraph
checkpoint database, and the vault. Copying them one after another produces
three snapshots of three different moments, and the corruption that causes is
invisible — every file is individually valid, every hash matches, and the
restored system holds a job whose checkpoint does not exist, or a compiled page
whose ledger row was written after the copy.

SQLite's backup API gives a consistent copy **of one database**. It gives
nothing across two, and this module does not pretend otherwise. What it does
instead:

1. **Admission control.** A durable barrier row that every application writer
   checks before starting new work. Held, the sweep and the workers decline to
   begin anything new — they do not abandon work in flight, they stop
   admitting more.
2. **Drain.** The caller supplies how to wait for in-flight work; the barrier
   bounds how long that may take.
3. **Copy**, using SQLite's own backup API per database.
4. **Prove it held.** `PRAGMA data_version` changes whenever *any* connection
   commits to that database. Recorded before and after the copy, it detects a
   writer that slipped through — including one this process does not know
   about, such as a second instance or a person with a shell.
5. **Retry, then refuse.** A snapshot that cannot be shown consistent is not
   written. A backup that might be torn is worse than no backup, because it
   will be trusted precisely when everything else has already failed.

The vault is a directory that people edit in an editor, so it gets the same
treatment by a different means: its tree hash is taken before and after, and a
change during the window fails the attempt.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.ids import new_id

logger = logging.getLogger(__name__)

#: How long a barrier may be held before other writers ignore it. A crashed
#: backup must not stop the service forever.
BARRIER_TTL_SECONDS = 300

#: How many times a snapshot retries after detecting a concurrent write.
MAX_ATTEMPTS = 3


class SnapshotRefused(RuntimeError):
    """Consistency could not be established, so nothing was written."""


@dataclass
class SnapshotAttempt:
    """One try, and why it did or did not hold."""

    number: int
    consistent: bool
    detail: str = ""
    changed: list[str] = field(default_factory=list)


class SnapshotBarrier:
    """Admission control every application writer honours.

    Deliberately *not* a lock around the writes themselves. Holding a lock
    across a multi-hundred-megabyte vault copy would stall the service for the
    length of the copy and invite exactly the timeouts that make people disable
    backups. This stops new work being admitted and lets what is running
    finish, which is enough: the `data_version` check afterwards is what
    actually proves nothing slipped through.
    """

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS snapshot_barrier ("
                " id INTEGER PRIMARY KEY CHECK (id = 1),"
                " holder TEXT NOT NULL,"
                " reason TEXT NOT NULL DEFAULT '',"
                " acquired_at INTEGER NOT NULL,"
                " expires_at INTEGER NOT NULL)"))
            session.commit()

    def held(self) -> bool:
        """Whether new work should be declined right now."""
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT expires_at FROM snapshot_barrier WHERE id = 1"
            )).first()
        return row is not None and int(row[0]) > now

    def holder(self) -> str | None:
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT holder, expires_at FROM snapshot_barrier WHERE id = 1"
            )).first()
        if row is None or int(row[1]) <= to_micros(self._clock.now()):
            return None
        return str(row[0])

    @contextmanager
    def hold(self, *, reason: str = "backup",
             ttl_seconds: int = BARRIER_TTL_SECONDS) -> Iterator[str]:
        """Stop admitting new work for the duration of the block."""
        holder = new_id()
        now = to_micros(self._clock.now())
        expires = now + ttl_seconds * 1_000_000
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO snapshot_barrier (id, holder, reason, acquired_at,"
                " expires_at) VALUES (1, :holder, :reason, :now, :expires)"
                " ON CONFLICT(id) DO UPDATE SET holder = excluded.holder,"
                " reason = excluded.reason, acquired_at = excluded.acquired_at,"
                " expires_at = excluded.expires_at"),
                {"holder": holder, "reason": reason, "now": now,
                 "expires": expires})
            session.commit()
        try:
            yield holder
        finally:
            # Released even when the snapshot raised. A barrier left behind by
            # a failed backup would quietly stop the service admitting work
            # until its TTL expired.
            with self._sessions() as session:
                session.execute(
                    text("DELETE FROM snapshot_barrier WHERE holder = :holder"),
                    {"holder": holder})
                session.commit()


class ConsistencyWatch:
    """Watches every store for the duration of one snapshot attempt.

    **The connection must stay open.** `PRAGMA data_version` only reports
    changes made by *other* connections since this connection last looked at
    it; a fresh connection each time returns a value that means nothing when
    compared with the previous one. Opening and closing per probe — the
    obvious implementation — silently always reports "unchanged", which is the
    worst possible failure for a consistency check: it passes.

    Directories have no such counter, so their fingerprint is recomputed.
    """

    def __init__(self, databases: dict[str, Path],
                 trees: dict[str, Callable[[], str]] | None = None) -> None:
        self._connections: dict[str, sqlite3.Connection] = {}
        self._trees = dict(trees or {})
        for name, path in databases.items():
            if path.exists():
                self._connections[name] = sqlite3.connect(
                    f"file:{path}?mode=ro", uri=True)
        self._baseline = self._read()

    def _read(self) -> dict[str, object]:
        state: dict[str, object] = {}
        for name, connection in self._connections.items():
            state[name] = connection.execute(
                "PRAGMA data_version").fetchone()[0]
        for name, fingerprint in self._trees.items():
            state[name] = fingerprint()
        return state

    def changed(self) -> list[str]:
        """Stores that a writer committed to since this watch began."""
        current = self._read()
        return sorted(name for name, value in self._baseline.items()
                      if current.get(name) != value)

    def close(self) -> None:
        for connection in self._connections.values():
            connection.close()
        self._connections.clear()


def data_version(database: Path) -> int | None:
    """One reading of SQLite's commit counter.

    Only meaningful when compared against another reading *from the same
    connection* — use `ConsistencyWatch` for that. Kept for diagnostics.
    """
    if not database.exists():
        return None
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return int(connection.execute("PRAGMA data_version").fetchone()[0])
    finally:
        connection.close()


def snapshot_database(source: Path, destination: Path) -> None:
    """Copy one database with SQLite's backup API, WAL content included."""
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def coordinated_snapshot(*, barrier: SnapshotBarrier,
                         databases: dict[str, Path],
                         # Returns whatever the caller finds useful; the
                         # protocol only cares that it ran between the probes.
                         copy: Callable[[], object],
                         trees: dict[str, Callable[[], str]] | None = None,
                         # Returns whatever it likes; only its effect matters.
                         drain: Callable[[], object] | None = None,
                         attempts: int = MAX_ATTEMPTS,
                         reason: str = "backup",
                         settle_seconds: float = 0.0,
                         ) -> list[SnapshotAttempt]:
    """Copy every store under one barrier, proving nothing changed underneath.

    Returns the attempts made, the last of which succeeded. Raises
    `SnapshotRefused` when consistency could not be established, having written
    nothing that a caller should keep — a torn backup is worse than none,
    because it is trusted exactly when everything else has already failed.
    """
    tried: list[SnapshotAttempt] = []

    for number in range(1, attempts + 1):
        with barrier.hold(reason=reason):
            if drain is not None:
                drain()
            if settle_seconds:
                time.sleep(settle_seconds)

            watch = ConsistencyWatch(databases, trees)
            try:
                copy()
                changed = watch.changed()
            finally:
                watch.close()
        if not changed:
            tried.append(SnapshotAttempt(number, True))
            return tried

        detail = ("a writer committed during the snapshot: "
                  + ", ".join(changed))
        logger.warning("Snapshot attempt %d was not consistent: %s",
                       number, detail)
        tried.append(SnapshotAttempt(number, False, detail, changed))

    raise SnapshotRefused(
        f"Could not take a consistent snapshot in {attempts} attempts. "
        f"Last: {tried[-1].detail}. Nothing was written that should be kept — "
        "stop the writers, or retry when the service is quiet.")
