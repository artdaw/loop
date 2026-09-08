"""Coordinated backup and isolated restore (O09, LG11 — R1).

Loop keeps three stores that must agree: domain SQLite, the LangGraph
checkpoint database, and the vault. Copying them in sequence yields three
snapshots of three moments, and the damage is invisible — every file is
individually valid, every hash matches, and the restored system holds a job
whose checkpoint does not exist.

So hash equality is deliberately *not* the exit assertion here. These tests ask
whether the protocol can tell a consistent snapshot from a torn one, and
whether it refuses rather than writes when it cannot.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from loop.app import build_application
from loop.core.settings import Settings
from loop.ops.backup import (
    BackupError,
    create_backup,
    read_manifest,
    restore_backup,
    verify_backup,
)
from loop.ops.snapshot import (
    ConsistencyWatch,
    SnapshotRefused,
    coordinated_snapshot,
    data_version,
)
from tests.vnext.vault_fixtures import build_minimal_vault


@pytest.fixture
def vault(tmp_path: Path):
    return build_minimal_vault(tmp_path / "vault")


@pytest.fixture
def app(settings: Settings, clock, vault):
    return build_application(
        settings.model_copy(update={"obsidian_vault_path": str(vault.root)}),
        clock=clock)


def backup_of(app, destination: Path, **kw):
    pending = app.service.pending_summary()
    return create_backup(
        database=Path(app.settings.database_url.replace("sqlite:///", "")),
        checkpoints=None,
        vault=app.settings.vault_root,
        journal=None, destination=destination,
        created_at=int(app.clock.now().timestamp()),
        schema_revision="0004_trips",
        pending_jobs=int(pending["jobs"]),
        pending_outbox=int(pending["outbox"]),
        barrier=app.barrier, **kw)


# --------------------------------------------------------------------------- #
# The barrier is admission control, honoured by real writers
# --------------------------------------------------------------------------- #
def test_the_sweep_declines_to_admit_work_while_a_snapshot_holds(app):
    with app.barrier.hold(reason="test"):
        report = app.service.tick()

    assert any("snapshot in progress" in detail for detail in report.details)
    assert report.triggers_fired == 0


def test_the_routine_worker_declines_too(app):
    app.jobs.enqueue("routine.run", dedupe_key="r1",
                     payload={"slug": "x", "occurrence_key": "o"})

    with app.barrier.hold(reason="test"):
        assert app.routine_worker.run_one() is None

    # And resumes the moment the barrier is released.
    assert app.routine_worker.run_one() is not None


def test_the_barrier_is_released_even_when_the_snapshot_raises(app):
    with pytest.raises(RuntimeError), app.barrier.hold(reason="test"):
        raise RuntimeError("boom")

    assert app.barrier.held() is False
    assert app.service.tick().details == [] or not any(
        "snapshot" in d for d in app.service.tick().details)


def test_a_stale_barrier_expires_rather_than_stopping_the_service(app, clock):
    """A crashed backup must not hold the service down forever."""
    with app.barrier.hold(reason="test", ttl_seconds=60):
        assert app.barrier.held()
        clock.advance(minutes=5)
        assert app.barrier.held() is False


# --------------------------------------------------------------------------- #
# Consistency is proven, not assumed
# --------------------------------------------------------------------------- #
def test_a_quiet_snapshot_succeeds_on_the_first_attempt(app, tmp_path):
    manifest = backup_of(app, tmp_path / "backup")

    assert manifest.attempts == 1
    assert verify_backup(tmp_path / "backup") == []
    assert read_manifest(tmp_path / "backup").to_json()["coordinated"] is True


def test_a_writer_racing_the_snapshot_is_detected_and_refused(app, tmp_path):
    """The failure this whole slice exists for.

    `data_version` changes when *any* connection commits, so a writer the
    snapshot does not know about is still caught. Here one commits during
    every copy, so no attempt can be shown consistent.
    """
    database = Path(app.settings.database_url.replace("sqlite:///", ""))

    def racing_copy() -> None:
        app.tasks.create("written during the snapshot")

    with pytest.raises(SnapshotRefused) as raised:
        coordinated_snapshot(
            barrier=app.barrier, databases={"database": database},
            copy=racing_copy, attempts=2)

    assert "a writer committed during the snapshot" in str(raised.value)
    assert "database" in str(raised.value)


def test_a_snapshot_that_settles_succeeds_after_retrying(app, tmp_path):
    """A transient writer should not fail the backup outright."""
    database = Path(app.settings.database_url.replace("sqlite:///", ""))
    calls = {"n": 0}

    def sometimes_racing() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            app.tasks.create("late write")

    attempts = coordinated_snapshot(
        barrier=app.barrier, databases={"database": database},
        copy=sometimes_racing, attempts=3)

    assert [a.consistent for a in attempts] == [False, True]


def test_a_vault_edited_during_the_snapshot_is_detected(app, tmp_path, vault):
    """People edit the vault in an editor; that is not a writer we control."""
    database = Path(app.settings.database_url.replace("sqlite:///", ""))
    from loop.ops.backup import _tree_hash

    def edit_the_vault() -> None:
        (vault.root / "0-raw/inbox/edited-mid-backup.md").write_text(
            "written while the backup ran", encoding="utf-8")

    with pytest.raises(SnapshotRefused) as raised:
        coordinated_snapshot(
            barrier=app.barrier, databases={"database": database},
            trees={"vault": lambda: _tree_hash(vault.root)[0]},
            copy=edit_the_vault, attempts=1)

    assert "vault" in str(raised.value)


def test_a_refused_snapshot_says_what_to_do_rather_than_leaving_a_bad_backup(
        app, tmp_path):
    database = Path(app.settings.database_url.replace("sqlite:///", ""))

    with pytest.raises(SnapshotRefused) as raised:
        coordinated_snapshot(
            barrier=app.barrier, databases={"database": database},
            copy=lambda: app.tasks.create("noise"), attempts=1)

    message = str(raised.value)
    assert "Nothing was written that should be kept" in message
    assert "retry when the service is quiet" in message


def test_the_watch_sees_another_connections_commit(app):
    """The property the whole protocol rests on, asserted directly.

    `PRAGMA data_version` reports changes made by *other* connections since
    **this** connection last read it. A fresh connection per reading — the
    obvious implementation — always compares equal, so the check would pass
    while detecting nothing. `ConsistencyWatch` holds one connection open for
    exactly this reason.
    """
    database = Path(app.settings.database_url.replace("sqlite:///", ""))
    watch = ConsistencyWatch({"database": database})
    try:
        assert watch.changed() == []

        other = sqlite3.connect(database)
        other.execute("CREATE TABLE IF NOT EXISTS scratch (id INTEGER)")
        other.execute("INSERT INTO scratch (id) VALUES (1)")
        other.commit()
        other.close()

        assert watch.changed() == ["database"]
    finally:
        watch.close()


def test_a_fresh_reading_per_probe_would_have_detected_nothing(app):
    """Why the standalone reading is kept for diagnostics only.

    Pinned as a test because the broken version is the one a reader would
    write by default, and it fails silently in the safe-looking direction.
    """
    database = Path(app.settings.database_url.replace("sqlite:///", ""))
    before = data_version(database)
    app.tasks.create("committed between readings")

    assert data_version(database) == before, (
        "if this ever differs, the standalone reading became comparable and "
        "the comment above should be revisited")


# --------------------------------------------------------------------------- #
# What a backup must refuse to restore
# --------------------------------------------------------------------------- #
def test_a_manifest_from_a_newer_build_is_refused(app, tmp_path):
    backup_of(app, tmp_path / "backup")
    manifest = tmp_path / "backup" / "loop-backup.json"
    manifest.write_text(manifest.read_text().replace('"version": 2',
                                                     '"version": 99'))

    problems = verify_backup(tmp_path / "backup")

    assert any("newer than this build" in p for p in problems)


def test_a_manifest_missing_the_database_is_refused(app, tmp_path):
    backup_of(app, tmp_path / "backup")
    manifest = tmp_path / "backup" / "loop-backup.json"
    body = manifest.read_text().replace('"name": "database"', '"name": "notes"')
    manifest.write_text(body)

    problems = verify_backup(tmp_path / "backup")

    assert any("required part is missing" in p for p in problems)


def test_a_part_pointing_outside_the_backup_is_refused(app, tmp_path):
    """The manifest is data; it must not choose where restore writes."""
    backup_of(app, tmp_path / "backup")
    manifest = tmp_path / "backup" / "loop-backup.json"
    manifest.write_text(manifest.read_text().replace(
        '"relative_path": "loop.db"',
        '"relative_path": "../../escaped.db"'))

    problems = verify_backup(tmp_path / "backup")

    assert any("escapes the backup" in p for p in problems)


def test_a_tampered_part_is_refused(app, tmp_path):
    backup_of(app, tmp_path / "backup")
    (tmp_path / "backup" / "loop.db").write_bytes(b"not a database")

    problems = verify_backup(tmp_path / "backup")

    assert any("hash does not match" in p for p in problems)
    with pytest.raises(BackupError):
        restore_backup(tmp_path / "backup", target=tmp_path / "restored")


def test_a_symlinked_part_is_refused(app, tmp_path):
    backup_of(app, tmp_path / "backup")
    database = tmp_path / "backup" / "loop.db"
    database.unlink()
    database.symlink_to(tmp_path / "elsewhere.db")

    assert any("symlink" in p for p in verify_backup(tmp_path / "backup"))


def test_restore_refuses_a_target_that_already_has_files(app, tmp_path):
    """Overwriting an installation is how a recovery destroys what it saves."""
    backup_of(app, tmp_path / "backup")
    target = tmp_path / "restored"
    target.mkdir()
    (target / "loop.db").write_text("existing installation")

    with pytest.raises(BackupError) as raised:
        restore_backup(tmp_path / "backup", target=target)

    assert "not empty" in str(raised.value)
    assert (target / "loop.db").read_text() == "existing installation"


def test_backup_refuses_a_destination_that_is_not_empty(app, tmp_path):
    destination = tmp_path / "backup"
    destination.mkdir()
    (destination / "stray.txt").write_text("something")

    with pytest.raises(BackupError):
        backup_of(app, destination)


# --------------------------------------------------------------------------- #
# Restore into a new process, and resume the pinned work
# --------------------------------------------------------------------------- #
RESUME = """
import json, sys
from pathlib import Path
from loop.app import build_application
from loop.core.settings import Settings

