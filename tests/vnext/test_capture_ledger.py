"""Exact capture and the six-column ledger (vault §5).

Covers V02, V03, V04, V05, V19, V20.
"""

from __future__ import annotations

import unicodedata
from datetime import date

import pytest

from loop.core.errors import Conflict, InvalidInput
from loop.vault.capture import (
    CaptureRequest,
    CaptureService,
    build_document,
    derive_title,
)
from loop.vault.gateway import SimulatedCrash, VaultGateway, WriteMode
from loop.vault.ledger import (
    Ledger,
    LedgerRow,
    escape_cell,
    normalise_identity,
    parse,
    render,
)
from tests.vnext.vault_fixtures import add_nfd_filename, build_minimal_vault

TODAY = date(2026, 9, 6)


@pytest.fixture
def vault(tmp_path):
    return build_minimal_vault(tmp_path / "vault")


@pytest.fixture
def gateway(vault, sessions, clock) -> VaultGateway:
    return VaultGateway(root=vault.root, sessions=sessions, clock=clock)


@pytest.fixture
def ledger(gateway) -> Ledger:
    return Ledger(gateway)


@pytest.fixture
def capture(gateway, ledger) -> CaptureService:
    return CaptureService(gateway=gateway, ledger=ledger)


def write_ledger(gateway: VaultGateway, ledger: Ledger,
                 rows: list[LedgerRow]) -> None:
    """Persist ledger rows through the gateway, as production does."""
    gateway.apply(gateway.begin([gateway.make_operation(
        ledger.relative_path, render(rows), mode=WriteMode.REPLACE,
        expected_hash=gateway.hash_of(ledger.relative_path))]))


# --------------------------------------------------------------------------- #
# Ledger cell safety (V02)
# --------------------------------------------------------------------------- #
def test_v02_a_pipe_in_a_cell_is_escaped():
    assert escape_cell("a | b") == r"a \| b"


def test_v02_newlines_cannot_break_a_row():
    assert "\n" not in escape_cell("line one\nline two")


def test_v02_an_escaped_pipe_round_trips_through_parsing():
    row = LedgerRow(source="0-raw/inbox/x.md", batch="inbox", added="2026-09-06",
                    pages_produced="a | b")
    parsed = parse(render([row]))

    assert len(parsed) == 1
    assert parsed[0].pages_produced == "a | b"


def test_v02_a_pipe_in_a_body_does_not_corrupt_neighbouring_rows(ledger, capture):
    capture.capture(CaptureRequest(body="six weeks | confirmed by email\n"),
                    today=TODAY)
    capture.capture(CaptureRequest(body="a second unrelated note\n"),
                    today=TODAY)

    rows = ledger.rows()
    assert len(rows) == 4          # 2 fixture rows + 2 new
    assert all(len(r.source.split()) >= 1 for r in rows)
    assert all(r.status in ("pending", "unfetched") for r in rows)


def test_v02_the_ledger_keeps_exactly_six_columns(ledger, capture, gateway):
    capture.capture(CaptureRequest(body="body with | pipe\n"), today=TODAY)

    lines = [ln for ln in gateway.read_text(ledger.relative_path).splitlines()
             if ln.strip().startswith("|")]
    for line in lines[2:]:
        # Count only unescaped separators.
        assert line.replace(r"\|", "").count("|") == 7   # 6 cells → 7 pipes


# --------------------------------------------------------------------------- #
# Exact preservation (V02)
# --------------------------------------------------------------------------- #
def test_v02_trailing_whitespace_is_preserved_exactly(capture, gateway):
    body = "The supplier said six weeks   \n"
    result = capture.capture(CaptureRequest(body=body), today=TODAY)

    stored = gateway.read_text(result.path)
    assert stored.endswith(body)


def test_v02_a_pipe_in_the_body_is_preserved(capture, gateway):
    body = "production takes six weeks | confirmed\n"
    result = capture.capture(CaptureRequest(body=body), today=TODAY)

    assert "six weeks | confirmed" in gateway.read_text(result.path)


