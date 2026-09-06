"""Exact capture into the vault (vault §5).

Scribe's job is **preservation**, not improvement. The body is stored byte-for-
byte, including trailing whitespace and any pipes, because the user asked Loop
to remember what they said — not a tidied version of it (V02). Frontmatter is
serialised by a YAML library rather than string formatting, so a title with a
colon or a quote cannot corrupt the document.

Two identity rules matter:

* A collision on ``YYYY-MM-DD-<slug>.md`` allocates a suffix from the stable
  capture ID, and a **retry reuses the same reserved path**. Without the reserved
  path a replayed capture would create a second file (V03, V05).
* The reply may only say "saved to your vault" once *both* the file and the
  ledger row exist. If only SQLite accepted the text, the honest wording is
  "queued for the vault" — and a retry job is retained.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

import yaml

from loop.core.errors import InvalidInput
from loop.core.ids import content_hash, new_id
from loop.core.privacy import PrivacyLabel
from loop.vault.gateway import VaultGateway, WriteMode
from loop.vault.ledger import Ledger, LedgerRow, render

logger = logging.getLogger(__name__)

SOURCE_KINDS = ("clip", "note", "transcript", "doc", "thread")

#: Words that make a poor slug on their own.
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


@dataclass
class CaptureRequest:
    """What the user asked to preserve."""

    body: str
    title: str | None = None
    source_kind: str = "note"
    origin: str = "gleb"
    capture_id: str = field(default_factory=new_id)
    privacy: PrivacyLabel = field(default_factory=PrivacyLabel.for_unlabelled_import)


@dataclass
class CaptureResult:
    """The outcome, stated precisely enough to quote back to the user."""

    capture_id: str
    path: str
    status: str            # saved | queued | duplicate
    body_hash: str
    registered: bool

    @property
    def message(self) -> str:
        """Wording that matches what actually happened."""
        if self.status == "saved":
            return f"Saved to your vault: {self.path}"
        if self.status == "duplicate":
            return f"Already saved: {self.path}"
        return f"Queued for the vault (not yet written): {self.path}"


def slugify(text: str) -> str:
    slug = _SLUG_STRIP.sub("-", text.strip().lower()).strip("-")
    return slug[:60] or "note"


def derive_title(body: str) -> str:
    """Use the first meaningful words when no title was given.

    A missing title must never block a short capture (vault §5). Pipes are
    stripped here because the *body* may legitimately contain them (V02) and a
    machine-derived title must not inherit a character that would corrupt the
    ledger row. An explicitly supplied title with a pipe is still rejected —
    that is a user error worth reporting, not something to silently rewrite.
    """
    for line in body.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            words = stripped.replace("|", " ").split()
            return " ".join(words[:8])
    return "Untitled note"


def build_document(request: CaptureRequest, *, added: date) -> str:
    """Render frontmatter + the exact body.

    The body is concatenated verbatim; only the frontmatter is serialised.
    """
    if request.source_kind not in SOURCE_KINDS:
        raise InvalidInput(f"Unknown source-kind {request.source_kind!r}")

    title = request.title or derive_title(request.body)
    if "|" in title:
        # A pipe in a *title* would corrupt the ledger row; a pipe in the body
        # is fine and must be preserved.
        raise InvalidInput("A capture title may not contain a pipe character.")

    frontmatter = yaml.safe_dump(
        {"type": "source", "source-kind": request.source_kind, "title": title,
         "added": added.isoformat(), "origin": request.origin},
        sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{frontmatter}---\n\n{request.body}"


class CaptureService:
    """Writes an exact capture and registers it, as one journalled operation."""

    def __init__(self, *, gateway: VaultGateway, ledger: Ledger | None = None,
                 inbox: str = "0-raw/inbox") -> None:
        self._gateway = gateway
        self._ledger = ledger or Ledger(gateway)
        self._inbox = inbox

    # ------------------------------------------------------------------ #
    # Path reservation
    # ------------------------------------------------------------------ #
    def reserve_path(self, request: CaptureRequest, *, today: date,
                     desired_hash: str | None = None) -> str:
        """Allocate the capture's path.

        A retry must land on the *same* path, or it creates a sibling file for a
        note the user only wrote once. The base path is therefore reused when
        the file already there is byte-identical to what we are about to write —
        that is our own earlier attempt. A *different* note with the same title
        on the same day gets a suffix derived from its stable capture ID, so its
        own retries are equally deterministic (V03, V05).
        """
        title = request.title or derive_title(request.body)
        base = f"{today.isoformat()}-{slugify(title)}"
        candidate = f"{self._inbox}/{base}.md"

        if not self._gateway.exists(candidate):
            return candidate
        if desired_hash is not None and self._gateway.hash_of(candidate) == desired_hash:
            return candidate      # our own earlier attempt at this capture

        return f"{self._inbox}/{base}-{request.capture_id[:8]}.md"

    # ------------------------------------------------------------------ #
    # Capture
    # ------------------------------------------------------------------ #
    def capture(self, request: CaptureRequest, *, today: date,
                batch: str = "inbox") -> CaptureResult:
        """Write the raw file and register it in the ledger.

        Both happen inside one journalled transaction, so a crash between them
        leaves a recoverable record rather than an unidentifiable orphan (V04).
        """
        if not request.body.strip():
            raise InvalidInput("Refusing to capture an empty note.")

        document = build_document(request, added=today)
        body_hash = content_hash(document)
        path = self.reserve_path(request, today=today, desired_hash=body_hash)

        # A completed identical capture is a replay, not a new note (V05).
        if self._gateway.exists(path):
            existing = self._gateway.hash_of(path)
            if existing == body_hash:
                registered = self._ledger.find(path) is not None
                return CaptureResult(capture_id=request.capture_id, path=path,
                                     status="duplicate" if registered else "queued",
                                     body_hash=body_hash, registered=registered)

        file_op = self._gateway.make_operation(path, document,
                                               mode=WriteMode.CREATE_NEW)
        rows = self._ledger.register(LedgerRow(
            source=path, batch=batch, added=today.isoformat(), status="pending"))
        ledger_hash = self._gateway.hash_of(self._ledger.relative_path)
        ledger_op = self._gateway.make_operation(
            self._ledger.relative_path, render(rows), mode=WriteMode.REPLACE,
            expected_hash=ledger_hash)

        journal = self._gateway.begin([file_op, ledger_op])
        self._gateway.apply(journal)

        return CaptureResult(capture_id=request.capture_id, path=path,
                             status="saved", body_hash=body_hash, registered=True)

    # ------------------------------------------------------------------ #
    # Recovery
    # ------------------------------------------------------------------ #
    def reconcile(self, *, today: date, batch: str = "inbox") -> list[str]:
        """Register raw files that exist without a ledger row (V04).

        A crash between the file write and the ledger update leaves exactly this
        state; reconciliation registers the orphan by its reserved identity
        instead of writing a second file.
        """
        inbox_dir = self._gateway.resolve(self._inbox)
        if not inbox_dir.is_dir():
            return []

        raw_paths = [f"{self._inbox}/{p.name}" for p in sorted(inbox_dir.iterdir())
                     if p.suffix == ".md"]
        orphans = self._ledger.orphans(raw_paths)
        if not orphans:
            return []

        rows = self._ledger.rows()
        for orphan in orphans:
            rows.append(LedgerRow(source=orphan, batch=batch,
                                  added=today.isoformat(), status="pending"))

        ledger_hash = self._gateway.hash_of(self._ledger.relative_path)
        operation = self._gateway.make_operation(
            self._ledger.relative_path, render(rows), mode=WriteMode.REPLACE,
            expected_hash=ledger_hash)
        self._gateway.apply(self._gateway.begin([operation]))
        return orphans
