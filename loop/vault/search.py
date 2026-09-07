"""Lexical retrieval over the vault (vault §6, runtime §4).

FTS5 rather than embeddings, deliberately. Embeddings are a **disposable
derivative**: if Ollama is unavailable or no embedding model is selected, search
must still work. A knowledge base that becomes unsearchable when a model service
is down is not a knowledge base (V25).

Two properties the index must preserve:

* **Privacy filtering happens in the query, not after it.** Filtering results
  after ranking would still have loaded private content into memory and into
  whatever produced the ranking. Local-only rows are excluded by the SQL itself.
* **Source layer is retained.** A hit from `_archive/` is not the same as a hit
  from the live wiki, and a raw source is not a compiled page. Callers need that
  distinction to avoid grounding output in the wrong layer (V23).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.privacy import PrivacyLabel

logger = logging.getLogger(__name__)

#: Vault layers, so a caller can tell a compiled page from a raw note.
LAYER_RAW = "raw"
LAYER_WIKI = "wiki"
LAYER_JOURNAL = "journal"
LAYER_OUTPUT = "output"
LAYER_ARCHIVE = "archive"


def layer_for(path: str) -> str:
    """Classify a vault path into its layer."""
    if path.startswith("_archive/"):
        return LAYER_ARCHIVE
    if path.startswith("0-raw/"):
        return LAYER_RAW
    if path.startswith("1-wiki/"):
        return LAYER_WIKI
    if path.startswith("3-output/"):
        return LAYER_OUTPUT
    if path.startswith("4-journal/"):
        return LAYER_JOURNAL
    return "other"


@dataclass
class SearchHit:
    """One retrieval result, with enough context to cite it."""

    path: str
    title: str
    layer: str
    snippet: str
    rank: float = 0.0
    local_only: bool = False


@dataclass
class IndexEntry:
    """A document to index."""

    path: str
    title: str
    body: str
    privacy: PrivacyLabel = field(
        default_factory=PrivacyLabel.for_unlabelled_import)

    @property
    def layer(self) -> str:
        return layer_for(self.path)


def _fts_query(user_query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    FTS5 treats punctuation as syntax, so a query containing a quote or a minus
    sign is a syntax error rather than a search. Terms are extracted and quoted.
    """
    terms = re.findall(r"[\w']+", user_query, flags=re.UNICODE)
    if not terms:
        return ""
    return " OR ".join(f'"{term}"' for term in terms)


