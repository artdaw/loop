"""Engine and session construction (runtime contract §4).

SQLite pragmas are set per connection: ``foreign_keys=ON`` so the schema's
reference constraints are actually enforced, WAL for concurrent readers,
``synchronous=FULL`` because durable commitments must survive a power loss, and
a busy timeout so a briefly-locked database waits rather than erroring.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker


def _apply_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=FULL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def create_db_engine(url: str) -> Engine:
    """Build an engine with the required SQLite pragmas applied."""
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, future=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _apply_pragmas)
    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False,
                        future=True)


def pragma(engine: Engine, name: str):
    """Read a pragma value, for diagnostics and tests."""
    with engine.connect() as conn:
        return conn.execute(text(f"PRAGMA {name}")).scalar()
