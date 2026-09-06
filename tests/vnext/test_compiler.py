"""Compile contract: the nine rules (vault §2, §6).

Covers V08, V09, V10, V11, V14, V15, V16, V17, V18, V22, V32.
"""

from __future__ import annotations

from datetime import date

import pytest

from loop.core.errors import InvalidInput, ValidationFailed
from loop.core.ids import content_hash
from loop.vault.compiler import (
    Claim,
    CompileOutcome,
    Compiler,
    PageProposal,
    is_bare_link,
    looks_like_login_wall,
    parse_page,
    validate_page,
)
from loop.vault.receipts import ByteRange, Evidence, ReceiptStore

BODY = b"The supplier said production takes six weeks.\n"
FETCHED_ON = date(2026, 9, 6)

HUMAN_PAGE = """---
type: entity
title: Nordic Panels
owner: human
confidence: stated
sources:
  - manual
---

Hand-written notes. Do not rewrite this body.
"""

MODEL_PAGE = """---
type: concept
title: Lead time
owner: model
confidence: stated
sources:
  - 0-raw/inbox/a.md
---

Time between order and delivery.
"""


@pytest.fixture
def receipts() -> ReceiptStore:
    store = ReceiptStore(run_id="run-1")
    store.read_whole(source_id="s1", path="0-raw/inbox/a.md", body=BODY)
    return store


@pytest.fixture
def compiler(receipts) -> Compiler:
    return Compiler(receipts=receipts, existing_pages={
        "1-wiki/entities/Nordic Panels.md": HUMAN_PAGE,
        "1-wiki/concepts/lead-time.md": MODEL_PAGE,
    })


def _claim(text: str, *, subject: str = "", attributed_to: str = "",
           source_id: str = "s1") -> Claim:
    return Claim(text=text, subject=subject, attributed_to=attributed_to,
                 evidence=Evidence(source_id=source_id, path="0-raw/inbox/a.md",
                                   span=ByteRange(0, 44),
                                   content_hash=content_hash(BODY)))


