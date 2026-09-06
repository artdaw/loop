"""Compile contract: sources into sourced wiki knowledge (vault §2, §6).

The nine governing rules are the specification; this module makes the ones a
machine can check actually enforceable, and is explicit about which ones it
cannot.

The rules that shape this code most:

* **Rule 2 — compile only from a source currently read.** Enforced by
  :mod:`loop.vault.receipts` *before* any page is written.
* **Rule 4 — preserve both sides of a contradiction.** Contradictions are never
  resolved by picking the newer or more confident claim. Both are retained,
  confidence becomes ``contested``, and an open question is recorded (V10).
* **Rule 5 — human-owned pages permit only appended notes.** A proposal to
  rewrite a human body is converted into an append, never applied as a replace
  (V11).
* **Rule 8 — extract distinct concepts and connect them.** This is the rule most
  likely to induce bad behaviour: an implementation eager to satisfy "connect
  it" will invent a second page and a plausible link. A genuinely isolated
  one-fact source therefore produces ``needs_context`` and stays *pending* in
  the ledger — filed, not compiled (V09).
* **Rule 9 — reconcile near-duplicates into the stronger page**, keeping
  provenance from both and archiving the weaker with a trail (V22).

Fetching is a separate authority from compiling. Single-source ingest never
fetches; a sweep may, when explicitly authorised (V14, V15). A login wall is not
content (V16), and a transient network failure is not a permanent verdict (V17).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any

import yaml

from loop.core.errors import InvalidInput, ValidationFailed
from loop.vault.receipts import Evidence, ReceiptStore

logger = logging.getLogger(__name__)

#: Frontmatter every content page must carry (vault §2, rule 3).
REQUIRED_PAGE_FIELDS = ("type", "title", "owner", "confidence", "sources")

CONFIDENCE_VALUES = ("stated", "observed", "contested")
OWNERS = ("model", "human")

#: Markers that a fetched page is a wall rather than the article (V16).
LOGIN_WALL_MARKERS = ("sign in to continue", "please enable javascript",
                      "subscribe to read", "create a free account",
                      "403 forbidden", "access denied")


class CompileOutcome(str, Enum):
    COMPILED = "compiled"
    NEEDS_CONTEXT = "needs_context"
    UNFETCHED = "unfetched"
    UNFETCHABLE = "unfetchable"
    REJECTED = "rejected"
    DEFERRED = "deferred"


@dataclass
class Claim:
    """One distinct assertion extracted from a source."""

    text: str
    evidence: Evidence
    subject: str = ""
    attributed_to: str = ""

    @property
    def is_attributed(self) -> bool:
        """Whether this is someone's statement rather than an established fact."""
        return bool(self.attributed_to)


@dataclass
class PageProposal:
    """A proposed create/update of a wiki page."""

    path: str
    title: str
    body: str
    page_type: str = "concept"
    owner: str = "model"
    confidence: str = "stated"
    sources: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    append_only: bool = False

    def to_markdown(self) -> str:
        frontmatter = yaml.safe_dump(
            {"type": self.page_type, "title": self.title, "owner": self.owner,
             "confidence": self.confidence, "sources": list(self.sources)},
            sort_keys=False, allow_unicode=True)
        links = ""
        if self.links:
            links = "\n## Links\n\n" + "\n".join(f"- [[{link}]]"
                                                 for link in self.links) + "\n"
        return f"---\n{frontmatter}---\n\n# {self.title}\n\n{self.body}\n{links}"


@dataclass
class Contradiction:
    """Two claims that cannot both be true."""

    subject: str
    first: Claim
    second: Claim

    def open_question(self) -> str:
        return (f"- **{self.subject}**: "
                f"{self.first.text!r} ({self.first.evidence.source_id}) vs "
                f"{self.second.text!r} ({self.second.evidence.source_id})")


@dataclass
class CompileResult:
    """What one compile run decided. Nothing is written by producing this."""

    source_id: str
    outcome: CompileOutcome
    pages: list[PageProposal] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)
    ledger_status: str = "pending"
    reason: str = ""
    archived: list[str] = field(default_factory=list)

    @property
    def produced_pages(self) -> list[str]:
        return [page.path for page in self.pages]


def validate_page(page: PageProposal) -> None:
    """Check a proposal against the frontmatter rules before it is written."""
    problems: list[str] = []
    if page.owner not in OWNERS:
        problems.append(f"owner must be one of {OWNERS}")
    if page.confidence not in CONFIDENCE_VALUES:
        problems.append(f"confidence must be one of {CONFIDENCE_VALUES}")
    if not page.sources:
        # Rule 3: every content page has non-empty sources.
        problems.append("a content page requires at least one source")
    if not page.title.strip():
        problems.append("a page requires a title")
    if problems:
        raise ValidationFailed(f"Invalid page proposal for {page.path}",
                               details={"problems": problems})


