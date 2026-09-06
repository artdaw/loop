"""Read-only vault onboarding (vault §1, §3, §4).

Onboarding *inspects*. It maps the layout, reads the governing rules, and reports
conflicts — and in dry-run mode, which is the default, it writes nothing at all
(V01). That default matters: pointing a new tool at years of accumulated notes
and having it reorganise them is the failure everyone fears, so the tool has to
earn the write with an explicit `--apply`.

Two structural rules it must not break:

* **No new PARA roots.** The observed vault is v2 (`0-raw` → `1-wiki` →
  `3-output`). Creating `Projects/`, `Areas/`, `Resources/` or `Archive/` inside
  it would resurrect the superseded v1 layout alongside the current one.
* **Never overwrite an existing document.** Proposed `_ctx/loop/` files are shown
  first; an apply skips anything already present rather than replacing it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from loop.vault.gateway import VaultGateway, WriteMode
from loop.vault.layout import (
    DEFAULT_LAYOUT as _DEFAULT_LAYOUT,
)
from loop.vault.layout import (
    LAYOUT_VERSION,
    VaultLayout,
    normalise_layout_version,
)

logger = logging.getLogger(__name__)

#: v1 folder names that must never be created inside a v2 vault (vault §1).
FORBIDDEN_ROOTS = ("Projects", "Areas", "Resources", "Archive", "System", "Inbox")

#: The layout an installer records. Loop's shipped default lives in
#: `loop.vault.layout`; a vault may override any of it in its own manifest.
DEFAULT_LAYOUT = dict(_DEFAULT_LAYOUT)

#: Files onboarding may propose. Never written without an explicit apply.
PROPOSED_FILES = (
    "_ctx/loop/manifest.yaml",
    "_ctx/loop/behavior.md",
    "_ctx/loop/permissions.md",
    "_ctx/loop/notifications.md",
)


@dataclass
class VaultConflict:
    """A disagreement between two authoritative documents."""

    kind: str
    detail: str
    paths: list[str] = field(default_factory=list)


@dataclass
class OnboardingReport:
    """What onboarding found, and what it would do."""

    root: str
    layout_version: str
    layout: dict[str, str] = field(default_factory=dict)
    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    conflicts: list[VaultConflict] = field(default_factory=list)
    proposed_files: list[str] = field(default_factory=list)
    written_files: list[str] = field(default_factory=list)
    dry_run: bool = True

    @property
    def is_v2(self) -> bool:
        return normalise_layout_version(self.layout_version) == LAYOUT_VERSION

    def summary(self) -> str:
        mode = "dry run" if self.dry_run else "applied"
        return (f"Vault {self.root} ({self.layout_version}, {mode}): "
                f"{len(self.present)} mapped paths, {len(self.missing)} missing, "
                f"{len(self.conflicts)} conflict(s), "
                f"{len(self.proposed_files)} proposed file(s).")


class VaultOnboarding:
    """Inspects a vault and proposes Loop's own context files."""

    def __init__(self, *, gateway: VaultGateway,
                 layout: VaultLayout | None = None) -> None:
        self._gateway = gateway
        self._layout = layout or VaultLayout()

    # ------------------------------------------------------------------ #
    # Inspection
    # ------------------------------------------------------------------ #
    def detect_layout(self) -> str:
        """Identify the vault layout from its own entry point."""
        if not self._gateway.exists("CLAUDE.md"):
            return "unknown"
        text = self._gateway.read_text("CLAUDE.md")
        if "v2" in text and self._layout.path("raw") in text:
            return LAYOUT_VERSION
        return "unknown"

    def inspect(self) -> OnboardingReport:
        """Read the vault and report. Writes nothing."""
        report = OnboardingReport(root=str(self._gateway.root),
                                  layout_version=self.detect_layout(),
                                  layout=self._layout.describe())

        for key, relative in self._layout.describe().items():
            if self._gateway.exists(relative):
                report.present.append(key)
            else:
                report.missing.append(key)

        report.counts = self._count(report)
        report.conflicts = self.find_conflicts()
        report.proposed_files = [p for p in PROPOSED_FILES
                                 if not self._gateway.exists(p)]
        return report

    def _count(self, report: OnboardingReport) -> dict[str, int]:
        def count_markdown(relative: str) -> int:
            path = self._gateway.resolve(relative)
            if not path.is_dir():
                return 0
            return len([p for p in path.rglob("*.md") if p.is_file()])

        return {
            "concepts": count_markdown("1-wiki/concepts"),
            "entities": count_markdown("1-wiki/entities"),
            "topics": count_markdown("1-wiki/topics"),
            "raw_sources": count_markdown("0-raw"),
            "projects": (
                len(list(self._gateway.resolve("2-projects").glob("*/CLAUDE.md")))
                if self._gateway.resolve("2-projects").is_dir() else 0),
        }

    # ------------------------------------------------------------------ #
    # Conflicts (V26, V28)
    # ------------------------------------------------------------------ #
    def find_conflicts(self) -> list[VaultConflict]:
        """Report disagreements rather than resolving them silently."""
        conflicts: list[VaultConflict] = []

        templates = self._gateway.resolve("_ctx/templates")
        if templates.is_dir():
            for template in sorted(templates.glob("*.md")):
                body = template.read_text(encoding="utf-8")
                if "type: zettel" in body or "tags:" in body and "owner:" not in body:
                    conflicts.append(VaultConflict(
                        kind="legacy_template",
                        detail=("Template uses v1 conventions and omits v2 "
                                "provenance fields. The v2 schema governs new "
                                "writes; the template body may still inspire "
                                "presentation."),
                        paths=[f"_ctx/templates/{template.name}"]))

        # A prepared routine document is a proposal, not an activation (V28).
        routines = self._gateway.resolve("_ctx/loop/routines")
        if routines.is_dir():
            for routine in sorted(routines.glob("*.md")):
                relative = f"_ctx/loop/routines/{routine.name}"
                if self._routine_claims_enabled(routine):
                    conflicts.append(VaultConflict(
                        kind="unregistered_routine",
                        detail=("Routine document sets enabled: true but has no "
                                "activation event. Its presence is not "
                                "permission; it stays inactive until activated."),
                        paths=[relative]))
        return conflicts

    @staticmethod
    def _routine_claims_enabled(path: Path) -> bool:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return False
        try:
            frontmatter = yaml.safe_load(text.split("---")[1]) or {}
        except yaml.YAMLError:
            return False
        return bool(frontmatter.get("enabled"))

    # ------------------------------------------------------------------ #
    # Apply
    # ------------------------------------------------------------------ #
    def apply(self, *, dry_run: bool = True) -> OnboardingReport:
        """Create the proposed `_ctx/loop/` files.

        ``dry_run`` defaults to True (interfaces §3: absence of an apply flag
        means dry run). Existing documents are never overwritten.
        """
        report = self.inspect()
        report.dry_run = dry_run
        if dry_run or not report.proposed_files:
            return report

        operations = []
        for relative in report.proposed_files:
            if self._gateway.exists(relative):
                continue          # never replace an existing document
            body = self._proposed_body(relative)
            operations.append(self._gateway.make_operation(
                relative, body, mode=WriteMode.CREATE_NEW))

        if operations:
            self._gateway.apply(self._gateway.begin(operations))
            report.written_files = [op.relative_path for op in operations]
        return report

    def _proposed_body(self, relative: str) -> str:
        if relative.endswith("manifest.yaml"):
            return yaml.safe_dump(
                {"schema_version": 1, "layout": LAYOUT_VERSION,
                 "paths": dict(DEFAULT_LAYOUT)},
                sort_keys=False, allow_unicode=True)
        name = Path(relative).stem
        return (f"# {name.title()}\n\n"
                "Proposed by Loop onboarding. Edit to change behaviour.\n")

    # ------------------------------------------------------------------ #
    # Structural guard
    # ------------------------------------------------------------------ #
    def forbidden_roots_present(self) -> list[str]:
        """v1 folders that must not exist inside a v2 vault."""
        return [name for name in FORBIDDEN_ROOTS
                if self._gateway.resolve(name).is_dir()]