def _page(**kw: object) -> PageProposal:
    defaults: dict[str, object] = {
        "path": "1-wiki/concepts/lead-time.md", "title": "Lead time",
        "body": "Time between order and delivery.",
        "sources": ["0-raw/inbox/a.md"]}
    defaults.update(kw)
    return PageProposal(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Rule 3 — every page has sources
# --------------------------------------------------------------------------- #
def test_a_page_without_sources_is_rejected():
    with pytest.raises(ValidationFailed):
        validate_page(_page(sources=[]))


def test_an_invalid_confidence_is_rejected():
    with pytest.raises(ValidationFailed):
        validate_page(_page(confidence="probably"))


def test_an_invalid_owner_is_rejected():
    with pytest.raises(ValidationFailed):
        validate_page(_page(owner="robot"))


def test_a_valid_page_passes():
    validate_page(_page())


def test_the_rendered_page_carries_its_frontmatter():
    parsed = parse_page(_page().to_markdown())
    assert parsed["frontmatter"]["sources"] == ["0-raw/inbox/a.md"]


# --------------------------------------------------------------------------- #
# V08 — a source touching several existing concepts
# --------------------------------------------------------------------------- #
def test_v08_claims_backed_by_evidence_compile(compiler):
    compiler.compile_claims("s1", [_claim("lead time is six weeks")],
                            require_complete_read=True)


def test_v08_a_claim_without_evidence_is_refused(compiler):
    unbacked = Claim(text="invented", evidence=Evidence(
        source_id="nope", path="x", span=ByteRange(0, 5),
        content_hash=content_hash(b"x")))

    with pytest.raises(ValidationFailed):
        compiler.compile_claims("s1", [unbacked])


def test_v08_links_are_recorded_on_the_page():
    page = _page(links=["Acoustics", "Nordic Panels"])
    assert "[[Acoustics]]" in page.to_markdown()


def test_v08_no_forced_page_count_when_links_exist(compiler):
    """Several claims connecting to real pages is a normal compile."""
    claims = [_claim("a"), _claim("b")]
    assert compiler.assess_isolation(claims, linkable=["1-wiki/topics/x.md"]) is None


# --------------------------------------------------------------------------- #
# V09 — a genuinely isolated one-fact source
# --------------------------------------------------------------------------- #
def test_v09_an_isolated_single_claim_needs_context(compiler):
    result = compiler.assess_isolation([_claim("one lonely fact")], linkable=[])

    assert result.outcome is CompileOutcome.NEEDS_CONTEXT


def test_v09_the_ledger_row_stays_pending(compiler):
    result = compiler.assess_isolation([_claim("one lonely fact")], linkable=[])
    assert result.ledger_status == "pending"


def test_v09_no_page_is_fabricated(compiler):
    """The failure mode: inventing a second concept to satisfy rule 8."""
    result = compiler.assess_isolation([_claim("one lonely fact")], linkable=[])

    assert result.pages == []
    assert "no concept invented" in result.reason.lower()


def test_v09_the_reason_is_recorded_for_the_user(compiler):
    result = compiler.assess_isolation([_claim("x")], linkable=[])
    assert "awaiting context" in result.reason.lower()


def test_v09_a_linkable_page_means_it_is_not_isolated(compiler):
    assert compiler.assess_isolation(
        [_claim("x")], linkable=["1-wiki/concepts/lead-time.md"]) is None


# --------------------------------------------------------------------------- #
# V10 — contradictions
# --------------------------------------------------------------------------- #
def test_v10_two_disagreeing_claims_are_detected(compiler):
    contradictions = compiler.find_contradictions([
        _claim("six weeks", subject="lead time"),
        _claim("ten weeks", subject="lead time"),
    ])
    assert len(contradictions) == 1


def test_v10_agreeing_claims_are_not_a_contradiction(compiler):
    assert compiler.find_contradictions([
        _claim("six weeks", subject="lead time"),
        _claim("six weeks", subject="lead time"),
    ]) == []


def test_v10_claims_about_different_subjects_do_not_conflict(compiler):
    assert compiler.find_contradictions([
        _claim("six weeks", subject="lead time"),
        _claim("blue", subject="colour"),
    ]) == []


def test_v10_both_sides_are_retained_on_the_page(compiler):
    contradictions = compiler.find_contradictions([
        _claim("six weeks", subject="lead time"),
        _claim("ten weeks", subject="lead time"),
    ])
    page = compiler.apply_contradictions(_page(), contradictions)

    assert "six weeks" in page.body
    assert "ten weeks" in page.body


def test_v10_confidence_becomes_contested(compiler):
    contradictions = compiler.find_contradictions([
        _claim("six weeks", subject="lead time"),
        _claim("ten weeks", subject="lead time"),
    ])
    page = compiler.apply_contradictions(_page(), contradictions)

    assert page.confidence == "contested"


def test_v10_neither_side_is_dropped_in_favour_of_the_newer(compiler):
    """The tempting shortcut: keep the latest claim and move on."""
    contradictions = compiler.find_contradictions([
        _claim("six weeks", subject="lead time"),
        _claim("ten weeks", subject="lead time"),
    ])
    page = compiler.apply_contradictions(_page(), contradictions)

    assert page.body.count("weeks") >= 2


def test_v10_an_open_question_entry_names_both_sources(compiler):
    contradictions = compiler.find_contradictions([
        _claim("six weeks", subject="lead time", source_id="s1"),
        _claim("ten weeks", subject="lead time", source_id="s1"),
    ])
    entry = compiler.open_questions_entry(contradictions)

    assert "lead time" in entry
    assert "six weeks" in entry and "ten weeks" in entry


def test_a_user_statement_stays_attributed(compiler):
    claim = _claim("production takes six weeks",
                   attributed_to="the supplier")
    assert claim.is_attributed is True


# --------------------------------------------------------------------------- #
# V11 — human-owned pages
# --------------------------------------------------------------------------- #
def test_v11_a_human_page_is_detected(compiler):
    assert compiler.is_human_owned("1-wiki/entities/Nordic Panels.md") is True
    assert compiler.is_human_owned("1-wiki/concepts/lead-time.md") is False


def test_v11_a_rewrite_of_a_human_page_becomes_an_append(compiler):
    page = compiler.respect_ownership(
        _page(path="1-wiki/entities/Nordic Panels.md",
              title="Nordic Panels", body="Model rewrite of everything."))

    assert page.append_only is True


def test_v11_the_appended_content_is_labelled_as_compiler_notes(compiler):
    page = compiler.respect_ownership(
        _page(path="1-wiki/entities/Nordic Panels.md",
              title="Nordic Panels", body="An observation."))

    assert page.body.strip().startswith("## Compiler notes")


def test_v11_ownership_is_preserved_as_human(compiler):
    page = compiler.respect_ownership(
        _page(path="1-wiki/entities/Nordic Panels.md", title="Nordic Panels",
              body="x", owner="model"))

    assert page.owner == "human"


def test_v11_a_model_page_is_not_forced_into_append_mode(compiler):
    page = compiler.respect_ownership(_page())
    assert page.append_only is False


def test_v11_a_new_page_is_unaffected(compiler):
    page = compiler.respect_ownership(_page(path="1-wiki/concepts/brand-new.md"))
    assert page.append_only is False


# --------------------------------------------------------------------------- #
# V14 / V15 — fetch authority
# --------------------------------------------------------------------------- #
BARE_LINK = "---\ntype: source\n---\n\nhttps://example.invalid/acoustics\n"


def test_v14_a_bare_link_is_recognised():
    assert is_bare_link(BARE_LINK) is True


def test_v14_a_source_with_prose_is_not_a_bare_link():
    assert is_bare_link("---\ntype: source\n---\n\nReal content here.\n") is False


def test_v14_single_source_ingest_does_not_fetch(compiler):
    result = compiler.classify_fetch(BARE_LINK, allow_fetch=False)

    assert result.outcome is CompileOutcome.UNFETCHED
    assert result.ledger_status == "unfetched"


def test_v14_no_page_content_is_invented_without_a_fetch(compiler):
    result = compiler.classify_fetch(BARE_LINK, allow_fetch=False)

    assert result.pages == []
    assert "invented" in result.reason.lower()


def test_v15_an_authorised_fetch_creates_a_new_adjacent_source(compiler):
    path = compiler.fetched_source_path("0-raw/clips/2026-09-04-panels.md",
                                        fetched_on=FETCHED_ON)

    assert path == "0-raw/clips/2026-09-04-panels--fetched-2026-09-06.md"


def test_v15_the_fetch_creates_a_sibling_not_an_edit(compiler):
    """Raw sources are immutable (rule 1): the bookmark is never rewritten."""
    original = "0-raw/clips/2026-09-04-panels.md"
    fetched = compiler.fetched_source_path(original, fetched_on=FETCHED_ON)

    assert fetched != original
    assert fetched.startswith(original.removesuffix(".md"))
    assert fetched.endswith(".md")


def test_v15_the_fetched_name_records_the_retrieval_date(compiler):
    fetched = compiler.fetched_source_path("0-raw/clips/x.md",
                                           fetched_on=FETCHED_ON)
    assert "2026-09-06" in fetched


# --------------------------------------------------------------------------- #
# V16 — login walls are not content
# --------------------------------------------------------------------------- #
def test_v16_a_login_wall_is_detected():
    assert looks_like_login_wall("Please sign in to continue reading.") is True


def test_v16_a_javascript_shell_is_detected():
    assert looks_like_login_wall("Please enable JavaScript to view.") is True


def test_v16_a_short_stub_is_not_substantive_content():
    assert looks_like_login_wall("404") is True


def test_v16_real_prose_is_accepted():
    prose = ("Acoustic performance depends on panel thickness, mounting method "
             "and the air gap behind the panel. Thicker panels absorb lower "
             "frequencies more effectively than thin ones do.")
    assert looks_like_login_wall(prose) is False


def test_v16_a_wall_becomes_unfetchable_with_a_reason(compiler):
    result = compiler.classify_fetched_body("Please sign in to continue.",
                                            fetched_on=FETCHED_ON)

    assert result.outcome is CompileOutcome.UNFETCHABLE
    assert result.ledger_status == "unfetchable"
    assert "2026-09-06" in result.reason


def test_v16_no_claims_are_compiled_from_a_wall(compiler):
    result = compiler.classify_fetched_body("Subscribe to read this article.",
                                            fetched_on=FETCHED_ON)
    assert result.pages == []


# --------------------------------------------------------------------------- #
# V17 — transient failures
# --------------------------------------------------------------------------- #
def test_v17_a_transient_failure_stays_pending(compiler):
    result = compiler.classify_fetch(BARE_LINK, allow_fetch=True,
                                     fetch_error="connection reset",
                                     transient=True)

    assert result.outcome is CompileOutcome.DEFERRED
    assert result.ledger_status == "pending"


def test_v17_a_transient_failure_is_not_marked_unfetchable(compiler):
    result = compiler.classify_fetch(BARE_LINK, allow_fetch=True,
                                     fetch_error="timeout", transient=True)
    assert result.ledger_status != "unfetchable"


def test_v17_a_permanent_failure_is_unfetchable(compiler):
    result = compiler.classify_fetch(BARE_LINK, allow_fetch=True,
                                     fetch_error="410 gone", transient=False)

    assert result.outcome is CompileOutcome.UNFETCHABLE
    assert "410 gone" in result.reason


def test_v17_no_content_is_fabricated_on_failure(compiler):
    result = compiler.classify_fetch(BARE_LINK, allow_fetch=True,
                                     fetch_error="timeout", transient=True)
    assert result.pages == []


# --------------------------------------------------------------------------- #
# V22 — near-duplicate reconciliation
# --------------------------------------------------------------------------- #
def test_v22_merging_keeps_sources_from_both_pages(compiler):
    stronger = _page(path="1-wiki/concepts/strong.md",
                     sources=["0-raw/inbox/b.md"])

    result = compiler.reconcile_duplicates(stronger,
                                           "1-wiki/concepts/lead-time.md")

    assert "0-raw/inbox/b.md" in result.pages[0].sources
    assert "0-raw/inbox/a.md" in result.pages[0].sources


def test_v22_the_weaker_page_is_archived_with_a_trail(compiler):
    result = compiler.reconcile_duplicates(
        _page(path="1-wiki/concepts/strong.md"), "1-wiki/concepts/lead-time.md")

    assert result.archived == ["1-wiki/concepts/lead-time.md"]
    assert "Merged" in result.reason


def test_v22_the_ledger_marks_the_superseded_source(compiler):
    result = compiler.reconcile_duplicates(
        _page(path="1-wiki/concepts/strong.md"), "1-wiki/concepts/lead-time.md")
    assert result.ledger_status == "superseded"


def test_v22_a_human_page_cannot_be_merged_away(compiler):
    """Rule 5 still holds during a rule 9 merge."""
    with pytest.raises(ValidationFailed):
        compiler.reconcile_duplicates(_page(path="1-wiki/concepts/strong.md"),
                                      "1-wiki/entities/Nordic Panels.md")


def test_v22_merging_a_missing_page_is_an_error(compiler):
    with pytest.raises(InvalidInput):
        compiler.reconcile_duplicates(_page(), "1-wiki/concepts/absent.md")


# --------------------------------------------------------------------------- #
# V32 — metadata follows prose, not the reverse
# --------------------------------------------------------------------------- #
CONTESTED_PROSE = """---
type: concept
title: Lead time
owner: model
confidence: stated
sources:
  - 0-raw/inbox/a.md
---

## Contested

- six weeks (source: a)
- ten weeks (source: b)
"""


def test_v32_metadata_is_aligned_to_the_prose(compiler):
    updated, changed = compiler.align_confidence(CONTESTED_PROSE)

    assert changed is True
    assert parse_page(updated)["frontmatter"]["confidence"] == "contested"


def test_v32_the_prose_is_preserved_verbatim(compiler):
    updated, _ = compiler.align_confidence(CONTESTED_PROSE)

    assert "- six weeks (source: a)" in updated
    assert "- ten weeks (source: b)" in updated


def test_v32_an_already_consistent_page_is_untouched(compiler):
    consistent = CONTESTED_PROSE.replace("confidence: stated",
                                         "confidence: contested")
    updated, changed = compiler.align_confidence(consistent)

    assert changed is False
    assert updated == consistent


def test_v32_a_plain_page_is_not_marked_contested(compiler):
    updated, changed = compiler.align_confidence(MODEL_PAGE)
    assert changed is False


# --------------------------------------------------------------------------- #
# V18 — a repeated sweep with nothing pending
# --------------------------------------------------------------------------- #
def test_v18_a_sweep_with_no_claims_produces_no_pages(compiler):
    """Bounded work: nothing pending means nothing written."""
    result = compiler.assess_isolation([], linkable=[])

    assert result is not None
    assert result.pages == []


def test_v18_repeated_isolation_checks_are_stable(compiler):
    first = compiler.assess_isolation([_claim("x")], linkable=[])
    second = compiler.assess_isolation([_claim("x")], linkable=[])

    assert first.outcome == second.outcome
    assert first.ledger_status == second.ledger_status
