"""Read receipts and evidence validation (vault §6, runtime §4).

Compile rule 2: *compile only from a source currently read*. A fetched-and-read
page qualifies; remembering what a URL probably contains does not. This module
is what turns that sentence into something a machine can check.

A **read receipt** records which byte ranges of which source version were
actually read. Evidence citing a source is valid only when:

* a receipt exists for that source **in this run** — not a previous one,
* the content hash still matches, so the file has not changed since,
* and the cited span is inside a range that was genuinely covered.

The last point is why coverage is tracked as ranges rather than a boolean. A
long source read as a 2 KB preview would otherwise "have a receipt" while the
claim being cited sits in the unread remainder (V07). Classifying a source from
its opening lines is exactly the mistake the range check prevents.

Two consequences worth stating plainly:

* **A model's prose is not evidence.** An answer produced during research cites
  the sources it read; saving that answer resolves to those sources, never to
  the answer itself (V24).
* **File existence is not a receipt.** Swapping in a different vault backend
  cannot bypass this: the adapter must produce receipts, and a backend that only
  proves a file exists fails the same test the builtin one does (V29).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from loop.core.errors import ValidationFailed
from loop.core.ids import content_hash, new_id

logger = logging.getLogger(__name__)

#: Read this many bytes per chunk when covering a long source.
DEFAULT_CHUNK_BYTES = 4096
#: Overlap between chunks so a claim spanning a boundary is still covered.
DEFAULT_OVERLAP_BYTES = 256


@dataclass(frozen=True)
class ByteRange:
    """A half-open ``[start, end)`` byte range."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"invalid range {self.start}:{self.end}")

    def contains(self, other: ByteRange) -> bool:
        return self.start <= other.start and other.end <= self.end

    def overlaps_or_touches(self, other: ByteRange) -> bool:
        return not (other.start > self.end or self.start > other.end)


def merge_ranges(ranges: list[ByteRange]) -> list[ByteRange]:
    """Coalesce overlapping or adjacent ranges into a minimal set."""
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda r: (r.start, r.end))
    merged = [ordered[0]]
    for candidate in ordered[1:]:
        last = merged[-1]
        if last.overlaps_or_touches(candidate):
            merged[-1] = ByteRange(last.start, max(last.end, candidate.end))
        else:
            merged.append(candidate)
    return merged


@dataclass
class ReadReceipt:
    """Proof that a specific version of a source was actually read."""

    id: str
    run_id: str
    source_id: str
    path: str
    content_hash: str
    byte_count: int
    covered: list[ByteRange] = field(default_factory=list)
    read_at: int = 0

    @property
    def covered_bytes(self) -> int:
        return sum(r.end - r.start for r in merge_ranges(self.covered))

    @property
    def is_complete(self) -> bool:
        """True when every byte of the source was covered."""
        merged = merge_ranges(self.covered)
        return (len(merged) == 1 and merged[0].start == 0
                and merged[0].end >= self.byte_count)

    def covers(self, span: ByteRange) -> bool:
        return any(r.contains(span) for r in merge_ranges(self.covered))


@dataclass
class Evidence:
    """A claim's pointer back to the exact span that supports it."""

    source_id: str
    path: str
    span: ByteRange
    content_hash: str
    quote: str = ""


