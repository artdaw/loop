"""M5: natural-language capture → exact raw + ledger → sourced wiki → cited answer.

The milestone's exit criterion for the vault half, run end to end through
`build_application()` so the composition root, the gateway, the ledger, the
receipt store, the compiler rules and the search index are all the real code.
The only fake is the extraction model — and the tests below are mostly about
what happens when that model is *wrong*, because a compile path that only
works with a cooperative model is not provenance, it is optimism.

The synthetic vault is built by `vault_fixtures.build_minimal_vault`, which
deliberately includes the awkward cases: pipes in a body, a human-owned page,
a bare-link source with nothing to compile.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from loop.ai.model_gateway import ModelGateway
from loop.app import build_application
from loop.core.errors import InvalidInput
from loop.core.settings import Settings
from loop.vault.compiler import CompileOutcome, parse_page
from tests.vnext.vault_fixtures import build_minimal_vault

SUPPLIER_NOTE = ("The supplier said production takes six weeks | confirmed by "
                 "email   ")


def fake_gateway(*payloads: dict | str) -> ModelGateway:
    """A gateway whose local model returns exactly these responses, in order."""
    messages = [AIMessage(content=p if isinstance(p, str)
                          else json.dumps(p)) for p in payloads]
    settings = Settings(_env_file=None,  # type: ignore[call-arg]
                        ollama_default_model="llama3.1:8b")
    return ModelGateway(settings=settings,
                        local_model=GenericFakeChatModel(messages=iter(messages)))


def extraction(*claims: dict, title: str = "Supplier Lead Times",
               summary: str = "How long the supplier takes to produce an order."
               ) -> dict:
    return {"page_title": title, "page_type": "concept", "summary": summary,
            "claims": list(claims)}


def claim(text: str, quote: str, **kw) -> dict:
    return {"text": text, "quote": quote, **kw}


@pytest.fixture
def vault(tmp_path: Path):
    return build_minimal_vault(tmp_path / "vault")


@pytest.fixture
def vault_settings(settings: Settings, vault) -> Settings:
    return settings.model_copy(update={"obsidian_vault_path": str(vault.root)})


def make_app(vault_settings: Settings, clock, *payloads, index: bool = True):
    """Build the real application, and index the vault it was pointed at.

    `reindex` is explicit rather than automatic at startup: walking a large
    vault on every process start would make `loop status` slow for no reason.
    A test that skips it is testing an unindexed vault, which is a different
    thing and says so.
    """
    app = build_application(
        vault_settings, clock=clock,
        model_gateway=fake_gateway(*payloads) if payloads else None)
    assert app.knowledge is not None, "the vault settings configure one"
    if index:
        app.knowledge.reindex()
    return app


# --------------------------------------------------------------------------- #
# Capture: exactly what was said, and a ledger row to prove it
# --------------------------------------------------------------------------- #
def test_a_captured_note_is_preserved_byte_for_byte(vault_settings, clock, vault):
    app = make_app(vault_settings, clock)

    result = app.knowledge.capture(SUPPLIER_NOTE)

    assert result.status == "saved"
    assert result.message.startswith("Saved to your vault")
    stored = vault.read(result.path)
    # Trailing whitespace and the pipe both survive: the body is concatenated
    # verbatim, only the frontmatter is serialised (vault §5).
    assert stored.endswith(SUPPLIER_NOTE)
    assert "|" in stored


def test_a_capture_lands_at_the_dated_inbox_path(vault_settings, clock):
    app = make_app(vault_settings, clock)

    result = app.knowledge.capture(SUPPLIER_NOTE)

    assert result.path.startswith("0-raw/inbox/2026-09-05-")
    assert result.path.endswith(".md")


def test_a_capture_is_registered_in_the_ledger_as_pending(vault_settings, clock,
                                                          vault):
    app = make_app(vault_settings, clock)

    result = app.knowledge.capture(SUPPLIER_NOTE)

    row = app.knowledge.ledger.find(result.path)
    assert row is not None
    assert row.status == "pending"
    assert row.batch == "inbox"
    # The pipe in the body did not become a seventh column.
    assert all(len(cells) == 6 for cells in vault.ledger_rows())


def test_a_title_with_a_pipe_is_refused_rather_than_rewritten(vault_settings,
                                                              clock):
    app = make_app(vault_settings, clock)

    with pytest.raises(InvalidInput):
        app.knowledge.capture("body", title="lead | time")


def test_a_capture_is_immediately_findable(vault_settings, clock):
    app = make_app(vault_settings, clock)
    result = app.knowledge.capture(SUPPLIER_NOTE)

    hits = app.vault_search.search("supplier")

    assert result.path in [hit.path for hit in hits]


# --------------------------------------------------------------------------- #
# Compile: a page whose claims point at bytes that were read
# --------------------------------------------------------------------------- #
def test_a_captured_note_compiles_into_a_sourced_wiki_page(vault_settings, clock):
    app = make_app(vault_settings, clock, extraction(
        claim("Production takes six weeks.", "production takes six weeks")))
    captured = app.knowledge.capture(SUPPLIER_NOTE)

    report = app.knowledge.compile_source(captured.path)

    assert report.outcome is CompileOutcome.COMPILED
    assert report.pages == ["1-wiki/concepts/supplier-lead-times.md"]
    page = parse_page(app.knowledge.gateway.read_text(report.pages[0]))
    assert page["frontmatter"]["sources"] == [captured.path]
    assert page["frontmatter"]["owner"] == "model"
    assert "six weeks" in page["body"]


def test_compiling_moves_the_ledger_row_and_records_the_page(vault_settings,
                                                             clock):
    app = make_app(vault_settings, clock, extraction(
        claim("Production takes six weeks.", "production takes six weeks")))
    captured = app.knowledge.capture(SUPPLIER_NOTE)

    report = app.knowledge.compile_source(captured.path)

    row = app.knowledge.ledger.find(captured.path)
    assert row.status == "compiled"
    assert row.compiled == "2026-09-05"
    assert row.pages_produced == report.pages[0]


def test_a_claim_whose_quote_is_not_in_the_source_is_dropped(vault_settings,
                                                             clock):
    """A model that paraphrases its own evidence writes an unsourced page.

    The span is not taken on the model's word — the quote is located in the
    source's actual bytes, and a claim that cannot be located is named and
    discarded rather than written with a citation nobody verified.
    """
    app = make_app(vault_settings, clock, extraction(
        claim("Production takes six weeks.", "production takes six weeks"),
        claim("The supplier offers a discount.",
              "a ten percent discount applies")))
    captured = app.knowledge.capture(SUPPLIER_NOTE)

    report = app.knowledge.compile_source(captured.path)

    assert report.claims_written == 1
    assert len(report.claims_rejected) == 1
    assert "not in the source" in report.claims_rejected[0]
    body = app.knowledge.gateway.read_text(report.pages[0])
    assert "discount" not in body


def test_a_source_with_no_verifiable_claim_writes_nothing(vault_settings, clock):
    app = make_app(vault_settings, clock, extraction(
        claim("Entirely invented.", "no such words appear here")))
    captured = app.knowledge.capture(SUPPLIER_NOTE)

    report = app.knowledge.compile_source(captured.path)

    assert report.outcome is CompileOutcome.NEEDS_CONTEXT
    assert report.pages == []
    # The capture is kept and stays pending: nothing was lost, and nothing
    # was invented to make the run look successful.
    assert app.knowledge.ledger.find(captured.path).status == "pending"


def test_without_a_model_the_capture_is_kept_and_the_gap_is_stated(
        vault_settings, clock):
    app = make_app(vault_settings, clock)          # no model configured at all
    captured = app.knowledge.capture(SUPPLIER_NOTE)

    report = app.knowledge.compile_source(captured.path)

    assert report.outcome is CompileOutcome.DEFERRED
    assert "No model is configured" in report.reason
    assert app.knowledge.ledger.find(captured.path).status == "pending"


def test_a_bare_link_is_recorded_as_unfetched_not_invented(vault_settings, clock):
    """Single-source ingest is not authorised to fetch (V14).

    Captured first so the row is genuinely `pending`: the fixture's own
    bare-link source is already `unfetched`, and compiling that would exercise
    the "not pending" branch while looking like it tested this one.
    """
    app = make_app(vault_settings, clock, extraction(
        claim("Should never be written.", "https://example.invalid/acoustics")))
    captured = app.knowledge.capture("https://example.invalid/acoustics",
                                     title="Acoustic panels clip")

    report = app.knowledge.compile_source(captured.path)

    assert report.outcome is CompileOutcome.UNFETCHED
    assert report.pages == []
    assert "No page content was invented" in report.reason
    assert app.knowledge.ledger.find(captured.path).status == "unfetched"


def test_a_source_whose_row_is_not_pending_is_left_alone(vault_settings, clock):
    app = make_app(vault_settings, clock)

    report = app.knowledge.compile_source(
        "0-raw/clips/2026-09-04-acoustic-panels.md")

    assert report.outcome is CompileOutcome.DEFERRED
    assert "not pending" in report.reason


def test_an_unregistered_source_cannot_be_compiled(vault_settings, clock):
    app = make_app(vault_settings, clock)

    with pytest.raises(InvalidInput):
        app.knowledge.compile_source("0-raw/inbox/never-registered.md")


def test_a_second_compile_of_the_same_source_does_not_append_again(
        vault_settings, clock):
    app = make_app(vault_settings, clock, extraction(
        claim("Production takes six weeks.", "production takes six weeks")))
    captured = app.knowledge.capture(SUPPLIER_NOTE)
    first = app.knowledge.compile_source(captured.path)
    before = app.knowledge.gateway.read_text(first.pages[0])

    second = app.knowledge.compile_source(captured.path)

    assert second.outcome is CompileOutcome.DEFERRED
    assert second.pages == []
    assert app.knowledge.gateway.read_text(first.pages[0]) == before


def test_contradicting_claims_keep_both_sides_and_mark_the_page_contested(
        vault_settings, clock):
    body = ("Lead time is six weeks per the supplier.\n"
            "Lead time is ten weeks per the shipping desk.\n")
    app = make_app(vault_settings, clock, extraction(
        claim("Lead time is six weeks.", "six weeks", subject="lead time",
              attributed_to="the supplier"),
        claim("Lead time is ten weeks.", "ten weeks", subject="lead time",
              attributed_to="the shipping desk")))
    captured = app.knowledge.capture(body, title="Lead time reports")

    report = app.knowledge.compile_source(captured.path)

    assert report.contradictions == 1
    page = parse_page(app.knowledge.gateway.read_text(report.pages[0]))
    assert page["frontmatter"]["confidence"] == "contested"
    assert "six weeks" in page["body"] and "ten weeks" in page["body"]


# --------------------------------------------------------------------------- #
# Answer: cited, and honest about what kind of source it found
# --------------------------------------------------------------------------- #
def test_an_answer_cites_the_wiki_page_and_its_underlying_source(vault_settings,
                                                                 clock):
    app = make_app(vault_settings, clock, extraction(
        claim("Production takes six weeks.", "production takes six weeks")))
    captured = app.knowledge.capture(SUPPLIER_NOTE)
    report = app.knowledge.compile_source(captured.path)

    answer = app.knowledge.answer("how long does production take")

    assert not answer.knowledge_gap
    assert report.pages[0] in answer.cited_paths
    citation = next(c for c in answer.citations if c.path == report.pages[0])
    assert citation.layer == "wiki"
    assert citation.sources == [captured.path]
    # The source chain travels with the answer, not just the page name.
    assert captured.path in answer.text


def test_an_uncompiled_capture_is_labelled_as_such(vault_settings, clock):
    """A raw fallback must not read like settled knowledge (vault §7)."""
    app = make_app(vault_settings, clock)
    app.knowledge.capture("Anodised aluminium brackets arrive on Thursday.")

    answer = app.knowledge.answer("anodised brackets")

    assert not answer.knowledge_gap
    assert "uncompiled" in answer.text.lower()
    assert all(citation.uncompiled for citation in answer.citations)
    assert all(citation.layer == "raw" for citation in answer.citations)


def test_nothing_in_the_vault_is_a_knowledge_gap_not_an_invented_answer(
        vault_settings, clock):
    app = make_app(vault_settings, clock)

    answer = app.knowledge.answer("quarterly revenue in Patagonia")

    assert answer.knowledge_gap
    assert answer.citations == []
    assert "nothing in the vault" in answer.text.lower()


def test_the_vault_search_port_returns_a_cited_answer(vault_settings, clock):
    """The trusted port packs declare returns the same cited answer."""
    app = make_app(vault_settings, clock, extraction(
        claim("Production takes six weeks.", "production takes six weeks")))
    captured = app.knowledge.capture(SUPPLIER_NOTE)
    app.knowledge.compile_source(captured.path)

    handler = app.invoker.handlers["vault.search"]
    from loop.ai.budget import RootBudget
    from loop.core.privacy import PrivacyLabel
    from loop.runtime.authority import AuthorityContext
    context = AuthorityContext(owner="owner", root_id="r", privacy=PrivacyLabel(),
                               budget=RootBudget())

    output = handler.handler({"query": "production six weeks"}, context)

    assert output["knowledge_gap"] is False
    assert output["sources"] == ["1-wiki/concepts/supplier-lead-times.md"]


# --------------------------------------------------------------------------- #
# Indexing an existing vault
# --------------------------------------------------------------------------- #
def test_reindexing_makes_a_pre_existing_vault_answerable(vault_settings, clock):
    """A vault that existed before Loop did is not an empty vault."""
    app = make_app(vault_settings, clock, index=False)
    assert app.knowledge.answer("lead time").knowledge_gap

    indexed = app.knowledge.reindex()

    assert indexed > 0
    answer = app.knowledge.answer("time between order and delivery")
    assert not answer.knowledge_gap
    assert "1-wiki/concepts/lead-time.md" in answer.cited_paths


def test_reindexing_excludes_rules_personal_state_and_the_archive(vault_settings,
                                                                  clock):
    """Policy, relationship state and retired material are not answers."""
    app = make_app(vault_settings, clock)

    paths = app.vault_search.indexed_paths()

    assert not any(path.startswith(("_ctx/", "_mem/", "_archive/"))
                   for path in paths), sorted(paths)
    # The ledger is a register of captures, not one of them: an answer citing
    # a ledger row instead of the note it points at is worse than no answer.
    assert "0-raw/_ledger.md" not in paths
    assert "1-wiki/concepts/lead-time.md" in paths


def test_reindexing_forgets_a_file_that_was_deleted(vault_settings, clock, vault):
    """A removed note must stop being citable, not stay searchable forever."""
    app = make_app(vault_settings, clock)
    assert "1-wiki/concepts/lead-time.md" in app.vault_search.indexed_paths()

    (vault.root / "1-wiki/concepts/lead-time.md").unlink()
    app.knowledge.reindex()

    assert "1-wiki/concepts/lead-time.md" not in app.vault_search.indexed_paths()


# --------------------------------------------------------------------------- #
# Ownership and privacy
# --------------------------------------------------------------------------- #
def test_a_human_owned_page_is_appended_to_never_rewritten(vault_settings, clock,
                                                           vault):
    """The owner's own words are not raw material for a model rewrite (rule 5)."""
    human_page = vault.root / "1-wiki/concepts/supplier-lead-times.md"
    human_page.write_text(
        "---\ntype: concept\ntitle: Supplier Lead Times\nowner: human\n"
        "confidence: stated\nsources:\n  - manual\n---\n\n"
        "My own notes on this. Do not rewrite.\n", encoding="utf-8")
    app = make_app(vault_settings, clock, extraction(
        claim("Production takes six weeks.", "production takes six weeks")))
    captured = app.knowledge.capture(SUPPLIER_NOTE)

    report = app.knowledge.compile_source(captured.path)

    body = app.knowledge.gateway.read_text(report.pages[0])
    assert "My own notes on this. Do not rewrite." in body
    assert "Compiler notes" in body
    assert "six weeks" in body


