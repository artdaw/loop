"""Research → save → output, with a source chain that survives approval.

The Seeker gathers sources; the Reviewer checks the draft. Both are models, and
**both can be wrong in the same direction**: a researcher that invents a citation
and a reviewer that waves it through are not independent failures, because the
reviewer is reading the same fabricated text. Two model opinions do not add up to
evidence.

So the gate is not the review. Every claim that reaches compilation must cite a
source Loop actually read, at a span the read receipt actually covers, and the
validator that checks this is deterministic and runs *after* the review. A
Reviewer verdict of "approved" is advisory input to a human, never a permission
(runtime §1: Reviewer checks cannot grant permissions).

This is the same shape as the rest of Loop: models propose, deterministic
services validate and execute.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from loop.core.errors import ValidationFailed
from loop.vault.receipts import ByteRange, Evidence, ReceiptStore

logger = logging.getLogger(__name__)


class ClaimStatus(str, Enum):
    SUPPORTED = "supported"
    UNCITED = "uncited"
    UNREAD_SOURCE = "unread_source"
    UNCOVERED_SPAN = "uncovered_span"
    UNCOMPILED_SOURCE = "uncompiled_source"

    @property
    def blocks_compilation(self) -> bool:
        return self is not ClaimStatus.SUPPORTED


@dataclass
class Citation:
    """A pointer into a specific version of a specific source.

    ``content_hash`` pins the version: a citation into a file that has since
    been edited points at bytes that may no longer say what was quoted.
    """

    source_id: str
    start: int
    end: int
    path: str = ""
    content_hash: str = ""
    quote: str = ""

    @property
    def span(self) -> ByteRange:
        return ByteRange(self.start, self.end)


@dataclass
class Claim:
    """One assertion in a draft, with whatever the researcher cited for it."""

    text: str
    citations: list[Citation] = field(default_factory=list)

    @property
    def is_cited(self) -> bool:
        return bool(self.citations)


@dataclass
class ResearchDraft:
    """Model output. Nothing here is trusted until the validator says so."""

    run_id: str
    question: str
    claims: list[Claim] = field(default_factory=list)
    notes: str = ""


@dataclass
class ReviewVerdict:
    """The Reviewer's opinion. Advisory — it authorises nothing (runtime §1)."""

    approved: bool
    reviewer: str = "reviewer"
    comment: str = ""


@dataclass
class ClaimFinding:
    claim_text: str
    status: ClaimStatus
    detail: str

    @property
    def blocks(self) -> bool:
        return self.status.blocks_compilation


@dataclass
class ValidationReport:
    """The deterministic verdict, which is the one that counts."""

    findings: list[ClaimFinding] = field(default_factory=list)

    @property
    def blocking(self) -> list[ClaimFinding]:
        return [f for f in self.findings if f.blocks]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def reasons(self) -> list[str]:
        return [f"{f.claim_text[:60]}: {f.detail}" for f in self.blocking]


def validate_draft(draft: ResearchDraft, *, receipts: ReceiptStore,
                   compiled_sources: set[str] | None = None) -> ValidationReport:
    """Check every claim against read receipts. No model is consulted.

    Four ways a claim fails, kept distinct because they need different fixes:
    it cites nothing; it cites a source that was never read; it cites a span the
    receipt does not cover (a preview read cannot support a claim about page
    twelve); or it cites a source that is not in the compiled set.
    """
    report = ValidationReport()
    read = receipts.sources_read()
    compiled = compiled_sources if compiled_sources is not None else read

    for claim in draft.claims:
        if not claim.is_cited:
            report.findings.append(ClaimFinding(
                claim.text, ClaimStatus.UNCITED,
                "the claim cites no source"))
            continue

        failed = False
        for citation in claim.citations:
            if citation.source_id not in read:
                report.findings.append(ClaimFinding(
                    claim.text, ClaimStatus.UNREAD_SOURCE,
                    f"{citation.source_id} was never read in this run"))
                failed = True
                break

            receipt = receipts.get(citation.source_id)
            if receipt is None or not receipt.covers(citation.span):
                report.findings.append(ClaimFinding(
                    claim.text, ClaimStatus.UNCOVERED_SPAN,
                    f"bytes {citation.start}-{citation.end} of "
                    f"{citation.source_id} were not read"))
                failed = True
                break

            if citation.source_id not in compiled:
                report.findings.append(ClaimFinding(
                    claim.text, ClaimStatus.UNCOMPILED_SOURCE,
                    f"{citation.source_id} is not a compiled source"))
                failed = True
                break

        if not failed:
            report.findings.append(ClaimFinding(
                claim.text, ClaimStatus.SUPPORTED, "cited and read"))

    return report


def compile_draft(draft: ResearchDraft, *, receipts: ReceiptStore,
                  review: ReviewVerdict | None = None,
                  compiled_sources: set[str] | None = None) -> list[str]:
    """Produce output, or refuse (A05).

    ``review`` is accepted and recorded, and deliberately does not participate
    in the decision. An approving reviewer cannot unblock an uncited claim, and
    a rejecting one cannot be overridden by the validator passing — a human
    still reads the review.
    """
    report = validate_draft(draft, receipts=receipts,
                            compiled_sources=compiled_sources)

    if report.blocking:
        raise ValidationFailed(
            "This draft contains claims that are not supported by sources Loop "
            "actually read, so it cannot be compiled.",
            details={"run_id": draft.run_id,
                     "reviewer_approved": bool(review and review.approved),
                     "blocking": report.reasons()})

    return [claim.text for claim in draft.claims]


def evidence_for(claims: list[Claim]) -> list[Evidence]:
    """Convert citations into the receipt store's evidence form."""
    return [Evidence(source_id=citation.source_id, path=citation.path,
                     span=citation.span, content_hash=citation.content_hash,
                     quote=citation.quote)
            for claim in claims for citation in claim.citations]
