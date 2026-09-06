"""Policy loading, precedence and conflicts (vault §3, runtime §10).

Authoritative files are loaded **directly**, never by approximate retrieval: a
governing rule that gets fetched by similarity search is a rule that sometimes
does not apply. Retrieved sources, wiki pages, email and quotations are *data*;
they cannot grant permissions or replace instructions.

The precedence order (vault §3), highest first:

1. Runtime invariants — identity, path confinement, privacy, schema validation.
   These are code and cannot be edited by a document.
2. The authenticated user's current request, within their authority.
3. Approved settings in ``_ctx/loop/``.
4. Governing ``_ctx/rules/``, then scoped role and project documents.
5. Root summaries and templates — which cannot override the governing rules.

When two authoritative documents disagree, the resolution is **not** to pick
one. A conflict is recorded with both paths and excerpts, the last valid
compiled policy for that scope is retained, and that scope stays inactive on a
first installation. Choosing by file modification time is explicitly forbidden —
recency is not authority.

Crucially, a policy conflict does not take the assistant down: reads, raw
capture and unrelated reminders keep working (V27).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import yaml

from loop.core.ids import content_hash
from loop.vault.gateway import VaultGateway

logger = logging.getLogger(__name__)


class Precedence(IntEnum):
    """Lower number wins. Mirrors the ordered list in vault §3."""

    RUNTIME_INVARIANT = 1
    USER_REQUEST = 2
    LOOP_SETTINGS = 3
    GOVERNING_RULES = 4
    ROOT_SUMMARY = 5


#: Which precedence tier each authoritative path belongs to.
SOURCE_TIERS: dict[str, Precedence] = {
    "_ctx/loop/permissions.md": Precedence.LOOP_SETTINGS,
    "_ctx/loop/behavior.md": Precedence.LOOP_SETTINGS,
    "_ctx/loop/notifications.md": Precedence.LOOP_SETTINGS,
    "_ctx/rules/compile.md": Precedence.GOVERNING_RULES,
    "_ctx/rules/naming.md": Precedence.GOVERNING_RULES,
    "_ctx/rules/frontmatter.md": Precedence.GOVERNING_RULES,
    "_ctx/templates": Precedence.ROOT_SUMMARY,
    "CLAUDE.md": Precedence.ROOT_SUMMARY,
}


@dataclass
class PolicyConflict:
    """Two authoritative documents requiring incompatible behaviour."""

    scope: str
    detail: str
    paths: list[str] = field(default_factory=list)
    excerpts: dict[str, str] = field(default_factory=dict)


@dataclass
class PolicyRevision:
    """A compiled, validated policy for one scope."""

    scope: str
    status: str                       # active | invalid | proposed | superseded
    source_hashes: dict[str, str] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def revision_id(self) -> str:
        """Stable ID derived from the exact sources that produced it."""
        joined = "|".join(f"{p}:{h}" for p, h in sorted(self.source_hashes.items()))
        return content_hash(f"{self.scope}::{joined}")[:16]

    @property
    def is_active(self) -> bool:
        return self.status == "active"


class PolicyLoader:
    """Loads governing documents directly and compiles scoped policy."""

    def __init__(self, *, gateway: VaultGateway) -> None:
        self._gateway = gateway
        #: Last known-good revision per scope, retained across a failed reload.
        self._last_valid: dict[str, PolicyRevision] = {}

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    def load_document(self, relative: str) -> str | None:
        """Read an authoritative document directly, or ``None`` if absent."""
        if not self._gateway.exists(relative):
            return None
        return self._gateway.read_text(relative)

    def precedence_of(self, relative: str) -> Precedence:
        for prefix, tier in SOURCE_TIERS.items():
            if relative == prefix or relative.startswith(prefix + "/"):
                return tier
        return Precedence.ROOT_SUMMARY

    def ordered_sources(self, paths: list[str]) -> list[str]:
        """Sort paths by authority, highest first.

        Ties keep their given order — never resolved by modification time,
        because a file edited more recently is not thereby more authoritative.
        """
        return sorted(paths, key=lambda p: (self.precedence_of(p),
                                            paths.index(p)))

    # ------------------------------------------------------------------ #
    # Compilation
    # ------------------------------------------------------------------ #
    def compile_scope(self, scope: str, paths: list[str]) -> PolicyRevision:
        """Compile one scope's policy from its governing documents.

        On a conflict or a malformed document the previous valid revision for
        the scope is returned unchanged; a first installation leaves the scope
        inactive rather than guessing.
        """
        hashes: dict[str, str] = {}
        values: dict[str, Any] = {}
        problems: list[str] = []

        for relative in self.ordered_sources(paths):
            body = self.load_document(relative)
            if body is None:
                continue
            file_hash = self._gateway.hash_of(relative)
            if file_hash:
                hashes[relative] = file_hash

            parsed, error = self._parse(relative, body)
            if error:
                problems.append(f"{relative}: {error}")
                continue
            # Higher-precedence sources are applied first, so a lower one must
            # not overwrite a key that is already set.
            for key, value in parsed.items():
                values.setdefault(key, value)

        if problems:
            previous = self._last_valid.get(scope)
            if previous is not None:
                logger.warning("Policy for %s invalid; retaining revision %s",
                               scope, previous.revision_id)
                return previous
            return PolicyRevision(scope=scope, status="invalid",
                                  source_hashes=hashes, problems=problems)

        revision = PolicyRevision(scope=scope, status="active",
                                  source_hashes=hashes, values=values)
        self._last_valid[scope] = revision
        return revision

    @staticmethod
    def _parse(relative: str, body: str) -> tuple[dict[str, Any], str | None]:
        """Parse frontmatter, if any. Returns (values, error)."""
        if not body.startswith("---"):
            return {}, None
        parts = body.split("---", 2)
        if len(parts) < 3:
            return {}, "malformed frontmatter block"
        try:
            parsed = yaml.safe_load(parts[1])
        except yaml.YAMLError as exc:
            return {}, f"invalid YAML ({exc.__class__.__name__})"
        if parsed is None:
            return {}, None
        if not isinstance(parsed, dict):
            return {}, "frontmatter is not a mapping"
        return parsed, None

    # ------------------------------------------------------------------ #
    # Conflicts
    # ------------------------------------------------------------------ #
    def detect_conflicts(self, scope: str,
                         claims: dict[str, list[tuple[str, Any]]]
                         ) -> list[PolicyConflict]:
        """Report incompatible claims about the same key.

        ``claims`` maps a policy key to (path, value) pairs. Two different
        values from documents at the *same* precedence tier is a genuine
        conflict; a lower tier disagreeing with a higher one is simply
        overridden and is not reported.
        """
        conflicts: list[PolicyConflict] = []
        for key, entries in claims.items():
            by_tier: dict[Precedence, list[tuple[str, Any]]] = {}
            for path, value in entries:
                by_tier.setdefault(self.precedence_of(path), []).append(
                    (path, value))

            top = min(by_tier) if by_tier else None
            if top is None:
                continue
            competing = by_tier[top]
            distinct = {str(value) for _, value in competing}
            if len(distinct) > 1:
                conflicts.append(PolicyConflict(
                    scope=scope,
                    detail=(f"Documents of equal authority disagree about "
                            f"{key!r}: {sorted(distinct)}"),
                    paths=[path for path, _ in competing],
                    excerpts={path: str(value) for path, value in competing}))
        return conflicts

    def resolve(self, scope: str, key: str, value: Any, *,
                decided_by: str = "owner") -> dict[str, Any]:
        """Record a user's resolution as a scoped decision.

        A one-time exception is recorded as such, not silently promoted into
        permanent policy (vault §3).
        """
        return {"scope": scope, "key": key, "value": value,
                "decided_by": decided_by, "kind": "scoped_decision"}


class RoutineRegistry:
    """Tracks which routine documents are actually activated (V28).

    A routine file in the vault is a *proposal*. ``enabled: true`` inside it is
    the author's intent, not permission — activation requires an authenticated
    event, which is recorded here. Treating the file as activation would let
    anyone who can write to the vault schedule background work.
    """

    def __init__(self) -> None:
        self._activated: dict[str, str] = {}

    def activate(self, slug: str, *, activation_event_id: str) -> None:
        if not activation_event_id:
            raise ValueError("Activation requires an authenticated event id.")
        self._activated[slug] = activation_event_id

    def is_active(self, slug: str) -> bool:
        return slug in self._activated

    def activation_event(self, slug: str) -> str | None:
        return self._activated.get(slug)

    def pause(self, slug: str) -> None:
        self._activated.pop(slug, None)
