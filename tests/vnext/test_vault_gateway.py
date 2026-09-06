"""Vault gateway: path confinement and journalled writes (vault §5, runtime §9).

Covers V04, V05, V13, V30.
"""

from __future__ import annotations

import os

import pytest

from loop.core.errors import Conflict, InvalidInput
from loop.core.ids import content_hash
from loop.vault.gateway import (
    FileOperation,
    PathRefused,
    SimulatedCrash,
    VaultGateway,
    WriteMode,
    normalise,
)
from tests.vnext.vault_fixtures import build_minimal_vault


@pytest.fixture
def vault(tmp_path):
    return build_minimal_vault(tmp_path / "vault")


@pytest.fixture
def gateway(vault, sessions, clock) -> VaultGateway:
    return VaultGateway(root=vault.root, sessions=sessions, clock=clock)


def _op(path: str, body: str, mode: WriteMode = WriteMode.CREATE_NEW,
        expected: str | None = None) -> FileOperation:
    return FileOperation(id=f"op-{path}", relative_path=path, body=body,
                         mode=mode, desired_hash=content_hash(body),
                         expected_hash=expected)


# --------------------------------------------------------------------------- #
# V30 — path confinement
# --------------------------------------------------------------------------- #
def test_v30_parent_traversal_is_refused(gateway):
    with pytest.raises(PathRefused):
        gateway.resolve("../outside.md")


def test_v30_nested_traversal_is_refused(gateway):
    with pytest.raises(PathRefused):
        gateway.resolve("0-raw/inbox/../../../etc/passwd")


def test_v30_absolute_paths_are_refused(gateway):
    with pytest.raises(PathRefused):
        gateway.resolve("/etc/passwd")


def test_v30_a_symlink_escaping_the_vault_is_refused(gateway, vault, tmp_path):
    """A link inside the vault pointing outside it is still an escape."""
    outside = tmp_path / "secret.txt"
    outside.write_text("not yours", encoding="utf-8")
    link = vault.root / "0-raw" / "escape.md"
    os.symlink(outside, link)

    with pytest.raises(PathRefused):
        gateway.resolve("0-raw/escape.md")