class ReceiptStore:
    """Collects receipts for one run and validates evidence against them."""

    def __init__(self, *, run_id: str) -> None:
        self.run_id = run_id
        self._receipts: dict[str, ReadReceipt] = {}

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    def record(self, *, source_id: str, path: str, body: bytes,
               covered: list[ByteRange], read_at: int = 0) -> ReadReceipt:
        """Record what was read. Merges with any existing receipt for the source."""
        digest = content_hash(body)
        existing = self._receipts.get(source_id)

        if existing is not None and existing.content_hash != digest:
            # The file changed mid-run. Earlier coverage described different
            # bytes and cannot be carried forward.
            logger.warning("Source %s changed during run %s; resetting coverage",
                           source_id, self.run_id)
            existing = None

        ranges = list(covered) if existing is None else existing.covered + covered
        receipt = ReadReceipt(
            id=existing.id if existing else new_id(), run_id=self.run_id,
            source_id=source_id, path=path, content_hash=digest,
            byte_count=len(body), covered=merge_ranges(ranges),
            read_at=read_at)
        self._receipts[source_id] = receipt
        return receipt

    def read_whole(self, *, source_id: str, path: str, body: bytes,
                   read_at: int = 0) -> ReadReceipt:
        """Record a complete read of a short source."""
        return self.record(source_id=source_id, path=path, body=body,
                           covered=[ByteRange(0, len(body))], read_at=read_at)

    def read_in_chunks(self, *, source_id: str, path: str, body: bytes,
                       chunk_bytes: int = DEFAULT_CHUNK_BYTES,
                       overlap: int = DEFAULT_OVERLAP_BYTES,
                       read_at: int = 0) -> ReadReceipt:
        """Read a long source in bounded overlapping chunks (V07).

        Overlapping matters: a claim straddling a chunk boundary would otherwise
        fall between two covered ranges and fail validation despite being read.
        """
        ranges: list[ByteRange] = []
        position = 0
        total = len(body)
        while position < total:
            end = min(position + chunk_bytes, total)
            ranges.append(ByteRange(position, end))
            if end >= total:
                break
            position = max(end - overlap, position + 1)
        return self.record(source_id=source_id, path=path, body=body,
                           covered=ranges or [ByteRange(0, 0)], read_at=read_at)

    def read_preview(self, *, source_id: str, path: str, body: bytes,
                     preview_bytes: int = 512, read_at: int = 0) -> ReadReceipt:
        """Record a *partial* read. Deliberately available, and deliberately
        insufficient for classification — see :meth:`validate`."""
        end = min(preview_bytes, len(body))
        return self.record(source_id=source_id, path=path, body=body,
                           covered=[ByteRange(0, end)], read_at=read_at)

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #
    def get(self, source_id: str) -> ReadReceipt | None:
        return self._receipts.get(source_id)

    def sources_read(self) -> set[str]:
        return set(self._receipts)

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def validate(self, evidence: list[Evidence], *,
                 require_complete: bool = False) -> None:
        """Raise unless every piece of evidence is backed by a receipt.

        Called **before** any wiki mutation (V06): the point is to refuse the
        write, not to discover afterwards that a page cites something unread.
        """
        problems: list[str] = []

        for item in evidence:
            receipt = self._receipts.get(item.source_id)
            if receipt is None:
                problems.append(
                    f"{item.source_id}: no read receipt in run {self.run_id}")
                continue
            if receipt.content_hash != item.content_hash:
                problems.append(
                    f"{item.source_id}: source changed since it was read")
                continue
            if not receipt.covers(item.span):
                problems.append(
                    f"{item.source_id}: cited bytes "
                    f"{item.span.start}:{item.span.end} were not read "
                    f"(covered {[(r.start, r.end) for r in receipt.covered]})")
                continue
            if require_complete and not receipt.is_complete:
                problems.append(
                    f"{item.source_id}: only {receipt.covered_bytes} of "
                    f"{receipt.byte_count} bytes were read")

        if problems:
            raise ValidationFailed(
                "Evidence validation failed; no wiki changes were made.",
                details={"problems": problems, "run_id": self.run_id})

    def classification_allowed(self, source_id: str) -> bool:
        """Whether a source may be classified from what was read.

        Never from a preview: deciding a source is "just a newsletter" from its
        first 512 bytes is how genuine content gets rejected (V07).
        """
        receipt = self._receipts.get(source_id)
        return receipt is not None and receipt.is_complete


@dataclass
class ProvenanceMap:
    """Maps an original source identity to its current location (V21).

    A source keeps its *original* raw path as identity even after archival, so a
    wiki page written years ago still resolves. Rewriting old pages to point at
    the new location would erase the historical trail the archive exists for.
    """

    moved: dict[str, str] = field(default_factory=dict)

    def archive(self, original_path: str, archived_path: str) -> None:
        self.moved[original_path] = archived_path

    def resolve(self, original_path: str) -> str:
        """Current on-disk location for an original identity."""
        return self.moved.get(original_path, original_path)

    def is_archived(self, original_path: str) -> bool:
        return original_path in self.moved


def require_compiled_sources(cited: list[str], compiled: set[str]) -> None:
    """Refuse to ground a deliverable in raw sources (V23, compile rule 6).

    Output derives from compiled wiki pages. A draft built straight from raw
    notes has skipped every check that compilation performs.
    """
    uncompiled = [source for source in cited if source not in compiled]
    if uncompiled:
        raise ValidationFailed(
            "These sources are not compiled yet; compile them or record a "
            "knowledge gap before drafting.",
            details={"uncompiled": uncompiled})
