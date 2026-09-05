"""Obsidian integration — local filesystem access + a live vault watcher.

Reads and writes markdown files in the user's Obsidian vault(s), organised with
PARA folders. No network auth — purely local. A *private* vault can be
configured separately; every event coming from it is flagged ``local_only`` so
downstream code (Knowledge Specialist, LLM router) never sends it to a cloud LLM.

Phase 2 delivers:
    - ``ObsidianVault``: read/write/list markdown notes.
    - ``ObsidianWatcher``: a ``watchdog`` file-system watcher that emits a
      :class:`VaultEvent` to a callback on new/modified ``.md`` files. Files from
      the private vault carry ``local_only=True``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from config.settings import Settings, get_settings


@dataclass
class VaultEvent:
    """A change detected in an Obsidian vault."""

    path: Path            # absolute path to the changed note
    vault_path: Path      # the vault root the note belongs to
    change: str           # "created" | "modified"
    local_only: bool      # True when the note came from the private vault

    @property
    def relative_path(self) -> str:
        """Path of the note relative to its vault root."""
        try:
            return str(self.path.relative_to(self.vault_path))
        except ValueError:
            return self.path.name


class ObsidianVault:
    """Local read/write access to an Obsidian vault."""

    PARA_FOLDERS = ("Projects", "Areas", "Resources", "Archive")

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.vault_path = Path(self.settings.obsidian_vault_path).expanduser()

    def list_notes(self) -> list[Path]:
        """Return all markdown note paths in the vault (skipping .obsidian/)."""
        if not self.vault_path.exists():
            return []
        return [
            p for p in self.vault_path.rglob("*.md")
            if ".obsidian" not in p.parts and "templates" not in {part.lower() for part in p.parts}
        ]

    def read_note(self, relative_path: str) -> str:
        """Read a note's markdown content."""
        target = self._resolve(relative_path)
        return target.read_text(encoding="utf-8")

    def write_note(self, relative_path: str, content: str, *, overwrite: bool = False) -> Path:
        """Write markdown content to a note; refuses to overwrite unless asked."""
        target = self._resolve(relative_path)
        if target.exists() and not overwrite:
            raise FileExistsError(f"Note already exists: {target} (pass overwrite=True)")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def _resolve(self, relative_path: str) -> Path:
        """Resolve a path safely under the vault root (prevents traversal)."""
        candidate = (self.vault_path / relative_path).resolve()
        root = self.vault_path.resolve()
        if root not in candidate.parents and candidate != root:
            raise ValueError(f"Refusing to access path outside the vault: {relative_path}")
        return candidate


class _MarkdownEventHandler(FileSystemEventHandler):
    """Bridges watchdog events to a Loop callback, filtering for .md files."""

    def __init__(self, vault_path: Path, local_only: bool,
                 callback: Callable[[VaultEvent], None]) -> None:
        self._vault_path = vault_path
        self._local_only = local_only
        self._callback = callback

    def _emit(self, event: FileSystemEvent, change: str) -> None:
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        if path.suffix.lower() != ".md":
            return
        if ".obsidian" in path.parts:
            return
        self._callback(
            VaultEvent(
                path=path,
                vault_path=self._vault_path,
                change=change,
                local_only=self._local_only,
            )
        )

    def on_created(self, event: FileSystemEvent) -> None:
        self._emit(event, "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        self._emit(event, "modified")


class ObsidianWatcher:
    """Watches one or two Obsidian vaults and emits events on note changes.

    Usage::

        watcher = ObsidianWatcher(on_event=knowledge.on_vault_event)
        watcher.start()
        ...
        watcher.stop()
    """

    def __init__(self, on_event: Callable[[VaultEvent], None],
                 settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._on_event = on_event
        self._observer: BaseObserver | None = None
        self._lock = threading.Lock()

        self.vault_path = Path(self.settings.obsidian_vault_path).expanduser()
        private = self.settings.obsidian_private_vault_path.strip()
        self.private_vault_path: Path | None = (
            Path(private).expanduser() if private else None
        )

    def start(self) -> None:
        """Start watching the configured vault(s)."""
        with self._lock:
            if self._observer is not None:
                return
            observer = Observer()
            watched = False

            if self.vault_path.exists():
                observer.schedule(
                    _MarkdownEventHandler(self.vault_path, False, self._on_event),
                    str(self.vault_path),
                    recursive=True,
                )
                watched = True

            if self.private_vault_path and self.private_vault_path.exists():
                observer.schedule(
                    _MarkdownEventHandler(self.private_vault_path, True, self._on_event),
                    str(self.private_vault_path),
                    recursive=True,
                )
                watched = True

            if not watched:
                raise FileNotFoundError(
                    "No Obsidian vault found to watch. Set OBSIDIAN_VAULT_PATH "
                    "(and optionally OBSIDIAN_PRIVATE_VAULT_PATH) to existing dirs."
                )

            observer.start()
            self._observer = observer

    def stop(self) -> None:
        """Stop watching and release the observer thread."""
        with self._lock:
            if self._observer is None:
                return
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None

    @property
    def is_running(self) -> bool:
        """True while the watcher observer thread is alive."""
        return self._observer is not None and self._observer.is_alive()
