"""Shared pytest fixtures.

Every fixture is hermetic: temp SQLite files, no network, no Ollama, no
Wrike key. ``get_settings`` is lru_cached, so it is cleared before each
``Settings`` construction to stop one test's environment leaking into another.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import Settings, get_settings
from core.memory import MemoryStore


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """An empty Obsidian vault directory."""
    path = tmp_path / "vault"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def settings(tmp_path: Path, vault: Path) -> Settings:
    """Settings pointed entirely at a temp directory."""
    get_settings.cache_clear()
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        chroma_persist_dir=str(tmp_path / "chroma"),
        obsidian_vault_path=str(vault),
        anthropic_api_key="",
        wrike_api_key="",
    )


@pytest.fixture
def memory_store(settings: Settings) -> MemoryStore:
    """A bootstrapped MemoryStore on a temp SQLite file."""
    store = MemoryStore(settings)
    store.bootstrap()
    return store