def test_v02_the_frontmatter_is_valid_yaml(capture, gateway):
    import yaml

    result = capture.capture(CaptureRequest(body="a fact\n", title="Odd: title"),
                             today=TODAY)
    document = gateway.read_text(result.path)
    frontmatter = document.split("---\n")[1]

    parsed = yaml.safe_load(frontmatter)
    assert parsed["type"] == "source"
    assert parsed["title"] == "Odd: title"


def test_v02_one_ledger_row_is_created(capture, ledger):
    result = capture.capture(CaptureRequest(body="a fact\n"), today=TODAY)

    row = ledger.find(result.path)
    assert row is not None
    assert row.status == "pending"
    assert row.added == "2026-09-06"


def test_a_title_containing_a_pipe_is_rejected():
    with pytest.raises(InvalidInput):
        build_document(CaptureRequest(body="x", title="bad | title"),
                       added=TODAY)


def test_a_missing_title_is_derived_not_required():
    assert derive_title("the supplier said six weeks") == \
        "the supplier said six weeks"


def test_an_empty_capture_is_refused(capture):
    with pytest.raises(InvalidInput):
        capture.capture(CaptureRequest(body="   \n"), today=TODAY)


# --------------------------------------------------------------------------- #
# V03 — same title, same day
# --------------------------------------------------------------------------- #
def test_v03_two_notes_with_the_same_title_get_distinct_paths(capture):
    first = capture.capture(CaptureRequest(body="first body\n",
                                           title="Supplier lead time"),
                            today=TODAY)
    second = capture.capture(CaptureRequest(body="second body\n",
                                            title="Supplier lead time"),
                             today=TODAY)

    assert first.path != second.path


def test_v03_neither_note_is_overwritten(capture, gateway):
    first = capture.capture(CaptureRequest(body="first body\n",
                                           title="Supplier lead time"),
                            today=TODAY)
    second = capture.capture(CaptureRequest(body="second body\n",
                                            title="Supplier lead time"),
                             today=TODAY)

    assert "first body" in gateway.read_text(first.path)
    assert "second body" in gateway.read_text(second.path)


def test_v03_both_notes_are_registered(capture, ledger):
    capture.capture(CaptureRequest(body="a\n", title="Same"), today=TODAY)
    capture.capture(CaptureRequest(body="b\n", title="Same"), today=TODAY)

    sources = {row.source for row in ledger.rows()}
    assert len([s for s in sources if "same" in s]) == 2


def test_a_retry_reuses_the_same_reserved_path(capture):
    """A replayed capture must not create a sibling file."""
    request = CaptureRequest(body="the same body\n", title="Repeat")
    first = capture.capture(request, today=TODAY)
    second = capture.capture(request, today=TODAY)

    assert second.path == first.path
    assert second.status == "duplicate"


# --------------------------------------------------------------------------- #
# V04 / V05 — crash boundaries
# --------------------------------------------------------------------------- #
def test_v04_a_crash_before_the_ledger_leaves_an_orphan_file(capture, gateway,
                                                             ledger):
    request = CaptureRequest(body="orphaned note\n", title="Orphan")
    from loop.vault.capture import build_document
    document = build_document(request, added=TODAY)
    document_path = capture.reserve_path(request, today=TODAY)
    operation = gateway.make_operation(document_path, document)
    journal = gateway.begin([operation])
    with pytest.raises(SimulatedCrash):
        gateway.apply(journal, crash_after=1)

    assert gateway.exists(document_path)
    assert ledger.find(document_path) is None


def test_v04_reconciliation_registers_the_orphan_without_a_second_file(
        capture, gateway, ledger):
    request = CaptureRequest(body="orphaned note\n", title="Orphan")
    from loop.vault.capture import build_document
    path = capture.reserve_path(request, today=TODAY)
    gateway.apply(gateway.begin([
        gateway.make_operation(path, build_document(request, added=TODAY))]))

    registered = capture.reconcile(today=TODAY)

    assert path in registered
    assert ledger.find(path) is not None
    inbox = gateway.resolve("0-raw/inbox")
    assert len([p for p in inbox.iterdir() if "orphan" in p.name.lower()]) == 1


