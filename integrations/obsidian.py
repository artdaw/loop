"""Obsidian integration — local filesystem access to the vault(s).

Reads and writes markdown files in the user's Obsidian vault, organised with
PARA folders. No network auth — purely local. Vaults flagged as private are
handled local-only (never indexed by a cloud LLM).

Phase 2 scope:
    - Watch the vault for changes (watchdog / inotify).
    - read_note()/write_note() for markdown files.
    - list_notes() to enumerate the vault for indexing.
"""

from __future__ import annotations

from pathlib import Path

from config.settings import Settings, get_settings


class ObsidianVault:
    """Local read/write access to an Obsidian vault."""

    PARA_FOLDERS = ("Projects", "Areas", "Resources", "Archive")

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.vault_path = Path(self.settings.obsidian_vault_path).expanduser()
        # TODO(phase2): validate the path; mark private vaults local-only.

    def list_notes(self) -> list[Path]:
        """Return all markdown note paths in the vault."""
        # TODO(phase2): glob **/*.md, skipping .obsidian/ and templates.
        raise NotImplementedError("ObsidianVault.list_notes is a Phase 2 stub.")

    def read_note(self, relative_path: str) -> str:
        """Read a note's markdown content."""
        # TODO(phase2): resolve safely under vault_path and read text.
        raise NotImplementedError("ObsidianVault.read_note is a Phase 2 stub.")

    def write_note(self, relative_path: str, content: str) -> Path:
        """Write markdown content to a note (after user approval)."""
        # TODO(phase2): ensure parent PARA folder exists; write atomically.
        raise NotImplementedError("ObsidianVault.write_note is a Phase 2 stub.")
