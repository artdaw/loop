"""Read receipts and evidence validation (vault §6).

Covers V06, V07, V21, V23, V24, V29.
"""

from __future__ import annotations

import pytest

from loop.core.errors import ValidationFailed
from loop.core.ids import content_hash
from loop.vault.receipts import (
    ByteRange,
    Evidence,
    ProvenanceMap,
    ReceiptStore,
    merge_ranges,
    require_compiled_sources,
)

SHORT = b"The supplier said production takes six weeks.\n"
LONG = b"".join(f"line {i:04d} of a long source document\n".encode()
                for i in range(500))


@pytest.fixture
def store() -> ReceiptStore:
    return ReceiptStore(run_id="run-1")


def _evidence(body: bytes, span: ByteRange, source_id: str = "s1") -> Evidence:
    return Evidence(source_id=source_id, path="0-raw/inbox/x.md", span=span,
                    content_hash=content_hash(body))


# --------------------------------------------------------------------------- #
# Ranges
# --------------------------------------------------------------------------- #
def test_overlapping_ranges_merge():
    merged = merge_ranges([ByteRange(0, 10), ByteRange(5, 20)])
    assert merged == [ByteRange(0, 20)]


def test_adjacent_ranges_merge():
    assert merge_ranges([ByteRange(0, 10), ByteRange(10, 20)]) == [ByteRange(0, 20)]


def test_disjoint_ranges_stay_separate():
    merged = merge_ranges([ByteRange(0, 10), ByteRange(50, 60)])
    assert len(merged) == 2


def test_an_invalid_range_is_rejected():
    with pytest.raises(ValueError):
        ByteRange(10, 5)


# --------------------------------------------------------------------------- #
# V06 — evidence must be backed by a receipt
# --------------------------------------------------------------------------- #
def test_v06_evidence_without_any_receipt_fails(store):
    with pytest.raises(ValidationFailed) as excinfo:
        store.validate([_evidence(SHORT, ByteRange(0, 10))])

    assert "no read receipt" in str(excinfo.value.details["problems"][0])


def test_v06_evidence_with_a_receipt_passes(store):
    store.read_whole(source_id="s1", path="0-raw/inbox/x.md", body=SHORT)
    store.validate([_evidence(SHORT, ByteRange(0, 20))])


def test_v06_a_changed_source_hash_fails_validation(store):
    store.read_whole(source_id="s1", path="0-raw/inbox/x.md", body=SHORT)

    stale = Evidence(source_id="s1", path="0-raw/inbox/x.md",
                     span=ByteRange(0, 10),
                     content_hash=content_hash(b"different content"))

    with pytest.raises(ValidationFailed) as excinfo:
        store.validate([stale])
    assert "changed since it was read" in str(excinfo.value.details["problems"])


def test_v06_validation_happens_before_any_mutation(store):
    """The exception is the point: nothing downstream runs."""
    mutated = []

    def compile_and_write():
        store.validate([_evidence(SHORT, ByteRange(0, 10))])
        mutated.append("wiki write")

    with pytest.raises(ValidationFailed):
        compile_and_write()
    assert mutated == []


def test_v06_a_receipt_from_another_run_does_not_count(store):
    """Compile rule 2: the source must be read *in this run*."""
    other = ReceiptStore(run_id="run-0")
    other.read_whole(source_id="s1", path="0-raw/inbox/x.md", body=SHORT)

    with pytest.raises(ValidationFailed):
        store.validate([_evidence(SHORT, ByteRange(0, 10))])


def test_v06_all_problems_are_reported_not_just_the_first(store):
    with pytest.raises(ValidationFailed) as excinfo:
        store.validate([_evidence(SHORT, ByteRange(0, 5), source_id="a"),
                        _evidence(SHORT, ByteRange(0, 5), source_id="b")])

    assert len(excinfo.value.details["problems"]) == 2


# --------------------------------------------------------------------------- #
# V07 — chunked reading and coverage
# --------------------------------------------------------------------------- #
def test_v07_a_long_source_read_in_chunks_is_fully_covered(store):
    receipt = store.read_in_chunks(source_id="s1", path="0-raw/notes/long.md",
                                   body=LONG, chunk_bytes=1024)

    assert receipt.is_complete
    assert receipt.covered_bytes == len(LONG)


def test_v07_chunk_coverage_is_recorded_as_ranges(store):
    receipt = store.read_in_chunks(source_id="s1", path="p", body=LONG,
                                   chunk_bytes=1024)
    # Overlapping chunks coalesce into one continuous range.
    assert receipt.covered == [ByteRange(0, len(LONG))]


def test_v07_a_claim_spanning_a_chunk_boundary_is_still_covered(store):
    store.read_in_chunks(source_id="s1", path="p", body=LONG, chunk_bytes=1024,
                         overlap=256)
    span = ByteRange(1000, 1100)     # straddles the first boundary

    store.validate([_evidence(LONG, span)])


def test_v07_a_preview_only_read_is_incomplete(store):
    receipt = store.read_preview(source_id="s1", path="p", body=LONG)

    assert receipt.is_complete is False
    assert receipt.covered_bytes < len(LONG)


def test_v07_a_claim_beyond_the_preview_fails_validation(store):
    store.read_preview(source_id="s1", path="p", body=LONG, preview_bytes=512)

    with pytest.raises(ValidationFailed) as excinfo:
        store.validate([_evidence(LONG, ByteRange(5000, 5100))])

    assert "were not read" in str(excinfo.value.details["problems"])


def test_v07_classification_is_refused_from_a_preview(store):
    """Deciding what a source *is* from its opening bytes is the failure mode."""
    store.read_preview(source_id="s1", path="p", body=LONG)
    assert store.classification_allowed("s1") is False


