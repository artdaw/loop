"""Shared fixtures for the vNext suite.

Hermetic by construction: temp SQLite, a frozen clock, blank settings and no
network, model or credentials (acceptance §1).
"""

from __future__ import annotations

import datetime as dt

import pytest

from loop.core.clock import UTC, FrozenClock
from loop.core.settings import Settings
from loop.db.migrations import migrate
from loop.db.session import create_db_engine, session_factory

#: 2026-09-05 09:00 Europe/Berlin, the instant several T-scenarios start from.
NOW = dt.datetime(2026, 9, 5, 7, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="test",
        timezone="Europe/Berlin",
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        data_dir=str(tmp_path / "data"),
        obsidian_vault_path="",
        telegram_chat_id="4242",
        telegram_user_id="99",
    )


@pytest.fixture
def engine(settings, clock):
    engine = create_db_engine(settings.database_url)
    migrate(engine, clock=clock)
    return engine


@pytest.fixture
def sessions(engine):
    return session_factory(engine)