def test_v30_refusal_happens_before_any_read(gateway, vault, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("not yours", encoding="utf-8")
    os.symlink(outside, vault.root / "0-raw" / "escape.md")

    with pytest.raises(PathRefused):
        gateway.read_text("0-raw/escape.md")


def test_v30_an_empty_path_is_rejected(gateway):
    with pytest.raises(InvalidInput):
        gateway.resolve("   ")


def test_a_legitimate_path_resolves(gateway, vault):
    resolved = gateway.resolve("0-raw/inbox/2026-09-05-supplier-lead-time.md",
                               must_exist=True)
    assert resolved.is_relative_to(vault.root)


def test_reading_a_missing_file_is_an_input_error(gateway):
    with pytest.raises(InvalidInput):
        gateway.read_text("0-raw/inbox/nope.md")


# --------------------------------------------------------------------------- #
# Journalled writes
# --------------------------------------------------------------------------- #
def test_intent_is_journalled_before_any_bytes_are_written(gateway):
    operation = _op("0-raw/inbox/new.md", "body\n")
    journal = gateway.begin([operation])

    record = gateway.load_journal(journal.id)
    assert record["state"] == "prepared"
    assert record["manifest"]["operations"][0]["desired_hash"] == \
        operation.desired_hash
    assert not gateway.exists("0-raw/inbox/new.md")


def test_apply_writes_the_file_and_records_the_commit(gateway):
    journal = gateway.begin([_op("0-raw/inbox/new.md", "hello\n")])
    committed = gateway.apply(journal)

    assert gateway.read_text("0-raw/inbox/new.md") == "hello\n"
    assert committed[0]["hash"] == content_hash("hello\n")
    assert gateway.load_journal(journal.id)["state"] == "committed"


def test_no_staging_files_are_left_behind(gateway, vault):
    gateway.apply(gateway.begin([_op("0-raw/inbox/new.md", "hello\n")]))
    leftovers = list((vault.root / "0-raw/inbox").glob(".loop-staging-*"))
    assert leftovers == []


def test_create_new_refuses_to_overwrite_a_different_file(gateway):
    """Raw sources are immutable (vault rule 1)."""
    gateway.apply(gateway.begin([_op("0-raw/inbox/new.md", "first\n")]))

    with pytest.raises(Conflict):
        gateway.apply(gateway.begin([_op("0-raw/inbox/new.md", "second\n")]))

    assert gateway.read_text("0-raw/inbox/new.md") == "first\n"


def test_replace_requires_a_matching_expected_hash(gateway):
    path = "1-wiki/concepts/lead-time.md"
    stale = content_hash("something else")

    with pytest.raises(Conflict):
        gateway.apply(gateway.begin([
            _op(path, "new body\n", WriteMode.REPLACE, expected=stale)]))


def test_replace_succeeds_with_the_current_hash(gateway):
    path = "1-wiki/concepts/lead-time.md"
    current = gateway.hash_of(path)

    gateway.apply(gateway.begin([
        _op(path, "new body\n", WriteMode.REPLACE, expected=current)]))

    assert gateway.read_text(path) == "new body\n"


def test_v12_a_concurrent_external_edit_is_detected_not_overwritten(gateway,
                                                                    vault):
    """V12: a newer human edit produces a conflict, never a forced overwrite."""
    path = "1-wiki/entities/Nordic Panels 🇸🇪.md"
    read_hash = gateway.hash_of(path)

    # A human edits the file after we read it.
    target = vault.root / path
    target.write_text(target.read_text(encoding="utf-8") + "\nHuman edit.\n",
                      encoding="utf-8")

    with pytest.raises(Conflict):
        gateway.apply(gateway.begin([
            _op(path, "compiler rewrite\n", WriteMode.REPLACE,
                expected=read_hash)]))

    assert "Human edit." in gateway.read_text(path)


def test_append_preserves_the_existing_body(gateway):
    """V11: human-owned pages permit appended notes only."""
    path = "1-wiki/entities/Nordic Panels 🇸🇪.md"
    original = gateway.read_text(path)
    current = gateway.hash_of(path)

    gateway.apply(gateway.begin([gateway.make_operation(
        path, "\n## Compiler notes\n\nAdded.\n", mode=WriteMode.APPEND,
        expected_hash=current)]))

    updated = gateway.read_text(path)
    assert updated.startswith(original)
    assert "Compiler notes" in updated


# --------------------------------------------------------------------------- #
# V04 / V05 / V13 — crash recovery at each boundary
# --------------------------------------------------------------------------- #
def test_v04_a_crash_before_any_write_leaves_a_resumable_journal(gateway):
    journal = gateway.begin([_op("0-raw/inbox/new.md", "hello\n")])
    # Process dies here — nothing was written.

    outcome = gateway.recover(journal.id)

    assert outcome["state"] == "prepared"
    assert outcome["outstanding"] == ["0-raw/inbox/new.md"]
    assert outcome["already_done"] == []


def test_v04_the_journal_identifies_the_orphan_after_a_crash(gateway):
    """Without the journal an orphan raw file could not be re-identified."""
    journal = gateway.begin([_op("0-raw/inbox/new.md", "hello\n")])
    with pytest.raises(SimulatedCrash):
        gateway.apply(journal, crash_after=1)

    outcome = gateway.recover(journal.id)

    assert outcome["already_done"] == ["0-raw/inbox/new.md"]
    assert outcome["outstanding"] == []


def test_v05_replaying_a_completed_operation_does_not_write_twice(gateway):
    """V05: a replay returns the existing saved capture."""
    operation = _op("0-raw/inbox/new.md", "hello\n")
    gateway.apply(gateway.begin([operation]))
    first_hash = gateway.hash_of("0-raw/inbox/new.md")

    # Same desired content arriving again is recognised as already done.
    gateway.apply(gateway.begin([_op("0-raw/inbox/new.md", "hello\n")]))

    assert gateway.hash_of("0-raw/inbox/new.md") == first_hash


def test_v13_a_crash_midway_through_multiple_writes_is_resumable(gateway):
    operations = [_op("1-wiki/concepts/a.md", "A\n"),
                  _op("1-wiki/concepts/b.md", "B\n"),
                  _op("1-wiki/concepts/c.md", "C\n")]
    journal = gateway.begin(operations)

    with pytest.raises(SimulatedCrash):
        gateway.apply(journal, crash_after=2)

    outcome = gateway.recover(journal.id)

    assert outcome["already_done"] == ["1-wiki/concepts/a.md",
                                       "1-wiki/concepts/b.md"]
    assert outcome["outstanding"] == ["1-wiki/concepts/c.md"]


def test_v13_resuming_does_not_duplicate_an_append(gateway):
    """The dangerous case: a naive resume appends the same note twice."""
    path = "1-wiki/entities/Nordic Panels 🇸🇪.md"
    current = gateway.hash_of(path)
    note = "\n## Compiler notes\n\nOnce only.\n"

    journal = gateway.begin([gateway.make_operation(
        path, note, mode=WriteMode.APPEND, expected_hash=current)])
    gateway.apply(journal)

    # Recovery of the same journal must see the work as complete, not redo it.
    outcome = gateway.recover(journal.id)
    body = gateway.read_text(path)

    assert outcome["state"] == "committed"
    assert body.count("Once only.") == 1


def test_recovery_flags_third_party_content_as_a_conflict(gateway, vault):
    """Someone else's content is never force-overwritten on resume."""
    journal = gateway.begin([_op("1-wiki/concepts/new.md", "ours\n")])
    (vault.root / "1-wiki/concepts/new.md").write_text("theirs\n",
                                                       encoding="utf-8")

    outcome = gateway.recover(journal.id)

    assert outcome["state"] == "conflicted"
    assert outcome["conflicts"] == ["1-wiki/concepts/new.md"]
    assert gateway.read_text("1-wiki/concepts/new.md") == "theirs\n"


def test_incomplete_journals_are_discoverable_after_restart(gateway):
    journal = gateway.begin([_op("0-raw/inbox/new.md", "hello\n")])

    pending = gateway.incomplete_journals()

    assert [j["id"] for j in pending] == [journal.id]


def test_a_committed_journal_is_not_listed_as_incomplete(gateway):
    gateway.apply(gateway.begin([_op("0-raw/inbox/new.md", "hello\n")]))
    assert gateway.incomplete_journals() == []


def test_recovering_an_unknown_journal_is_an_input_error(gateway):
    with pytest.raises(InvalidInput):
        gateway.recover("no-such-journal")


# --------------------------------------------------------------------------- #
# V19 — Unicode identity
# --------------------------------------------------------------------------- #
def test_v19_normalisation_is_for_comparison_only():
    nfd = "Café Acoustics.md"
    nfc = "Café Acoustics.md"

    assert normalise(nfd) == normalise(nfc)
    assert nfd != nfc, "the raw forms differ; only the comparison normalises"


def test_v19_an_emoji_filename_is_readable_unchanged(gateway):
    body = gateway.read_text("1-wiki/entities/Nordic Panels 🇸🇪.md")
    assert "Nordic Panels" in body
