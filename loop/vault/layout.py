"""Where things live in a vault, and how to change it (vault contract §1).

Loop ships an **opinionated default layout** — numbered stages from raw capture
through compiled knowledge to output — because a system with no opinion about
structure cannot enforce rules like "deliverables derive from compiled pages,
never directly from raw". The stages are the rule.

But the *names* are not the rule. Someone whose notes already live in `sources/`
and `notes/` should not have to rename their vault to use Loop, so every path is
a key in a mapping that a vault can override in its own manifest. Code refers to
roles (`raw`, `wiki_concepts`, `ledger`); only this module knows the strings.

Two things a layout may not do, because they are correctness rather than taste:

* **Collapse two stages into one path.** Raw and wiki pointing at the same
  directory would make compiled output indistinguishable from its sources, and
  the immutability rule unenforceable.
* **Escape the vault root.** An absolute or parent-relative path is refused
  here, before the gateway ever sees it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from loop.core.errors import InvalidInput

logger = logging.getLogger(__name__)

#: The layout Loop ships with. Numbered stages make the pipeline order visible
#: in the file tree itself, which is why the default is opinionated.
DEFAULT_LAYOUT: dict[str, str] = {
    "root_guide": "CLAUDE.md",
    "raw": "0-raw",
    "inbox": "0-raw/inbox",
    "ledger": "0-raw/_ledger.md",
    "wiki": "1-wiki",
    "concepts": "1-wiki/concepts",
    "entities": "1-wiki/entities",
    "topics": "1-wiki/topics",
    "open_questions": "1-wiki/open-questions.md",
    "projects": "2-projects",
    "outputs": "3-output",
    "journal": "4-journal",
    "profile": "_mem/profile.md",
    "goals": "_mem/goals.md",
    "people": "_mem/people",
    "memory": "_mem/loop",
    "archive": "_archive",
    "context": "_ctx",
    "agents": "_ctx/agents",
    "compile_rules": "_ctx/rules/compile.md",
    "frontmatter_rules": "_ctx/rules/frontmatter.md",
    "naming_rules": "_ctx/rules/naming.md",
    "loop_context": "_ctx/loop",
}

#: Keys whose paths must stay distinct: each is a different pipeline stage, and
#: merging two of them removes a boundary the compile rules depend on.
DISTINCT_STAGE_KEYS = ("raw", "wiki", "outputs", "archive")

#: The identifier Loop records for its own default layout. A vault created by an
#: earlier version may still carry the old name; `normalise_layout_version`
#: accepts it rather than declaring a working vault unrecognised.
LAYOUT_VERSION = "loop-vault-v2"
LEGACY_LAYOUT_VERSIONS = ("glebos-v2",)


def normalise_layout_version(value: str) -> str:
    """Map a recognised historical layout id onto the current one."""
    return LAYOUT_VERSION if value in LEGACY_LAYOUT_VERSIONS else value


def _validate_relative(key: str, value: str) -> str:
    candidate = str(value).strip().strip("/")
    if not candidate:
        raise InvalidInput(f"vault layout key {key!r} cannot be empty")
    path = PurePosixPath(candidate)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise InvalidInput(
            f"vault layout key {key!r} must stay inside the vault root",
            details={"key": key, "value": value})
    return candidate


@dataclass(frozen=True)
class VaultLayout:
    """A resolved layout. Immutable, so nothing rewrites it mid-run."""

    paths: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LAYOUT))
    version: str = LAYOUT_VERSION

    def __post_init__(self) -> None:
        for key, value in self.paths.items():
            _validate_relative(key, value)
        stages = [self.paths.get(key) for key in DISTINCT_STAGE_KEYS
                  if self.paths.get(key)]
        if len(set(stages)) != len(stages):
            raise InvalidInput(
                "raw, wiki, outputs and archive must be distinct paths: "
                "merging them removes the boundary the compile rules rely on.",
                details={"stages": stages})

    def path(self, key: str) -> str:
        """The relative path for a layout role."""
        value = self.paths.get(key)
        if value is None:
            raise InvalidInput(
                f"unknown vault layout key {key!r}",
                details={"known": sorted(self.paths)})
        return value

    def get(self, key: str, default: str = "") -> str:
        return self.paths.get(key, default)

    def is_within(self, key: str, relative_path: str) -> bool:
        """Whether a path falls inside a layout area."""
        base = self.paths.get(key)
        if not base:
            return False
        candidate = str(relative_path).strip("/")
        return candidate == base or candidate.startswith(base + "/")

    def describe(self) -> dict[str, str]:
        return dict(self.paths)

    @property
    def is_default(self) -> bool:
        return self.paths == DEFAULT_LAYOUT


def load_layout(manifest: dict[str, Any] | None) -> VaultLayout:
    """Build a layout from a vault manifest, filling gaps from the default.

    An override names only what differs. A vault that renames `0-raw` to
    `sources` writes one key and keeps the rest, so an unfamiliar layout is
    still mostly the shipped one — which keeps error messages meaningful.
    """
    if not manifest:
        return VaultLayout()

    declared = manifest.get("layout")
    version = str(manifest.get("layout_version") or LAYOUT_VERSION)

    if not isinstance(declared, dict) or not declared:
        return VaultLayout(version=normalise_layout_version(version))

    merged = dict(DEFAULT_LAYOUT)
    unknown = sorted(set(declared) - set(DEFAULT_LAYOUT))
    if unknown:
        # A key Loop does not use is more likely a typo than an extension, and
        # silently ignoring it would leave the owner's override with no effect.
        raise InvalidInput(
            "The vault manifest declares layout keys Loop does not use.",
            details={"unknown": unknown, "known": sorted(DEFAULT_LAYOUT)})

    for key, value in declared.items():
        merged[key] = _validate_relative(key, str(value))

    return VaultLayout(paths=merged, version=normalise_layout_version(version))