class VaultSearch:
    """A lexical FTS5 index over vault documents."""

    def __init__(self, *, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with self._sessions() as session:
            session.execute(text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts USING fts5("
                "path, title, body, layer UNINDEXED, "
                "local_only UNINDEXED, tokenize = 'unicode61')"))
            session.commit()

    # ------------------------------------------------------------------ #
    # Indexing
    # ------------------------------------------------------------------ #
    def index(self, entry: IndexEntry) -> None:
        """Index one document, replacing any previous row for its path."""
        with self._sessions() as session:
            session.execute(text("DELETE FROM vault_fts WHERE path = :path"),
                            {"path": entry.path})
            session.execute(text(
                "INSERT INTO vault_fts (path, title, body, layer, local_only) "
                "VALUES (:path, :title, :body, :layer, :local_only)"),
                {"path": entry.path, "title": entry.title, "body": entry.body,
                 "layer": entry.layer,
                 "local_only": 1 if entry.privacy.is_local_only else 0})
            session.commit()

    def index_many(self, entries: list[IndexEntry]) -> int:
        for entry in entries:
            self.index(entry)
        return len(entries)

    def indexed_paths(self, *, layers: list[str] | None = None) -> set[str]:
        """Every path currently indexed, optionally within given layers.

        Reindexing needs this to notice a *deletion*: a file removed from the
        vault would otherwise stay searchable and citable forever, because
        nothing else ever revisits a row once written.
        """
        sql = "SELECT path FROM vault_fts"
        params: dict[str, Any] = {}
        if layers:
            placeholders = ", ".join(f":layer{i}" for i in range(len(layers)))
            sql += f" WHERE layer IN ({placeholders})"
            params.update({f"layer{i}": layer for i, layer in enumerate(layers)})
        with self._sessions() as session:
            return {row[0] for row in session.execute(text(sql), params).all()}

    def remove(self, path: str) -> None:
        """Drop a document from the index (forgetting, or an archived move)."""
        with self._sessions() as session:
            session.execute(text("DELETE FROM vault_fts WHERE path = :path"),
                            {"path": path})
            session.commit()

    def count(self) -> int:
        with self._sessions() as session:
            return int(session.execute(
                text("SELECT COUNT(*) FROM vault_fts")).scalar() or 0)

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #
    def search(self, query: str, *, limit: int = 10,
               layers: list[str] | None = None,
               include_archive: bool = False,
               allow_local_only: bool = True) -> list[SearchHit]:
        """Lexical search with privacy and layer filters applied in SQL.

        ``allow_local_only=False`` excludes private rows *in the query*, so a
        caller preparing content for a cloud model never loads them at all.
        """
        match = _fts_query(query)
        if not match:
            return []

        clauses = ["vault_fts MATCH :match"]
        params: dict[str, Any] = {"match": match, "limit": limit}

        if not allow_local_only:
            clauses.append("local_only = 0")
        if not include_archive:
            clauses.append("layer != :archive")
            params["archive"] = LAYER_ARCHIVE
        if layers:
            placeholders = ", ".join(f":layer{i}" for i in range(len(layers)))
            clauses.append(f"layer IN ({placeholders})")
            params.update({f"layer{i}": layer for i, layer in enumerate(layers)})

        sql = (
            "SELECT path, title, layer, local_only, "
            "  snippet(vault_fts, 2, '[', ']', '…', 12) AS snip, rank "
            "FROM vault_fts WHERE " + " AND ".join(clauses) +
            " ORDER BY rank LIMIT :limit")

        with self._sessions() as session:
            rows = session.execute(text(sql), params).all()

        return [SearchHit(path=row[0], title=row[1], layer=row[2],
                          local_only=bool(row[3]), snippet=row[4],
                          rank=float(row[5] or 0.0))
                for row in rows]

    def title_search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        """Title-only lookup, for when a user names a page directly."""
        match = _fts_query(query)
        if not match:
            return []
        with self._sessions() as session:
            rows = session.execute(text(
                "SELECT path, title, layer, local_only, title, rank "
                "FROM vault_fts WHERE title MATCH :match "
                "ORDER BY rank LIMIT :limit"),
                {"match": match, "limit": limit}).all()
        return [SearchHit(path=row[0], title=row[1], layer=row[2],
                          local_only=bool(row[3]), snippet=row[4],
                          rank=float(row[5] or 0.0))
                for row in rows]


# --------------------------------------------------------------------------- #
# Meeting notes (V31)
# --------------------------------------------------------------------------- #
@dataclass
class MeetingNote:
    """A journal entry for a meeting, with its own schema and path."""

    date_iso: str
    title: str
    attendees: list[str] = field(default_factory=list)
    body: str = ""
    actions: list[str] = field(default_factory=list)

    @property
    def path(self) -> str:
        """Meetings live under the journal year, not the wiki (vault §1)."""
        year = self.date_iso[:4]
        slug = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        return f"4-journal/meetings/{year}/{self.date_iso}-{slug}.md"

    def to_markdown(self) -> str:
        import yaml

        frontmatter = yaml.safe_dump(
            {"type": "meeting", "title": self.title, "date": self.date_iso,
             "attendees": list(self.attendees)},
            sort_keys=False, allow_unicode=True)
        parts = [f"---\n{frontmatter}---\n\n# {self.title}\n"]
        if self.body:
            parts.append(f"\n{self.body}\n")
        if self.actions:
            parts.append("\n## Actions\n\n"
                         + "\n".join(f"- {a}" for a in self.actions) + "\n")
        return "".join(parts)


def reusable_facts(note: MeetingNote) -> list[str]:
    """Facts worth capturing separately before any wiki compilation (V31).

    A meeting note is a journal record, not knowledge. Durable facts mentioned
    in it are captured as their own sources first, so the wiki is grounded in
    sourced captures rather than in a meeting write-up.
    """
    facts: list[str] = []
    for line in note.body.splitlines():
        stripped = line.strip("- ").strip()
        if not stripped:
            continue
        # A crude but honest heuristic: statements about the world, not about
        # the meeting itself. The point is that they are captured separately,
        # not that this classifier is clever.
        if any(marker in stripped.lower()
               for marker in (" is ", " are ", " takes ", " costs ", " requires ")):
            facts.append(stripped)
    return facts