def parse_page(markdown: str) -> dict[str, Any]:
    """Read an existing page's frontmatter and body."""
    if not markdown.startswith("---"):
        return {"frontmatter": {}, "body": markdown}
    parts = markdown.split("---", 2)
    if len(parts) < 3:
        return {"frontmatter": {}, "body": markdown}
    try:
        frontmatter = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        frontmatter = {}
    return {"frontmatter": frontmatter, "body": parts[2]}


def looks_like_login_wall(body: str) -> bool:
    """Whether fetched content is a wall rather than the article (V16)."""
    lowered = body.lower()
    if any(marker in lowered for marker in LOGIN_WALL_MARKERS):
        return True
    # A page with almost no prose is not substantive content either.
    words = re.findall(r"[a-z]{3,}", lowered)
    return len(words) < 20


def is_bare_link(body: str) -> bool:
    """A source whose body is only a URL has no content of its own (V14)."""
    stripped = re.sub(r"^---.*?---", "", body, flags=re.DOTALL).strip()
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    return len(lines) == 1 and lines[0].startswith(("http://", "https://"))


class Compiler:
    """Turns read sources into validated, sourced page proposals."""

    def __init__(self, *, receipts: ReceiptStore,
                 existing_pages: dict[str, str] | None = None) -> None:
        self._receipts = receipts
        #: path -> markdown, for pages already in the wiki.
        self._existing = existing_pages or {}

    # ------------------------------------------------------------------ #
    # Rule 2 / rule 3 — evidence
    # ------------------------------------------------------------------ #
    def compile_claims(self, source_id: str, claims: list[Claim], *,
                       require_complete_read: bool = True) -> None:
        """Validate that every claim is backed before anything is proposed."""
        self._receipts.validate([claim.evidence for claim in claims],
                                require_complete=require_complete_read)

    # ------------------------------------------------------------------ #
    # Rule 8 — distinct concepts, honestly
    # ------------------------------------------------------------------ #
    def assess_isolation(self, claims: list[Claim], *,
                         linkable: list[str]) -> CompileResult | None:
        """Return a ``needs_context`` result for a genuinely isolated source.

        Rule 8 asks for distinct concepts *connected* to existing knowledge. When
        a tiny source supports exactly one claim and nothing real to link to,
        the honest outcome is to keep the capture and say so. Inventing a second
        page to satisfy the shape of the rule would put a fabricated concept in
        the user's permanent knowledge base (V09).
        """
        if len(claims) <= 1 and not linkable:
            return CompileResult(
                source_id=claims[0].evidence.source_id if claims else "",
                outcome=CompileOutcome.NEEDS_CONTEXT,
                ledger_status="pending",
                reason=("Single isolated claim with no existing page to connect "
                        "to. Captured and awaiting context; no concept invented."))
        return None

    # ------------------------------------------------------------------ #
    # Rule 4 — contradictions
    # ------------------------------------------------------------------ #
    @staticmethod
    def find_contradictions(claims: list[Claim]) -> list[Contradiction]:
        """Pair claims about the same subject that disagree."""
        by_subject: dict[str, list[Claim]] = {}
        for claim in claims:
            if claim.subject:
                by_subject.setdefault(claim.subject, []).append(claim)

        contradictions: list[Contradiction] = []
        for subject, group in by_subject.items():
            for index, first in enumerate(group):
                for second in group[index + 1:]:
                    if first.text.strip() != second.text.strip():
                        contradictions.append(
                            Contradiction(subject=subject, first=first,
                                          second=second))
        return contradictions

    def apply_contradictions(self, page: PageProposal,
                             contradictions: list[Contradiction]) -> PageProposal:
        """Retain both sides and mark the page contested (rule 4).

        Never drops a side. The page carries both statements with their
        attributions, and confidence becomes ``contested`` so the disagreement
        is visible rather than averaged away.
        """
        if not contradictions:
            return page

        lines = [page.body, "", "## Contested"]
        for contradiction in contradictions:
            lines.append(f"- {contradiction.first.text} "
                         f"(source: {contradiction.first.evidence.source_id})")
            lines.append(f"- {contradiction.second.text} "
                         f"(source: {contradiction.second.evidence.source_id})")
        page.body = "\n".join(lines)
        page.confidence = "contested"
        return page

    @staticmethod
    def open_questions_entry(contradictions: list[Contradiction]) -> str:
        return "\n".join(c.open_question() for c in contradictions) + "\n"

    # ------------------------------------------------------------------ #
    # Rule 5 — human-owned pages
    # ------------------------------------------------------------------ #
    def respect_ownership(self, page: PageProposal) -> PageProposal:
        """Convert a rewrite of a human-owned page into an append (rule 5).

        The user's own words are not raw material for a model rewrite. A
        proposal to replace a human body becomes appended Compiler notes.
        """
        existing = self._existing.get(page.path)
        if existing is None:
            return page

        parsed = parse_page(existing)
        if parsed["frontmatter"].get("owner") != "human":
            return page

        page.append_only = True
        page.owner = "human"
        page.body = ("\n## Compiler notes\n\n" + page.body.strip() + "\n")
        return page

    def is_human_owned(self, path: str) -> bool:
        existing = self._existing.get(path)
        if existing is None:
            return False
        return parse_page(existing)["frontmatter"].get("owner") == "human"

    # ------------------------------------------------------------------ #
    # Rule 9 — near-duplicate reconciliation
    # ------------------------------------------------------------------ #
    def reconcile_duplicates(self, stronger: PageProposal,
                             weaker_path: str) -> CompileResult:
        """Merge into the stronger page, keeping both provenance trails (V22)."""
        weaker = self._existing.get(weaker_path)
        if weaker is None:
            raise InvalidInput(f"No page at {weaker_path} to reconcile")

        parsed = parse_page(weaker)
        weaker_sources = parsed["frontmatter"].get("sources") or []
        if parsed["frontmatter"].get("owner") == "human":
            # Rule 5 still applies during a merge: a human body is never
            # absorbed into a model-owned page by rewriting it.
            raise ValidationFailed(
                f"{weaker_path} is human-owned and cannot be merged away.",
                details={"path": weaker_path})

        merged_sources = list(dict.fromkeys(
            [*stronger.sources, *(str(s) for s in weaker_sources)]))
        stronger.sources = merged_sources
        return CompileResult(
            source_id=stronger.path, outcome=CompileOutcome.COMPILED,
            pages=[stronger], ledger_status="superseded",
            archived=[weaker_path],
            reason=f"Merged {weaker_path} into {stronger.path}; both trails kept.")

    # ------------------------------------------------------------------ #
    # Fetching authority (V14–V17)
    # ------------------------------------------------------------------ #
    @staticmethod
    def classify_fetch(body: str, *, allow_fetch: bool,
                       fetch_error: str | None = None,
                       transient: bool = False) -> CompileResult:
        """Decide a bare-link source's outcome without inventing content."""
        if not is_bare_link(body):
            return CompileResult(source_id="", outcome=CompileOutcome.COMPILED)

        if not allow_fetch:
            # Single-source ingest must not fetch (V14).
            return CompileResult(
                source_id="", outcome=CompileOutcome.UNFETCHED,
                ledger_status="unfetched",
                reason=("Bare link and fetching is not authorised for this "
                        "operation. No page content was invented."))

        if fetch_error and transient:
            # A network blip is not a permanent verdict (V17).
            return CompileResult(
                source_id="", outcome=CompileOutcome.DEFERRED,
                ledger_status="pending",
                reason=f"Transient fetch failure ({fetch_error}); will retry.")

        if fetch_error:
            return CompileResult(
                source_id="", outcome=CompileOutcome.UNFETCHABLE,
                ledger_status="unfetchable",
                reason=f"Permanent fetch failure: {fetch_error}")

        return CompileResult(source_id="", outcome=CompileOutcome.COMPILED)

    @staticmethod
    def classify_fetched_body(body: str, *, fetched_on: date) -> CompileResult:
        """Reject a login wall or JS shell as non-content (V16)."""
        if looks_like_login_wall(body):
            return CompileResult(
                source_id="", outcome=CompileOutcome.UNFETCHABLE,
                ledger_status="unfetchable",
                reason=(f"Retrieved page is a login/paywall or script shell, "
                        f"not substantive content ({fetched_on.isoformat()})."))
        return CompileResult(source_id="", outcome=CompileOutcome.COMPILED)

    @staticmethod
    def fetched_source_path(original_path: str, *, fetched_on: date) -> str:
        """Name for the NEW adjacent source a fetch creates (V15).

        A fetch never edits the bookmark: raw sources are immutable (rule 1).
        The retrieved content becomes its own source and the bookmark row is
        superseded.
        """
        stem = original_path.rsplit(".md", 1)[0]
        return f"{stem}--fetched-{fetched_on.isoformat()}.md"

    # ------------------------------------------------------------------ #
    # Rule 4 metadata repair (V32)
    # ------------------------------------------------------------------ #
    @staticmethod
    def align_confidence(markdown: str) -> tuple[str, bool]:
        """Match metadata to preserved prose, never the other way round (V32).

        When a page's prose describes a contested claim but its frontmatter says
        ``stated``, the *metadata* is corrected. Rewriting the prose to match the
        metadata would edit the knowledge to fit its label.
        """
        parsed = parse_page(markdown)
        frontmatter = parsed["frontmatter"]
        body = parsed["body"]

        prose_contested = ("## Contested" in body
                           or "contradict" in body.lower()
                           or "disagree" in body.lower())
        if prose_contested and frontmatter.get("confidence") != "contested":
            frontmatter["confidence"] = "contested"
            rendered = yaml.safe_dump(frontmatter, sort_keys=False,
                                      allow_unicode=True)
            return f"---\n{rendered}---{body}", True
        return markdown, False