def test_local_only_content_with_no_local_model_reaches_no_cloud_provider(
        vault_settings, clock):
    """The answer is "compile cannot run", never "send it somewhere else"."""
    cloud_only = vault_settings.model_copy(update={
        "cloud_enabled": True, "anthropic_api_key": "test-key",
        "anthropic_model": "claude-sonnet-5", "cloud_daily_budget_usd": 5.0})
    app = build_application(cloud_only, clock=clock)   # no local model at all
    app.knowledge.reindex()
    captured = app.knowledge.capture(SUPPLIER_NOTE)

    report = app.knowledge.compile_source(captured.path)

    assert report.outcome is CompileOutcome.DEFERRED
    assert app.model_gateway.audit.cloud_calls == 0
    assert app.knowledge.ledger.find(captured.path).status == "pending"


def test_an_unregistered_orphan_file_is_not_indexed(vault_settings, clock, vault):
    """A crash between the file write and the ledger leaves exactly this.

    Until reconciliation registers it, the file is not knowledge Loop has
    accepted — and an indexed row would let it be retrieved and cited as
    though it were.
    """
    from datetime import date

    from loop.vault.capture import CaptureRequest, build_document

    request = CaptureRequest(body="Orphaned by a crash.", title="Orphan note")
    document = build_document(request, added=date(2026, 9, 5))
    orphan = "0-raw/inbox/2026-09-05-orphan-note.md"
    (vault.root / orphan).write_text(document, encoding="utf-8")

    app = make_app(vault_settings, clock, index=False)
    result = app.knowledge.capture("Orphaned by a crash.", title="Orphan note")

    assert result.status == "queued"
    assert result.registered is False
    assert orphan not in app.vault_search.indexed_paths()


