"""Knowledge Specialist — Obsidian capture with PARA + Zettelkasten.

Watches the Obsidian vault(s) marked "shared with AI". When the user sends a
note via chat, it extracts the core idea, suggests the correct PARA folder,
formats it as a Zettelkasten slip, and (after approval) writes the markdown to
the vault. Also powers semantic search across the vault.

Phase 1 scope:
    - Provide the interfaces used by later phases (kept as stubs).

Phase 2 delivers: vault watcher, PARA/Zettelkasten formatter, ChromaDB index,
and `loop find "topic"` semantic search. Private vaults are indexed locally only.
"""

from __future__ import annotations

from dataclasses import dataclass

from config.settings import Settings, get_settings


@dataclass
class NoteDraft:
    """A formatted note ready to be written to the vault (after approval)."""

    title: str
    body: str
    para_folder: str  # Projects | Areas | Resources | Archive
    tags: list[str]
    links: list[str]


class KnowledgeSpecialist:
    """Focused sub-agent for the Obsidian knowledge base."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # TODO(phase2): accept the Obsidian connector, ChromaDB store, LLMRouter.

    def format_note(self, raw_text: str) -> NoteDraft:
        """Turn raw captured text into a PARA-classified Zettelkasten slip."""
        # TODO(phase2): extract idea, classify PARA folder, add tags + links.
        raise NotImplementedError("KnowledgeSpecialist.format_note is a Phase 2 stub.")

    def write_note(self, draft: NoteDraft) -> str:
        """Write an approved note to the vault; return the file path."""
        # TODO(phase2): render markdown and write via the Obsidian connector.
        raise NotImplementedError("KnowledgeSpecialist.write_note is a Phase 2 stub.")

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        """Semantic search across the vault; return matching note paths."""
        # TODO(phase2): embed the query locally and query ChromaDB.
        raise NotImplementedError("KnowledgeSpecialist.search is a Phase 2 stub.")
