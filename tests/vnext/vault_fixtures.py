"""Synthetic GlebOS fixtures (acceptance §1).

The real vault at ``~/Claude_Cowork/GlebOS`` is **read-only during development**
and is never touched by tests. Everything here builds a temporary vault with the
same shape, including the awkward cases that a tidy fixture would hide:

* a filename in NFD form alongside NFC comparisons (V19, V20),
* an entity filename carrying an emoji, which migration must preserve (V19),
* a source body containing ``|`` characters that must not corrupt ledger rows (V02),
* a v1-era template that conflicts with the v2 schema (V26),
* a *prepared but unregistered* routine, which must not self-activate (V28),
* a human-owned wiki page whose body may only be appended to (V11).

A fixture that only contains clean data proves the easy half of the contract.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

#: The six-column ledger header, exactly as the vault contract specifies.
LEDGER_HEADER = ("| source | batch | added | status | compiled | pages produced |\n"
                 "|---|---|---|---|---|---|\n")

ROOT_CLAUDE_MD = """# GlebOS

Layout v2: source → wiki → output.

- Rules: `_ctx/rules/compile.md`, `_ctx/rules/naming.md`, `_ctx/rules/frontmatter.md`
- Roles: `_ctx/agents/`
- Personal context: `_mem/profile.md`
"""

COMPILE_RULES = """# Compile rules

1. Raw sources are immutable. Change processing state only in their ledger rows.
2. Compile only from a source currently read.
3. Every compiled claim has provenance; every content page has non-empty `sources`.
4. Preserve both sides of contradictions and record them in open-questions.
5. Human-owned wiki pages permit only appended Compiler notes.
6. Deliverables in `3-output/` derive from compiled wiki pages, not raw.
7. Retire content to `_archive/`, never silently delete it.
8. Extract distinct concepts and connect them with existing knowledge.
9. Reconcile near-duplicates into the stronger page with provenance from both.
"""

NAMING_RULES = """# Naming

New entity and person filenames use the plain proper-name pattern.
Existing migrated filenames with flags or emoji are intentional; preserve them.
Normalize paths to Unicode NFC for identity comparisons.
"""

FRONTMATTER_RULES = """# Frontmatter

Content pages require: type, title, owner, confidence, sources.
`owner` is `model` or `human`. `confidence` is one of stated/observed/contested.
"""

#: A v1 template retained in the vault. Its conventions conflict with v2 (V26).
LEGACY_TEMPLATE = """---
type: zettel
tags: [fleeting]
---

# {{title}}

v1 template: no provenance block, no owner or confidence fields.
"""

#: Prepared but NOT registered. Its presence is not activation (V28).
PREPARED_ROUTINE = """---
schema_version: 1
id: daily-compile
title: Daily compile sweep
enabled: true
trigger:
  kind: local_schedule
  days: [mon, tue, wed, thu, fri]
  at: "18:00"
  timezone: Europe/Berlin
---