def test_reindexing_leaves_rows_outside_its_layers_alone(vault_settings, clock):
    """Reindex owns the layers it walks, and only those.

    Its stale-row sweep is scoped for the same reason it does not walk `_ctx`
    and `_mem`: a future caller indexing something outside those layers must
    not have its rows quietly deleted by an unrelated vault reindex.
    """
    from loop.vault.search import IndexEntry

    app = make_app(vault_settings, clock)
    app.vault_search.index(IndexEntry(path="_mem/goals.md", title="Goals",
                                      body="Ship Loop."))

    app.knowledge.reindex()

    assert "_mem/goals.md" in app.vault_search.indexed_paths()


# --------------------------------------------------------------------------- #
# A configured model that cannot do the job (found by a live run, not by pytest)
# --------------------------------------------------------------------------- #
def test_a_model_that_never_returns_valid_json_defers_one_source(vault_settings,
                                                                 clock):
    """Configured is not the same as capable.

    A real local model answering with prose instead of the schema is the
    ordinary case on a small model, and it must not be mistaken for "this
    source has been processed".
    """
    app = make_app(vault_settings, clock, "not json at all", "still not json",
                   "nor this", "or this")
    captured = app.knowledge.capture(SUPPLIER_NOTE)

    report = app.knowledge.compile_source(captured.path)

    assert report.outcome is CompileOutcome.DEFERRED
    assert "Claim extraction did not succeed" in report.reason
    assert report.pages == []
    assert app.knowledge.ledger.find(captured.path).status == "pending"


def test_one_unextractable_source_does_not_abort_the_backlog(vault_settings,
                                                             clock):
    """A batch that stops at the first bad source silently skips the rest.

    The fixture vault already has one pending source of its own, and it is
    first in the ledger — so the four unusable responses are spent on it, and
    the capture made here is only reached at all if the batch kept going.
    """
    good = extraction(claim("Production takes six weeks.",
                            "production takes six weeks"))
    app = make_app(vault_settings, clock,
                   "not json", "not json", "not json", "not json",
                   json.dumps(good))
    captured = app.knowledge.capture(SUPPLIER_NOTE)

    reports = app.knowledge.compile_pending()

    by_source = {report.source_path: report for report in reports}
    assert len(by_source) == 2, "the batch stopped early"
    stalled = by_source["0-raw/inbox/2026-09-05-supplier-lead-time.md"]
    assert stalled.outcome is CompileOutcome.DEFERRED
    assert "Claim extraction did not succeed" in stalled.reason
    assert by_source[captured.path].outcome is CompileOutcome.COMPILED
    assert by_source[captured.path].pages
