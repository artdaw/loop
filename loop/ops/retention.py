"""Retention maintenance and index rebuilding (vault §9, O10, O11).

Retention deletes *expiring operational state* — a fare quote, a forecast, an
expired approval. It does not touch the vault's evidence, and it never removes
anything an active piece of work still points at, because a task that outlives
the note explaining it is worse than a slightly larger database.

Index rebuilding is derivation, not repair: indexes are recomputed **from** the
sources, so a corrupted index is discarded and rebuilt while the sources stay
untouched. An index rebuild that writes to a source has misunderstood which of
the two is authoritative (O10).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class Category(str, Enum):
    """What a record is, which decides whether retention may remove it."""

    OPERATIONAL = "operational"          # forecasts, quotes, run artifacts
    VAULT_EVIDENCE = "vault_evidence"    # captures, receipts, ledger rows
    HUMAN_CONTENT = "human_content"      # anything a person wrote

    @property
    def is_expirable(self) -> bool:
        return self is Category.OPERATIONAL


@dataclass
class Record:
    id: str
    category: Category
    expires_at: int | None = None
    pinned_by: tuple[str, ...] = ()

    def is_expired(self, now: int) -> bool:
        return self.expires_at is not None and now >= self.expires_at


@dataclass
class RetentionPlan:
    remove: list[str] = field(default_factory=list)
    kept_pinned: list[str] = field(default_factory=list)
    kept_protected: list[str] = field(default_factory=list)

    @property
    def total_removed(self) -> int:
        return len(self.remove)


def plan_retention(records: list[Record], *, now: int,
                   active_refs: set[str] | None = None) -> RetentionPlan:
    """Decide what maintenance may remove (O11)."""
    active = active_refs or set()
    plan = RetentionPlan()

    for record in sorted(records, key=lambda r: r.id):
        if not record.category.is_expirable:
            plan.kept_protected.append(record.id)
            continue
        if not record.is_expired(now):
            continue
        if any(ref in active for ref in record.pinned_by):
            # Still referenced by live work. Expiry is a hint, not a mandate.
            plan.kept_pinned.append(record.id)
            continue
        plan.remove.append(record.id)

    return plan


@dataclass
class IndexRebuild:
    sources_read: list[str] = field(default_factory=list)
    entries_written: int = 0
    sources_modified: list[str] = field(default_factory=list)

    @property
    def sources_untouched(self) -> bool:
        return not self.sources_modified


def rebuild_index(sources: dict[str, str], *, indexer=None) -> IndexRebuild:
    """Recompute an index from its sources, writing to neither (O10).

    ``sources`` is a read-only mapping of source id to body. The function
    returns what it derived; it has no route to a source because the source is
    not its output.
    """
    result = IndexRebuild()
    before = dict(sources)

    entries: list[tuple[str, str]] = []
    for source_id in sorted(sources):
        result.sources_read.append(source_id)
        body = sources[source_id]
        derived = indexer(source_id, body) if indexer else [(source_id, body)]
        entries.extend(derived)

    result.entries_written = len(entries)
    result.sources_modified = [key for key, value in before.items()
                               if sources.get(key) != value]
    return result