Prepared for review. Not registered with the scheduler.
"""


@dataclass
class SyntheticVault:
    """A temporary vault plus the facts a test needs to assert against."""

    root: Path
    ledger_path: Path
    sources: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)

    def read(self, relative: str) -> str:
        return (self.root / relative).read_text(encoding="utf-8")

    def ledger_rows(self) -> list[list[str]]:
        """Parse the ledger into cells, excluding the header and separator."""
        lines = [line for line in self.ledger_path.read_text(encoding="utf-8")
                 .splitlines() if line.strip().startswith("|")]
        rows = []
        for line in lines[2:]:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(cells)
        return rows


def build_minimal_vault(root: Path) -> SyntheticVault:
    """Create the minimal-correctness fixture from acceptance §1."""
    root.mkdir(parents=True, exist_ok=True)

    for folder in ("0-raw/inbox", "0-raw/clips", "0-raw/notes",
                   "1-wiki/concepts", "1-wiki/entities", "1-wiki/topics",
                   "2-projects", "3-output/articles", "4-journal/daily/2026",
                   "4-journal/meetings/2026", "_ctx/rules", "_ctx/agents",
                   "_ctx/templates", "_mem/people", "_mem/state", "_archive"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    (root / "CLAUDE.md").write_text(ROOT_CLAUDE_MD, encoding="utf-8")
    (root / "_ctx/rules/compile.md").write_text(COMPILE_RULES, encoding="utf-8")
    (root / "_ctx/rules/naming.md").write_text(NAMING_RULES, encoding="utf-8")
    (root / "_ctx/rules/frontmatter.md").write_text(FRONTMATTER_RULES,
                                                    encoding="utf-8")
    for role in ("scribe", "compiler", "seeker", "edward"):
        (root / f"_ctx/agents/{role}.md").write_text(
            f"# {role.title()}\n\nRole definition.\n", encoding="utf-8")

    # A v1 template that conflicts with the v2 schema (V26).
    (root / "_ctx/templates/Zettel.md").write_text(LEGACY_TEMPLATE,
                                                   encoding="utf-8")
    # Prepared, deliberately unregistered (V28).
    (root / "_ctx/loop/routines").mkdir(parents=True, exist_ok=True)
    (root / "_ctx/loop/routines/daily-compile.md").write_text(
        PREPARED_ROUTINE, encoding="utf-8")

    (root / "_mem/profile.md").write_text(
        "# Profile\n\nInvented test persona. No real personal data.\n",
        encoding="utf-8")
    (root / "_mem/goals.md").write_text(
        "# Goals\n\n| Goal | Current |\n|---|---|\n| Ship Loop | in progress |\n",
        encoding="utf-8")
    (root / "_mem/people/Sam Rivers.md").write_text(
        "# Sam Rivers\n\nRelationship state for an invented person.\n",
        encoding="utf-8")

    # --- Raw sources -------------------------------------------------------
    # A body containing pipes: these must survive into storage but must never
    # break the ledger's own pipe-delimited rows (V02).
    exact_note = (
        "---\n"
        "type: source\n"
        "source-kind: note\n"
        "title: Supplier lead time\n"
        "added: 2026-09-05\n"
        "origin: gleb\n"
        "---\n\n"
        "The supplier said production takes six weeks | confirmed by email   \n"
    )
    (root / "0-raw/inbox/2026-09-05-supplier-lead-time.md").write_text(
        exact_note, encoding="utf-8")

    bare_link = (
        "---\ntype: source\nsource-kind: clip\ntitle: Acoustic panels\n"
        "added: 2026-09-04\norigin: https://example.invalid/acoustics\n---\n\n"
        "https://example.invalid/acoustics\n"
    )
    (root / "0-raw/clips/2026-09-04-acoustic-panels.md").write_text(
        bare_link, encoding="utf-8")

    # --- Ledger ------------------------------------------------------------
    ledger = root / "0-raw/_ledger.md"
    ledger.write_text(
        LEDGER_HEADER
        + "| 0-raw/inbox/2026-09-05-supplier-lead-time.md | inbox | 2026-09-05 "
          "| pending | — | — |\n"
        + "| 0-raw/clips/2026-09-04-acoustic-panels.md | clips | 2026-09-04 "
          "| unfetched | — | — |\n",
        encoding="utf-8")

    # --- Wiki --------------------------------------------------------------
    (root / "1-wiki/concepts/lead-time.md").write_text(
        "---\ntype: concept\ntitle: Lead time\nowner: model\n"
        "confidence: stated\nsources:\n"
        "  - 0-raw/inbox/2026-09-05-supplier-lead-time.md\n---\n\n"
        "Time between order and delivery.\n", encoding="utf-8")

    # Human-owned: body may only be appended to (V11).
    (root / "1-wiki/entities/Nordic Panels 🇸🇪.md").write_text(
        "---\ntype: entity\ntitle: Nordic Panels\nowner: human\n"
        "confidence: stated\nsources:\n  - manual\n---\n\n"
        "Hand-written notes about the supplier. Do not rewrite this body.\n",
        encoding="utf-8")

    (root / "1-wiki/topics/acoustics.md").write_text(
        "---\ntype: topic\ntitle: Acoustics\nowner: model\n"
        "confidence: stated\nsources:\n  - 0-raw/clips/2026-09-04-acoustic-panels.md\n"
        "---\n\nHub for acoustic performance.\n", encoding="utf-8")

    (root / "1-wiki/index.md").write_text("# Wiki index\n\n3 pages.\n",
                                          encoding="utf-8")
    (root / "1-wiki/open-questions.md").write_text(
        "# Open questions\n\nNone recorded.\n", encoding="utf-8")

    (root / "2-projects/loop").mkdir(parents=True, exist_ok=True)
    (root / "2-projects/loop/CLAUDE.md").write_text(
        "---\ntype: project\ntitle: Loop\nstatus: active\n"
        "goal: Ship the assistant\nupdated: 2026-09-05\n---\n\n"
        "Project-scoped instructions.\n", encoding="utf-8")

    (root / "_archive/2025-old-source.md").write_text(
        "---\ntype: source\ntitle: Retired source\n---\n\nArchived.\n",
        encoding="utf-8")

    return SyntheticVault(
        root=root, ledger_path=ledger,
        sources=["0-raw/inbox/2026-09-05-supplier-lead-time.md",
                 "0-raw/clips/2026-09-04-acoustic-panels.md"],
        concepts=["1-wiki/concepts/lead-time.md"],
    )


def add_nfd_filename(vault: SyntheticVault) -> tuple[Path, str]:
    """Add a file whose name is NFD-encoded (V19).

    macOS stores names decomposed; comparisons must normalise to NFC without
    renaming what is on disk.
    """
    nfc_name = "Café Acoustics.md"
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    path = vault.root / "1-wiki/entities" / nfd_name
    path.write_text(
        "---\ntype: entity\ntitle: Café Acoustics\nowner: human\n"
        "confidence: stated\nsources:\n  - manual\n---\n\nNotes.\n",
        encoding="utf-8")
    return path, nfc_name


def build_perf_vault(root: Path, *, sources: int = 500,
                     pages: int = 200) -> SyntheticVault:
    """A larger fixture for indexing and queue behaviour (acceptance §1)."""
    vault = build_minimal_vault(root)
    rows = [LEDGER_HEADER]
    rows.append("| 0-raw/inbox/2026-09-05-supplier-lead-time.md | inbox "
                "| 2026-09-05 | pending | — | — |\n")

    for index in range(sources):
        relative = f"0-raw/notes/2026-08-{(index % 28) + 1:02d}-note-{index}.md"
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(
            f"---\ntype: source\nsource-kind: note\ntitle: Note {index}\n"
            f"added: 2026-08-01\norigin: gleb\n---\n\n"
            f"Synthetic body {index} with some searchable words.\n",
            encoding="utf-8")
        rows.append(f"| {relative} | notes | 2026-08-01 | compiled | 2026-08-02 "
                    f"| 1-wiki/concepts/page-{index % pages}.md |\n")
        vault.sources.append(relative)

    for index in range(pages):
        relative = f"1-wiki/concepts/page-{index}.md"
        (root / relative).write_text(
            f"---\ntype: concept\ntitle: Page {index}\nowner: model\n"
            f"confidence: stated\nsources:\n  - 0-raw/notes/2026-08-01-note-{index}.md\n"
            f"---\n\nBody for page {index}.\n", encoding="utf-8")
        vault.concepts.append(relative)

    vault.ledger_path.write_text("".join(rows), encoding="utf-8")
    return vault