target = Path(sys.argv[1])
settings = Settings(_env_file=None, environment="test",
                    database_url=f"sqlite:///{target / 'loop.db'}",
                    data_dir=str(target), obsidian_vault_path=str(target / "vault"),
                    capability_paths="[]")
app = build_application(settings)
print(json.dumps({
    "tasks": [t.title for t in app.tasks.list()],
    "jobs": app.service.pending_summary()["jobs"],
    "vault_present": (target / "vault" / "0-raw" / "_ledger.md").exists(),
}))
"""


def test_a_restored_backup_opens_in_a_new_process_with_its_pending_work(
        app, tmp_path):
    """Restoring is only useful if another process can pick the work up."""
    app.tasks.create("survive the restore")
    app.jobs.enqueue("routine.run", dedupe_key="pending-1",
                     payload={"slug": "x", "occurrence_key": "o"})

    backup_of(app, tmp_path / "backup")
    result = restore_backup(tmp_path / "backup", target=tmp_path / "restored")
    assert result.database.exists()

    script = tmp_path / "resume.py"
    script.write_text(RESUME, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "restored")],
        capture_output=True, text=True, cwd=Path.cwd())

    assert completed.returncode == 0, completed.stderr
    state = json.loads(completed.stdout.strip().splitlines()[-1])
    assert "survive the restore" in state["tasks"]
    assert state["jobs"] == 1, "pending work did not survive the restore"
    assert state["vault_present"] is True


def test_restoring_does_not_touch_the_original(app, tmp_path):
    app.tasks.create("original only")
    backup_of(app, tmp_path / "backup")

    restore_backup(tmp_path / "backup", target=tmp_path / "restored")
    app.tasks.create("added after the backup")

    with sqlite3.connect(tmp_path / "restored" / "loop.db") as restored:
        titles = {row[0] for row in
                  restored.execute("SELECT title FROM tasks").fetchall()}
    assert "original only" in titles
    assert "added after the backup" not in titles
    assert len(app.tasks.list()) == 2
