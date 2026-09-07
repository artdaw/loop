"""Capture → raw + ledger → sourced wiki → cited answer, connected.

M5's other half. Every piece existed as a tested component and none of them
were reachable from an application: `CaptureService` wrote an exact raw file
and registered it, `Compiler` held the nine compile rules, `ReceiptStore` could
prove a source was read, `VaultSearch` could retrieve, and `VaultGateway` could
write journalled, crash-recoverable operations. What was missing was the thing
that runs them in order and refuses to let the chain break — the "knowledge
extraction/capture-to-wiki reasoning is not fully connected" gap.

Three operations, in the order a piece of knowledge actually moves:

* `capture` — preserve the owner's exact words and register them. Wording is
  earned, not assumed: "saved to your vault" requires both the file and the
  ledger row (vault §5), and anything less says so.
* `compile_source` — read the whole source, extract claims, and write a wiki
  page whose every claim points at bytes that were actually read.
* `answer` — retrieve wiki first, cite what was read, and label a raw
  fallback as uncompiled rather than presenting it as settled knowledge.

**The evidence check is the load-bearing part of compile.** A model proposing
a claim also proposes the quote that supports it, and this module does *not*
take the quote's location on trust: it locates the quote in the source's actual
bytes and builds the byte range from what it found. A claim whose quote is not
in the source is dropped, named, and never written. That is the difference
between provenance and a citation-shaped string — a model that paraphrases its
own evidence produces a page that looks sourced and is not, and no amount of
schema validation downstream would catch it.

**No model configured is not a compile failure.** Claim extraction genuinely
needs one. Without it the capture stays preserved, the ledger row stays
`pending`, and the operation says so — rather than writing an empty page so
that something happened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from loop.ai.budget import RootBudget
from loop.ai.model_gateway import ModelGateway, gateway_chat_model
from loop.ai.structured import invoke_structured
from loop.core.clock import Clock, SystemClock
from loop.core.errors import InvalidInput, LoopError
from loop.core.ids import content_hash, new_id
from loop.core.privacy import PrivacyLabel
from loop.vault.capture import CaptureRequest, CaptureResult, CaptureService
from loop.vault.compiler import (
    Claim,
    CompileOutcome,
    Compiler,
    PageProposal,
    parse_page,
    validate_page,
)
from loop.vault.gateway import VaultGateway, WriteMode
from loop.vault.ledger import Ledger, render
from loop.vault.receipts import ByteRange, Evidence, ReceiptStore
from loop.vault.search import IndexEntry, VaultSearch, layer_for

logger = logging.getLogger(__name__)

#: Vault layers worth retrieving from. `_ctx` rules, `_mem` personal state
#: and `_archive` are deliberately excluded: they are policy, relationship
#: state and retired material, not answers to questions.
INDEXED_LAYERS = ("raw", "wiki", "journal", "output")

#: What the extraction model must return. `quote` is required on every claim
#: precisely because it is the thing that gets verified against the bytes.
CLAIMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "page_title": {"type": "string"},
        "page_type": {"type": "string", "enum": ["concept", "entity", "topic"]},
        "summary": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "quote": {"type": "string"},
                    "subject": {"type": "string"},
                    "attributed_to": {"type": "string"},
                },
                "required": ["text", "quote"],
            },
        },
    },
    "required": ["page_title", "claims"],
}

EXTRACTION_SYSTEM = (
    "You extract distinct factual claims from one source document for a "
    "personal knowledge base. Every claim must include `quote`: a span copied "
    "**verbatim** from the document, character for character, that supports "
    "it. A quote that is not present in the document word for word will be "
    "rejected and its claim discarded. Do not infer, summarise across claims, "
    "or add knowledge the document does not contain. If the document states "
    "something as somebody's assertion rather than as fact, put that person "
    "or organisation in `attributed_to`. Return only the JSON object."
)


@dataclass
class CompileReport:
    """What one compile run did, precisely enough to explain to the owner."""

    source_path: str
    outcome: CompileOutcome
    pages: list[str] = field(default_factory=list)
    ledger_status: str = "pending"
    reason: str = ""
    claims_written: int = 0
    claims_rejected: list[str] = field(default_factory=list)
    contradictions: int = 0

    @property
    def compiled(self) -> bool:
        return self.outcome is CompileOutcome.COMPILED


@dataclass
class Citation:
    """One page an answer actually rests on."""

    path: str
    title: str
    layer: str
    snippet: str
    sources: list[str] = field(default_factory=list)
    uncompiled: bool = False


@dataclass
class Answer:
    """A retrieval answer that can always say what it rests on."""

    question: str
    text: str
    citations: list[Citation] = field(default_factory=list)
    knowledge_gap: bool = False

    @property
    def cited_paths(self) -> list[str]:
        return [citation.path for citation in self.citations]


class KnowledgeService:
    """The vault workflow as one application service."""

    def __init__(self, *, gateway: VaultGateway, search: VaultSearch,
                 model_gateway: ModelGateway | None = None,
                 ledger: Ledger | None = None,
                 capture_service: CaptureService | None = None,
                 clock: Clock | None = None,
                 wiki_root: str = "1-wiki/concepts") -> None:
        self.gateway = gateway
        self.search = search
        self.model_gateway = model_gateway
        self.ledger = ledger or Ledger(gateway)
        self.captures = capture_service or CaptureService(gateway=gateway,
                                                          ledger=self.ledger)
        self._clock = clock or SystemClock()
        self.wiki_root = wiki_root.rstrip("/")

    # ------------------------------------------------------------------ #
    # Capture
    # ------------------------------------------------------------------ #
    def capture(self, body: str, *, title: str | None = None,
                source_kind: str = "note", origin: str = "owner",
                privacy: PrivacyLabel | None = None) -> CaptureResult:
        """Preserve exact input, register it, and make it findable.

        The search index is updated *after* the journalled write, never
        instead of it: an indexed row for a file that was not written would
        make a lost capture look retrievable.
        """
        request = CaptureRequest(
            body=body, title=title, source_kind=source_kind, origin=origin,
            privacy=privacy or PrivacyLabel.for_unlabelled_import())
        result = self.captures.capture(request, today=self._today())

        # Indexed only once *registered*. A file on disk with no ledger row is
        # an unreconciled orphan (the state a crash between the two writes
        # leaves behind), and indexing it would make an unregistered source
        # retrievable and citable before anything had claimed it exists.
        if result.registered:
            self.search.index(IndexEntry(
                path=result.path,
                title=title or _title_of(self.gateway.read_text(result.path)),
                body=self.gateway.read_text(result.path),
                privacy=request.privacy))
        return result

    def reindex(self, *, layers: tuple[str, ...] = INDEXED_LAYERS) -> int:
        """Index the vault as it exists on disk, and drop what no longer does.

        Without this, a fresh process pointed at an existing vault can only
        retrieve what *it* captured — every note written before Loop was
        installed, or by the owner in their editor since, would be invisible
        to search while appearing perfectly present in the vault. Layers are
        filtered rather than the whole tree walked blindly: `_ctx` rules,
        `_mem` personal state and `_archive` are not retrieval material.
        """
        seen: set[str] = set()
        entries: list[IndexEntry] = []
        for path in sorted(self.gateway.root.rglob("*.md")):
            relative = path.relative_to(self.gateway.root).as_posix()
            if layer_for(relative) not in layers or _is_control_file(relative):
                continue
            document = path.read_text(encoding="utf-8")
            seen.add(relative)
            entries.append(IndexEntry(path=relative, title=_title_of(document),
                                      body=document))
        self.search.index_many(entries)

        for stale in self.search.indexed_paths(layers=list(layers)) - seen:
            self.search.remove(stale)
        return len(entries)

    # ------------------------------------------------------------------ #
    # Compile
    # ------------------------------------------------------------------ #
    def compile_source(self, source_path: str, *,
                       privacy: PrivacyLabel | None = None,
                       budget: RootBudget | None = None) -> CompileReport:
        """Turn one registered pending source into a sourced wiki page."""
        row = self.ledger.find(source_path)
        if row is None:
            # Rule 1: a single-source request needs an existing pending row.
            raise InvalidInput(
                f"{source_path} is not registered in the ledger.",
                details={"source": source_path})
        if row.status != "pending":
            return CompileReport(
                source_path=source_path, outcome=CompileOutcome.DEFERRED,
                ledger_status=row.status,
                reason=f"The ledger row is {row.status!r}, not pending.")

        document = self.gateway.read_text(source_path)
        body_bytes = document.encode("utf-8")

        # Rule 2: read the *whole* source and record the receipt before
        # anything is classified. Never from a preview (V07).
        receipts = ReceiptStore(run_id=new_id())
        receipts.read_whole(source_id=source_path, path=source_path,
                            body=body_bytes)

        compiler = Compiler(receipts=receipts,
                            existing_pages=self._existing_pages())

        bare = compiler.classify_fetch(document, allow_fetch=False)
        if bare.outcome is not CompileOutcome.COMPILED:
            # A bare link has no content of its own, and single-source ingest
            # is not authorised to fetch one (V14). Recorded, not invented.
            self._set_ledger_status(source_path, bare.ledger_status)
            return CompileReport(source_path=source_path, outcome=bare.outcome,
                                 ledger_status=bare.ledger_status,
                                 reason=bare.reason)

        label = privacy or PrivacyLabel.for_unlabelled_import()
        if not self._can_extract(label):
            return CompileReport(
                source_path=source_path, outcome=CompileOutcome.DEFERRED,
                reason=("No model is configured, so claims cannot be "
                        "extracted. The capture is preserved and its ledger "
                        "row stays pending."))

        try:
            extracted = self._extract(document, privacy=label, budget=budget)
        except LoopError as exc:
            # A model that is configured but cannot produce a valid claim set
            # — offline, too small for the schema, rate-limited — stops *this*
            # source, not the batch. The capture is preserved and the row
            # stays pending, so the same source is retried when the model
            # situation changes rather than being marked processed by a run
            # that extracted nothing.
            logger.warning("Extraction failed for %s: %s", source_path, exc)
            return CompileReport(
                source_path=source_path, outcome=CompileOutcome.DEFERRED,
                reason=f"Claim extraction did not succeed: {exc.message}")

        claims, rejected = self._verify_claims(
            extracted.get("claims") or [], source_path=source_path,
            body=body_bytes)

        if not claims:
            return CompileReport(
                source_path=source_path, outcome=CompileOutcome.NEEDS_CONTEXT,
                claims_rejected=rejected,
                reason=("No claim survived evidence verification, so nothing "
                        "was written. The capture is preserved."))

        # Rule 3 / V06: refuse the write unless every span was actually read.
        compiler.compile_claims(source_path, claims)

        linkable = self._linkable_pages(extracted.get("page_title", ""), claims)
        isolated = compiler.assess_isolation(claims, linkable=linkable)
        if isolated is not None:
            return CompileReport(
                source_path=source_path, outcome=isolated.outcome,
                ledger_status=isolated.ledger_status,
                reason=isolated.reason, claims_rejected=rejected)

        page = self._build_page(extracted, claims, source_path=source_path,
                                links=linkable)
        contradictions = compiler.find_contradictions(claims)
        page = compiler.apply_contradictions(page, contradictions)
        page = compiler.respect_ownership(page)
        validate_page(page)

        self._write_page_and_ledger(page, source_path=source_path)
        self.search.index(IndexEntry(path=page.path, title=page.title,
                                     body=page.body))

        return CompileReport(
            source_path=source_path, outcome=CompileOutcome.COMPILED,
            pages=[page.path], ledger_status="compiled",
            claims_written=len(claims), claims_rejected=rejected,
            contradictions=len(contradictions),
            reason=f"Wrote {page.path} from {len(claims)} verified claim(s).")

    def compile_pending(self, *, limit: int = 10,
                        privacy: PrivacyLabel | None = None
                        ) -> list[CompileReport]:
        """Compile the pending backlog, bounded (rule 9's batch of ten)."""
        pending = [row.source for row in self.ledger.rows()
                   if row.status == "pending"][:limit]
        return [self.compile_source(path, privacy=privacy) for path in pending]

    # ------------------------------------------------------------------ #
    # Answer
    # ------------------------------------------------------------------ #
    def answer(self, question: str, *, limit: int = 5,
               allow_local_only: bool = True) -> Answer:
        """Retrieve wiki first, then raw — and say which one it found.

        Seeker searches the wiki first and labels a raw fallback as uncompiled
        (vault §7). Presenting an uncompiled capture as an answer without that
        label is how an unverified note becomes indistinguishable from
        compiled knowledge.
        """
        wiki = self.search.search(question, limit=limit, layers=["wiki"],
                                  allow_local_only=allow_local_only)
        if wiki:
            citations = [self._cite(hit) for hit in wiki]
            return Answer(question=question,
                          text=self._compose(question, citations),
                          citations=citations)

        raw = self.search.search(question, limit=limit, layers=["raw"],
                                 allow_local_only=allow_local_only)
        if raw:
            citations = [self._cite(hit, uncompiled=True) for hit in raw]
            return Answer(
                question=question,
                text=("Nothing compiled covers this yet. From uncompiled "
                      "captures only:\n" + self._compose(question, citations)),
                citations=citations)

        return Answer(
            question=question, knowledge_gap=True,
            text=("I have nothing in the vault about that. Capture something "
                  "on it, or ask me to research it."))

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _today(self) -> date:
        return self._clock.now().date()

    def _existing_pages(self) -> dict[str, str]:
        """Wiki pages already on disk, for ownership and duplicate rules."""
        pages: dict[str, str] = {}
        root = self.gateway.resolve("1-wiki")
        if not root.is_dir():
            return pages
        for path in sorted(root.rglob("*.md")):
            relative = path.relative_to(self.gateway.root).as_posix()
            pages[relative] = path.read_text(encoding="utf-8")
        return pages

    def _can_extract(self, label: PrivacyLabel) -> bool:
        """Whether a model this content may legally reach actually exists.

        A local-only source with no local model is not a cloud opportunity:
        the answer is that compile cannot run, not that the note gets sent
        somewhere it was never allowed to go.
        """
        if self.model_gateway is None:
            return False
        if self.model_gateway.has_local_model:
            return True
        return not label.is_local_only and self.model_gateway.has_cloud_model

    def _extract(self, document: str, *, privacy: PrivacyLabel,
                 budget: RootBudget | None) -> dict[str, Any]:
        """Ask the model for claims, as a schema-valid object."""
        assert self.model_gateway is not None
        from langchain_core.messages import HumanMessage, SystemMessage

        label = privacy
        model = gateway_chat_model(self.model_gateway, labels=[label],
                                   budget=budget, purpose="compile")
        # `invoke_structured` raises `ValidationFailed` when every attempt
        # including its repairs fails the schema, so there is no `None` case
        # to check for here — the caller catches `LoopError` instead.
        result = invoke_structured(
            model,
            [SystemMessage(content=EXTRACTION_SYSTEM),
             HumanMessage(content=document)],
            schema=CLAIMS_SCHEMA, budget=budget, label="claim set")
        return result.value or {}

    def _verify_claims(self, raw_claims: list[Any], *, source_path: str,
                       body: bytes) -> tuple[list[Claim], list[str]]:
        """Turn proposed claims into evidence anchored in real bytes.

        The quote is searched for in the source itself. A model that
        paraphrases its own evidence — or invents it — produces a claim with
        no locatable span, and that claim is dropped rather than written with
        a span nobody checked.
        """
        digest = content_hash(body)
        claims: list[Claim] = []
        rejected: list[str] = []

        for item in raw_claims:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            quote = str(item.get("quote", ""))
            if not text or not quote.strip():
                rejected.append(f"{text or '(no text)'}: no quote supplied")
                continue

            start = body.find(quote.encode("utf-8"))
            if start < 0:
                rejected.append(
                    f"{text}: the supporting quote is not in the source")
                continue

            claims.append(Claim(
                text=text, subject=str(item.get("subject", "")),
                attributed_to=str(item.get("attributed_to", "")),
                evidence=Evidence(
                    source_id=source_path, path=source_path,
                    span=ByteRange(start, start + len(quote.encode("utf-8"))),
                    content_hash=digest, quote=quote)))
        return claims, rejected

    def _linkable_pages(self, title: str, claims: list[Claim]) -> list[str]:
        """Existing wiki pages this source genuinely relates to (rule 8).

        Link targets come from the index, never from the model: rule 7 forbids
        fabricating a link target, and a `[[...]]` to a page that does not
        exist is exactly that.
        """
        query = " ".join([title, *(claim.text for claim in claims)])
        hits = self.search.search(query, limit=5, layers=["wiki"])
        return [hit.title for hit in hits if hit.title]

    def _build_page(self, extracted: dict[str, Any], claims: list[Claim], *,
                    source_path: str, links: list[str]) -> PageProposal:
        title = str(extracted.get("page_title") or "").strip()
        if not title:
            raise InvalidInput("The extraction returned no page title.")

        lines: list[str] = []
        summary = str(extracted.get("summary", "")).strip()
        if summary:
            lines.extend([summary, ""])
        for claim in claims:
            prefix = f"{claim.attributed_to} states: " if claim.is_attributed else ""
            lines.append(f"- {prefix}{claim.text}")

        return PageProposal(
            path=f"{self.wiki_root}/{_slug(title)}.md", title=title,
            body="\n".join(lines),
            page_type=str(extracted.get("page_type") or "concept"),
            owner="model",
            # "stated" is what a single source supports. A source supporting a
            # claim is not proof the claim is true (vault §6).
            confidence="stated", sources=[source_path], links=links)

    def _write_page_and_ledger(self, page: PageProposal, *,
                               source_path: str) -> None:
        """One journalled operation: the page, then its ledger transition.

        Rule 8 requires the ledger to move only once the page operations are
        committed. Doing both in one journalled transaction is how a crash in
        between leaves a recoverable record rather than a compiled page with a
        pending row — or, worse, a pending row's second compile appending the
        same claims to the page again.
        """
        existing = self.gateway.exists(page.path)
        if page.append_only and existing:
            body = self.gateway.read_text(page.path) + page.body
            mode = WriteMode.REPLACE
            expected = self.gateway.hash_of(page.path)
        else:
            body = page.to_markdown()
            mode = WriteMode.REPLACE if existing else WriteMode.CREATE_NEW
            expected = self.gateway.hash_of(page.path) if existing else None

        page_op = self.gateway.make_operation(page.path, body, mode=mode,
                                              expected_hash=expected)
        rows = self.ledger.set_status(
            source_path, "compiled", compiled=self._today().isoformat(),
            pages_produced=page.path)
        ledger_op = self.gateway.make_operation(
            self.ledger.relative_path, render(rows), mode=WriteMode.REPLACE,
            expected_hash=self.gateway.hash_of(self.ledger.relative_path))
        self.gateway.apply(self.gateway.begin([page_op, ledger_op]))

    def _set_ledger_status(self, source_path: str, status: str) -> None:
        rows = self.ledger.set_status(source_path, status)
        self.gateway.apply(self.gateway.begin([self.gateway.make_operation(
            self.ledger.relative_path, render(rows), mode=WriteMode.REPLACE,
            expected_hash=self.gateway.hash_of(self.ledger.relative_path))]))

    def _cite(self, hit: Any, *, uncompiled: bool = False) -> Citation:
        """Read the page before citing it (vault §7)."""
        sources: list[str] = []
        if self.gateway.exists(hit.path):
            parsed = parse_page(self.gateway.read_text(hit.path))
            sources = [str(s) for s in (parsed["frontmatter"].get("sources") or [])]
        return Citation(path=hit.path, title=hit.title,
                        layer=hit.layer or layer_for(hit.path),
                        snippet=hit.snippet, sources=sources,
                        uncompiled=uncompiled)

    @staticmethod
    def _compose(question: str, citations: list[Citation]) -> str:
        """List supporting pages first, with their own source chain intact.

        Saving a generated answer never makes it independent evidence, so the
        underlying raw source of every cited page travels with the answer
        (vault §7).
        """
        del question
        lines: list[str] = []
        for citation in citations:
            lines.append(f"- {citation.title} ({citation.path}): "
                         f"{citation.snippet}")
            for source in citation.sources:
                lines.append(f"    source: {source}")
        return "\n".join(lines)


def _is_control_file(relative: str) -> bool:
    """Underscore-prefixed files are the vault's own bookkeeping.

    `0-raw/_ledger.md` is the clearest case: it is a register of what has been
    captured, and indexing it means a question about "supplier lead time" can
    come back citing the *ledger row* as though it were the note.
    """
    return Path(relative).name.startswith("_")


def _title_of(document: str) -> str:
    parsed = parse_page(document)
    return str(parsed["frontmatter"].get("title") or "Untitled")


def _slug(title: str) -> str:
    from loop.vault.capture import slugify
    return slugify(title)
