"""The six-column source ledger (vault §5).

The ledger is a Markdown table, which means every cell is delimited by ``|`` —
and source bodies routinely contain pipes. Writing a row without escaping them
silently corrupts the table and, worse, changes what later reads believe about
other sources. Cells are therefore escaped on write and unescaped on read, and
a title containing a pipe is rejected outright while the *body* keeps its pipes
untouched (V02).

Rows are updated by **exact normalised identity**, never by global string
replacement: two sources whose paths share a prefix would otherwise clobber each
other. Identity is the original raw path, retained even after archival, so an
old wiki page's provenance still resolves (V21).
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from typing import Any

from loop.core.errors import Conflict, InvalidInput

logger = logging.getLogger(__name__)

HEADER = ("| source | batch | added | status | compiled | pages produced |\n"
          "|---|---|---|---|---|---|\n")

#: Exactly the statuses the vault contract defines.
STATUSES = ("pending", "compiled", "unfetched", "unfetchable", "rejected",
            "superseded")

EMPTY = "—"


def escape_cell(value: str) -> str:
    """Make a value safe inside a Markdown table cell.

    Pipes become ``\\|`` and newlines become spaces: either would end the cell
    early and shift every following column.
    """
    text = str(value if value is not None else "")
    return text.replace("|", r"\|").replace("\n", " ").replace("\r", " ").strip()


def unescape_cell(value: str) -> str:
    return str(value).replace(r"\|", "|").strip()


def normalise_identity(path: str) -> str:
    """NFC form, for comparing paths that may be stored decomposed (V19)."""
    return unicodedata.normalize("NFC", str(path).strip())


@dataclass
class LedgerRow:
    """One source's processing state."""

    source: str
    batch: str
    added: str
    status: str = "pending"
    compiled: str = EMPTY
    pages_produced: str = EMPTY

    @property
    def identity(self) -> str:
        return normalise_identity(self.source)

    def to_cells(self) -> list[str]:
        return [escape_cell(self.source), escape_cell(self.batch),
                escape_cell(self.added), escape_cell(self.status),
                escape_cell(self.compiled), escape_cell(self.pages_produced)]

    def to_line(self) -> str:
        return "| " + " | ".join(self.to_cells()) + " |\n"


def parse(markdown: str) -> list[LedgerRow]:
    """Parse a ledger document into rows, skipping header and separator."""
    rows: list[LedgerRow] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if set(stripped) <= set("|- "):
            continue                      # separator row
        cells = _split_row(stripped)
        if len(cells) < 6:
            continue
        if cells[0].lower() == "source":
            continue                      # header row
        rows.append(LedgerRow(
            source=cells[0], batch=cells[1], added=cells[2], status=cells[3],
            compiled=cells[4], pages_produced=cells[5]))
    return rows


def _split_row(line: str) -> list[str]:
    """Split a table row on unescaped pipes only."""
    cells: list[str] = []
    current: list[str] = []
    index = 0
    body = line.strip().strip("|")
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body) and body[index + 1] == "|":
            current.append("|")
            index += 2
            continue
        if char == "|":
            cells.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    cells.append("".join(current).strip())
    return cells


def render(rows: list[LedgerRow]) -> str:
    return HEADER + "".join(row.to_line() for row in rows)


class Ledger:
    """Reads and updates the ledger through the gateway."""

    def __init__(self, gateway: Any, *,
                 relative_path: str = "0-raw/_ledger.md") -> None:
        self._gateway = gateway
        self.relative_path = relative_path

    def rows(self) -> list[LedgerRow]:
        if not self._gateway.exists(self.relative_path):
            return []
        return parse(self._gateway.read_text(self.relative_path))

    def find(self, source: str) -> LedgerRow | None:
        wanted = normalise_identity(source)
        for row in self.rows():
            if row.identity == wanted:
                return row
        return None

    def identities(self) -> set[str]:
        return {row.identity for row in self.rows()}

    # ------------------------------------------------------------------ #
    # Mutation
    # ------------------------------------------------------------------ #
    def register(self, row: LedgerRow) -> list[LedgerRow]:
        """Append a row, refusing a duplicate identity.

        Returns the full row list for the caller to write through the gateway,
        so the ledger update joins the same journalled transaction as the file.
        """
        if "|" in row.source:
            raise InvalidInput("A source path may not contain a pipe character.")
        if row.status not in STATUSES:
            raise InvalidInput(f"Unknown ledger status {row.status!r}")

        rows = self.rows()
        if any(existing.identity == row.identity for existing in rows):
            raise Conflict(f"{row.source} is already registered.",
                           details={"source": row.source})
        rows.append(row)
        return rows

    def set_status(self, source: str, status: str, *,
                   compiled: str | None = None,
                   pages_produced: str | None = None) -> list[LedgerRow]:
        """Update one row by exact identity. Never a global replacement."""
        if status not in STATUSES:
            raise InvalidInput(f"Unknown ledger status {status!r}")

        wanted = normalise_identity(source)
        rows = self.rows()
        matched = False
        for row in rows:
            if row.identity != wanted:
                continue
            row.status = status
            if compiled is not None:
                row.compiled = compiled
            if pages_produced is not None:
                row.pages_produced = pages_produced
            matched = True
        if not matched:
            raise InvalidInput(f"No ledger row for {source!r}")
        return rows

    # ------------------------------------------------------------------ #
    # Reconciliation
    # ------------------------------------------------------------------ #
    def orphans(self, raw_paths: list[str]) -> list[str]:
        """Raw files with no ledger row, compared in NFC form (V19).

        Comparing raw bytes would report every decomposed macOS filename as a
        missing orphan and then register a duplicate for it.
        """
        registered = self.identities()
        return [path for path in raw_paths
                if normalise_identity(path) not in registered]

    def normalisation_collisions(self, raw_paths: list[str]) -> list[list[str]]:
        """Distinct files whose names normalise to the same identity (V20).

        Reported as an explicit conflict; picking one silently would make the
        other unreachable.
        """
        seen: dict[str, list[str]] = {}
        for path in raw_paths:
            seen.setdefault(normalise_identity(path), []).append(path)
        return [group for group in seen.values() if len(group) > 1]