def test_v07_classification_is_allowed_after_a_complete_read(store):
    store.read_in_chunks(source_id="s1", path="p", body=LONG)
    assert store.classification_allowed("s1") is True


def test_v07_require_complete_rejects_a_partial_read(store):
    store.read_preview(source_id="s1", path="p", body=LONG, preview_bytes=512)

    with pytest.raises(ValidationFailed):
        store.validate([_evidence(LONG, ByteRange(0, 100))],
                       require_complete=True)


def test_v07_reading_more_extends_coverage(store):
    store.read_preview(source_id="s1", path="p", body=LONG, preview_bytes=512)
    receipt = store.read_in_chunks(source_id="s1", path="p", body=LONG)

    assert receipt.is_complete


def test_a_source_changing_mid_run_resets_its_coverage(store):
    store.read_whole(source_id="s1", path="p", body=SHORT)
    receipt = store.read_whole(source_id="s1", path="p", body=b"new content\n")

    assert receipt.content_hash == content_hash(b"new content\n")
    assert receipt.covered_bytes == len(b"new content\n")


# --------------------------------------------------------------------------- #
# V21 — archived sources keep their identity
# --------------------------------------------------------------------------- #
def test_v21_an_archived_source_resolves_to_its_new_location():
    provenance = ProvenanceMap()
    provenance.archive("0-raw/notes/2024-old.md", "_archive/2024-old.md")

    assert provenance.resolve("0-raw/notes/2024-old.md") == "_archive/2024-old.md"


def test_v21_an_unarchived_source_resolves_to_itself():
    assert ProvenanceMap().resolve("0-raw/inbox/x.md") == "0-raw/inbox/x.md"


def test_v21_the_original_identity_is_preserved_not_rewritten():
    """An old wiki page still cites the original path; that trail is the point."""
    provenance = ProvenanceMap()
    provenance.archive("0-raw/notes/2024-old.md", "_archive/2024-old.md")

    assert provenance.is_archived("0-raw/notes/2024-old.md") is True
    assert "0-raw/notes/2024-old.md" in provenance.moved


def test_v21_evidence_against_an_archived_source_still_validates(store):
    provenance = ProvenanceMap()
    provenance.archive("0-raw/notes/2024-old.md", "_archive/2024-old.md")
    current = provenance.resolve("0-raw/notes/2024-old.md")

    store.read_whole(source_id="0-raw/notes/2024-old.md", path=current,
                     body=SHORT)
    store.validate([Evidence(source_id="0-raw/notes/2024-old.md", path=current,
                             span=ByteRange(0, 20),
                             content_hash=content_hash(SHORT))])


# --------------------------------------------------------------------------- #
# V23 — output derives from compiled pages
# --------------------------------------------------------------------------- #
def test_v23_drafting_from_uncompiled_sources_is_refused():
    with pytest.raises(ValidationFailed) as excinfo:
        require_compiled_sources(["0-raw/inbox/new.md"], compiled=set())

    assert "0-raw/inbox/new.md" in excinfo.value.details["uncompiled"]


def test_v23_drafting_from_compiled_sources_is_allowed():
    require_compiled_sources(["0-raw/inbox/a.md"],
                             compiled={"0-raw/inbox/a.md"})


def test_v23_a_partially_compiled_set_names_only_the_gaps():
    with pytest.raises(ValidationFailed) as excinfo:
        require_compiled_sources(["a.md", "b.md"], compiled={"a.md"})

    assert excinfo.value.details["uncompiled"] == ["b.md"]


# --------------------------------------------------------------------------- #
# V24 — a model answer is not evidence
# --------------------------------------------------------------------------- #
def test_v24_saving_an_answer_resolves_to_the_sources_it_read(store):
    """The answer text is not itself a source; its cited spans are."""
    store.read_whole(source_id="s1", path="0-raw/clips/a.md", body=SHORT)
    cited = [_evidence(SHORT, ByteRange(0, 44))]

    store.validate(cited)

    assert {e.source_id for e in cited} == {"s1"}
    assert "s1" in store.sources_read()


def test_v24_an_answer_citing_nothing_read_cannot_be_saved(store):
    """A fluent answer with no receipt is exactly what must be refused."""
    fabricated = Evidence(source_id="model-answer", path="(model output)",
                          span=ByteRange(0, 100),
                          content_hash=content_hash(b"the model said so"))

    with pytest.raises(ValidationFailed):
        store.validate([fabricated])


# --------------------------------------------------------------------------- #
# V29 — a different backend cannot bypass the rule
# --------------------------------------------------------------------------- #
def test_v29_file_existence_alone_is_not_a_receipt(store, tmp_path):
    """The Scriptorium adapter faces the same test as the builtin one."""
    source = tmp_path / "exists.md"
    source.write_text("real content on disk\n", encoding="utf-8")

    # The file demonstrably exists, and that proves nothing about reading it.
    with pytest.raises(ValidationFailed):
        store.validate([Evidence(source_id="s1", path=str(source),
                                 span=ByteRange(0, 10),
                                 content_hash=content_hash(
                                     source.read_bytes()))])


def test_v29_an_adapter_must_produce_a_receipt_to_pass(store, tmp_path):
    source = tmp_path / "exists.md"
    body = b"real content on disk\n"
    source.write_bytes(body)

    store.read_whole(source_id="s1", path=str(source), body=body)
    store.validate([Evidence(source_id="s1", path=str(source),
                             span=ByteRange(0, 10),
                             content_hash=content_hash(body))])


def test_v29_a_refusal_cannot_be_bypassed_by_reporting_success(store):
    """An adapter claiming it read something does not create a receipt."""
    claimed_but_unrecorded = _evidence(SHORT, ByteRange(0, 10))

    with pytest.raises(ValidationFailed):
        store.validate([claimed_but_unrecorded])