def test_v04_reconciliation_is_idempotent(capture, gateway, ledger):
    request = CaptureRequest(body="orphaned note\n", title="Orphan")
    from loop.vault.capture import build_document
    path = capture.reserve_path(request, today=TODAY)
    gateway.apply(gateway.begin([
        gateway.make_operation(path, build_document(request, added=TODAY))]))

    capture.reconcile(today=TODAY)
    second = capture.reconcile(today=TODAY)

    assert second == []
    assert len([r for r in ledger.rows() if r.source == path]) == 1


def test_v05_a_replay_returns_the_existing_saved_capture(capture):
    request = CaptureRequest(body="a fact\n", title="Fact")
    first = capture.capture(request, today=TODAY)
    replay = capture.capture(request, today=TODAY)

    assert replay.status == "duplicate"
    assert replay.path == first.path
    assert "Already saved" in replay.message


def test_the_message_distinguishes_saved_from_queued(capture):
    result = capture.capture(CaptureRequest(body="a fact\n"), today=TODAY)
    assert result.status == "saved"
    assert "Saved to your vault" in result.message


# --------------------------------------------------------------------------- #
# V19 / V20 — Unicode identity
# --------------------------------------------------------------------------- #
def test_v19_nfd_and_nfc_paths_share_one_identity():
    nfc = "1-wiki/entities/Café.md"
    nfd = unicodedata.normalize("NFD", nfc)

    assert normalise_identity(nfc) == normalise_identity(nfd)


def test_v19_a_decomposed_filename_is_not_a_false_orphan(vault, ledger, gateway):
    path, nfc_name = add_nfd_filename(vault)
    relative_nfd = f"1-wiki/entities/{path.name}"
    rows = ledger.rows()
    rows.append(LedgerRow(source=f"1-wiki/entities/{nfc_name}",
                          batch="wiki", added="2026-09-06"))
    write_ledger(gateway, ledger, rows)

    assert ledger.orphans([relative_nfd]) == []


def test_v19_the_filename_on_disk_is_not_renamed(vault):
    path, nfc_name = add_nfd_filename(vault)
    assert path.exists()
    assert path.name != nfc_name, "the decomposed name must stay as-is on disk"


def test_v20_a_normalisation_collision_is_reported_explicitly(ledger):
    nfc = "1-wiki/entities/Café.md"
    nfd = unicodedata.normalize("NFD", nfc)

    collisions = ledger.normalisation_collisions([nfc, nfd])

    assert len(collisions) == 1
    assert set(collisions[0]) == {nfc, nfd}


def test_v20_distinct_names_do_not_collide(ledger):
    assert ledger.normalisation_collisions(["a.md", "b.md"]) == []


# --------------------------------------------------------------------------- #
# Ledger updates
# --------------------------------------------------------------------------- #
def test_status_is_updated_by_exact_identity(ledger, gateway):
    rows = ledger.set_status("0-raw/inbox/2026-09-05-supplier-lead-time.md",
                             "compiled", compiled="2026-09-06",
                             pages_produced="1-wiki/concepts/lead-time.md")
    write_ledger(gateway, ledger, rows)

    row = ledger.find("0-raw/inbox/2026-09-05-supplier-lead-time.md")
    assert row.status == "compiled"
    # The unrelated row is untouched: no global string replacement.
    other = ledger.find("0-raw/clips/2026-09-04-acoustic-panels.md")
    assert other.status == "unfetched"


def test_an_unknown_status_is_rejected(ledger):
    with pytest.raises(InvalidInput):
        ledger.set_status("0-raw/inbox/2026-09-05-supplier-lead-time.md", "nope")


def test_updating_a_missing_row_is_an_error(ledger):
    with pytest.raises(InvalidInput):
        ledger.set_status("0-raw/inbox/absent.md", "compiled")


def test_registering_a_duplicate_identity_is_refused(ledger):
    with pytest.raises(Conflict):
        ledger.register(LedgerRow(
            source="0-raw/inbox/2026-09-05-supplier-lead-time.md",
            batch="inbox", added="2026-09-06"))


def test_the_fixture_ledger_parses_to_six_columns(vault):
    rows = vault.ledger_rows()
    assert all(len(cells) == 6 for cells in rows)
