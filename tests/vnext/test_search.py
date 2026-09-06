"""Lexical retrieval and meeting notes (vault §1, §6).

Covers V25 and V31.
"""

from __future__ import annotations

import pytest

from loop.core.privacy import ModelScope, PrivacyLabel
from loop.vault.search import (
    LAYER_ARCHIVE,
    LAYER_RAW,
    LAYER_WIKI,
    IndexEntry,
    MeetingNote,
    VaultSearch,
    layer_for,
    reusable_facts,
)

PRIVATE = PrivacyLabel(model_scope=ModelScope.LOCAL_ONLY, sensitive=True)
PUBLIC = PrivacyLabel(model_scope=ModelScope.CLOUD_ALLOWED)


@pytest.fixture
def search(sessions) -> VaultSearch:
    index = VaultSearch(sessions=sessions)
    index.index_many([
        IndexEntry("1-wiki/concepts/lead-time.md", "Lead time",
                   "Time between order and delivery for a supplier.", PUBLIC),
        IndexEntry("1-wiki/topics/acoustics.md", "Acoustics",
                   "Panel thickness affects acoustic absorption.", PUBLIC),
        IndexEntry("0-raw/inbox/note.md", "Supplier note",
                   "The supplier said production takes six weeks.", PUBLIC),
        IndexEntry("_archive/old.md", "Retired",
                   "An archived note about supplier delivery.", PUBLIC),
        IndexEntry("4-journal/daily/2026/2026-09-05.md", "Daily",
                   "Private reflection about the supplier meeting.", PRIVATE),
    ])
    return index


# --------------------------------------------------------------------------- #
# Layers
# --------------------------------------------------------------------------- #
def test_paths_are_classified_by_layer():
    assert layer_for("0-raw/inbox/a.md") == LAYER_RAW
    assert layer_for("1-wiki/concepts/a.md") == LAYER_WIKI
    assert layer_for("_archive/a.md") == LAYER_ARCHIVE


# --------------------------------------------------------------------------- #
# V25 — lexical search without embeddings
# --------------------------------------------------------------------------- #
def test_v25_search_works_with_no_embedding_model(search):
    """Nothing here touches Ollama or an embedding model."""
    hits = search.search("supplier")
    assert hits


def test_v25_results_carry_their_source_layer(search):
    hits = search.search("supplier")
    assert {hit.layer for hit in hits} <= {"raw", "wiki", "journal"}


def test_v25_a_snippet_is_returned_for_citation(search):
    hits = search.search("acoustic")
    assert any("acoustic" in hit.snippet.lower() for hit in hits)


def test_v25_title_search_finds_a_named_page(search):
    hits = search.title_search("Acoustics")
    assert hits[0].path == "1-wiki/topics/acoustics.md"


def test_v25_the_archive_is_excluded_by_default(search):
    paths = {hit.path for hit in search.search("supplier")}
    assert "_archive/old.md" not in paths


def test_v25_the_archive_can_be_searched_explicitly(search):
    paths = {hit.path for hit in
             search.search("supplier", include_archive=True)}
    assert "_archive/old.md" in paths


def test_v25_private_rows_are_excluded_when_not_allowed(search):
    """Filtered in SQL, so private content is never loaded at all."""
    hits = search.search("supplier", allow_local_only=False)
    assert all(hit.local_only is False for hit in hits)
    assert "4-journal/daily/2026/2026-09-05.md" not in {h.path for h in hits}


def test_v25_private_rows_are_available_for_local_use(search):
    paths = {hit.path for hit in search.search("supplier",
                                               allow_local_only=True)}
    assert "4-journal/daily/2026/2026-09-05.md" in paths


def test_v25_layer_filtering_narrows_results(search):
    hits = search.search("supplier", layers=[LAYER_WIKI])
    assert all(hit.layer == LAYER_WIKI for hit in hits)


def test_v25_punctuation_does_not_break_the_query(search):
    """FTS5 treats punctuation as syntax; a raw query would be an error."""
    assert search.search("supplier's \"delivery\" -time") is not None


def test_v25_an_empty_query_returns_nothing(search):
    assert search.search("   ") == []
    assert search.search("!!!") == []


def test_v25_reindexing_replaces_rather_than_duplicates(search):
    before = search.count()
    search.index(IndexEntry("1-wiki/concepts/lead-time.md", "Lead time",
                            "Updated body.", PUBLIC))

    assert search.count() == before
    assert "Updated" in search.search("Updated")[0].snippet


def test_v25_removing_a_document_makes_it_unfindable(search):
    search.remove("1-wiki/topics/acoustics.md")
    paths = {hit.path for hit in search.search("acoustic")}
    assert "1-wiki/topics/acoustics.md" not in paths


# --------------------------------------------------------------------------- #
# V31 — meeting notes
# --------------------------------------------------------------------------- #
def test_v31_a_meeting_note_uses_the_journal_path():
    note = MeetingNote(date_iso="2026-09-06", title="Supplier review")
    assert note.path == "4-journal/meetings/2026/2026-09-06-supplier-review.md"


def test_v31_a_meeting_note_is_not_written_to_the_wiki():
    note = MeetingNote(date_iso="2026-09-06", title="Supplier review")
    assert not note.path.startswith("1-wiki/")


def test_v31_attendees_are_recorded_in_frontmatter():
    import yaml

    note = MeetingNote(date_iso="2026-09-06", title="Supplier review",
                       attendees=["Sam Rivers", "Anna Fischer"])
    frontmatter = yaml.safe_load(note.to_markdown().split("---")[1])

    assert frontmatter["attendees"] == ["Sam Rivers", "Anna Fischer"]
    assert frontmatter["type"] == "meeting"


def test_v31_actions_are_listed_separately():
    note = MeetingNote(date_iso="2026-09-06", title="Review",
                       actions=["Anna sends the revised quote"])
    assert "## Actions" in note.to_markdown()


def test_v31_reusable_facts_are_extracted_for_separate_capture():
    """Durable facts are captured as their own sources before any wiki work."""
    note = MeetingNote(date_iso="2026-09-06", title="Review",
                       body="- Production takes six weeks\n"
                            "- We talked about scheduling\n")
    facts = reusable_facts(note)

    assert any("six weeks" in fact for fact in facts)


def test_v31_meeting_chatter_is_not_treated_as_a_reusable_fact():
    note = MeetingNote(date_iso="2026-09-06", title="Review",
                       body="- Good meeting\n- Follow up next week\n")
    assert reusable_facts(note) == []
