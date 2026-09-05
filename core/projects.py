"""Project context — which piece of work a task, email, or note belongs to.

Loop already commits to **PARA** in the knowledge specialist, so the vault's
``Projects/`` folder is the natural registry: one note or subdirectory per
project. A ``projects`` setting supplements it (and covers installs with no
vault) using ``"Name:keyword1|keyword2,Other:kw"`` syntax.

Matching is **deterministic and LLM-free** by design. It runs on every inbound
email, task, and note, so it must be cheap enough to run unconditionally and
testable without a model. Scoring combines:

    * whole-word keyword hits, weighted by keyword length (a hit on
      "atlas migration" outranks a hit on "api"),
    * fuzzy similarity between the project name and the text,
    * a bonus for an explicit ``#slug`` mention.

An LLM tiebreak is deliberately deferred; revisit only if accuracy proves
insufficient in real use.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: Folder inside the vault that holds one entry per project (PARA).
PROJECTS_FOLDER = "Projects"

#: Minimum score for a match to be returned.
DEFAULT_THRESHOLD = 0.35

#: Line inside a project note that lists extra keywords.
_KEYWORDS_RE = re.compile(r"^\s*keywords\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

#: Words too generic to be useful as automatic keywords.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at",
    "project", "work", "new", "old",
})


def slugify(name: str) -> str:
    """Return a URL/id-safe slug for a project name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    return slug.strip("-")


@dataclass(frozen=True)
class Project:
    """One project Loop knows about."""

    slug: str
    name: str
    keywords: frozenset[str]
    source: str  # "vault" | "settings"


@dataclass(frozen=True)
class ProjectMatch:
    """A project match plus the score that produced it."""

    project: Project
    score: float


class ProjectRegistry:
    """Discovers the set of known projects, with a simple cache."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._cache: list[Project] | None = None

    def refresh(self) -> None:
        """Drop the cache so the next :meth:`discover` re-reads the vault."""
        self._cache = None

    def discover(self) -> list[Project]:
        """Return every known project, from the vault and the settings."""
        if self._cache is not None:
            return self._cache

        by_slug: dict[str, Project] = {}
        for project in self._from_vault():
            by_slug[project.slug] = project
        for project in self._from_settings():
            existing = by_slug.get(project.slug)
            if existing is None:
                by_slug[project.slug] = project
            else:
                # Merge settings keywords into the vault-discovered project.
                by_slug[project.slug] = Project(
                    slug=existing.slug,
                    name=existing.name,
                    keywords=existing.keywords | project.keywords,
                    source=existing.source,
                )

        self._cache = list(by_slug.values())
        return self._cache

    # ------------------------------------------------------------------ #
    # Sources
    # ------------------------------------------------------------------ #
    def _from_vault(self) -> list[Project]:
        """Read one project per entry in ``<vault>/Projects/``.

        A missing vault or missing folder yields an empty list — Loop must work
        for users who keep no Obsidian vault at all.
        """
        raw_path = (self.settings.obsidian_vault_path or "").strip()
        if not raw_path:
            return []
        projects_dir = Path(raw_path).expanduser() / PROJECTS_FOLDER
        if not projects_dir.is_dir():
            return []

        projects: list[Project] = []
        try:
            entries = sorted(projects_dir.iterdir())
        except OSError:
            logger.warning("Could not read the projects folder at %s", projects_dir)
            return []

        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                name = entry.name
                extra: set[str] = set()
            elif entry.suffix.lower() == ".md":
                name = entry.stem
                extra = self._keywords_from_note(entry)
            else:
                continue
            projects.append(self._build(name, extra, source="vault"))
        return projects

    def _from_settings(self) -> list[Project]:
        """Parse the ``projects`` CSV setting."""
        raw = (self.settings.projects or "").strip()
        if not raw:
            return []

        projects: list[Project] = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            name, _, keyword_blob = entry.partition(":")
            name = name.strip()
            if not name:
                continue
            extra = {
                kw.strip().lower()
                for kw in keyword_blob.split("|")
                if kw.strip()
            }
            projects.append(self._build(name, extra, source="settings"))
        return projects

    @staticmethod
    def _keywords_from_note(path: Path) -> set[str]:
        """Read a ``keywords:`` line from a project note, if present."""
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return set()
        match = _KEYWORDS_RE.search(text)
        if not match:
            return set()
        return {kw.strip().lower() for kw in match.group(1).split(",") if kw.strip()}

    @staticmethod
    def _build(name: str, extra_keywords: set[str], *, source: str) -> Project:
        """Build a project, seeding keywords from its own name and slug."""
        slug = slugify(name)
        keywords = {kw for kw in extra_keywords if kw}
        keywords.add(name.lower())
        keywords.add(slug)
        # Individual words of the name, minus generic ones.
        for word in re.split(r"[^a-z0-9]+", name.lower()):
            if len(word) > 2 and word not in _STOPWORDS:
                keywords.add(word)
        return Project(slug=slug, name=name, keywords=frozenset(keywords), source=source)


class ProjectMatcher:
    """Scores text against the known projects. Deterministic, no LLM."""

    def __init__(self, registry: ProjectRegistry | None = None,
                 settings: Settings | None = None) -> None:
        self.registry = registry or ProjectRegistry(settings)

    def match(self, text: str, *,
              threshold: float = DEFAULT_THRESHOLD) -> ProjectMatch | None:
        """Return the best-scoring project for ``text``, or ``None``."""
        cleaned = (text or "").strip()
        if not cleaned:
            return None
        projects = self.registry.discover()
        if not projects:
            return None

        lowered = cleaned.lower()
        best: ProjectMatch | None = None
        for project in projects:
            score = self._score(project, lowered)
            if score >= threshold and (best is None or score > best.score):
                best = ProjectMatch(project=project, score=score)
        return best

    def match_slug(self, text: str, *,
                   threshold: float = DEFAULT_THRESHOLD) -> str | None:
        """Convenience wrapper returning just the slug (or ``None``)."""
        match = self.match(text, threshold=threshold)
        return match.project.slug if match else None

    # ------------------------------------------------------------------ #
    # Scoring
    # ------------------------------------------------------------------ #
    @staticmethod
    def _score(project: Project, lowered_text: str) -> float:
        """Score one project against already-lowercased text."""
        # An explicit "#slug" mention is as close to intent as we get.
        if f"#{project.slug}" in lowered_text:
            return 1.0

        # Whole-word keyword hits, weighted by keyword length so a hit on a
        # specific multi-word phrase beats a hit on a short generic token.
        hit_weight = 0.0
        for keyword in project.keywords:
            if not keyword:
                continue
            pattern = r"(?<!\w)" + re.escape(keyword) + r"(?!\w)"
            if re.search(pattern, lowered_text):
                hit_weight = max(hit_weight, len(keyword))

        keyword_score = 0.0
        if hit_weight:
            # Saturating: a 10-character keyword hit is already convincing.
            keyword_score = min(1.0, 0.45 + hit_weight / 20.0)

        # Fuzzy similarity catches near-misses in short texts.
        similarity = difflib.SequenceMatcher(None, project.name.lower(),
                                             lowered_text).ratio()

        return max(keyword_score, similarity * 0.6)
