"""Backup and isolated restore (runtime §9, O09, O10, O11).

A backup that restores the database but not the vault is not a backup of Loop —
half the state is files and half is rows, and they reference each other. So the
manifest covers both, plus the operation journal, and records a hash of each
part. A restore that cannot verify what it is restoring is a hope.

**Restore is isolated by construction.** It writes into a target directory it
was given and refuses to touch anything outside it. The failure this prevents is
the memorable one: a restore that "helpfully" cleans the destination and removes
files the backup never contained.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loop.core.ids import content_hash
from loop.ops.snapshot import (
    MAX_ATTEMPTS,
    SnapshotBarrier,
    coordinated_snapshot,
)
from loop.runtime.runs import secure_checkpoint_file

logger = logging.getLogger(__name__)

MANIFEST_NAME = "loop-backup.json"
MANIFEST_VERSION = 2


@dataclass
class BackupPart:
    """One component of a backup, with the hash that proves it arrived."""

    name: str
    relative_path: str
    sha256: str
    byte_count: int


@dataclass
class BackupManifest:
    version: int
    created_at: int
    schema_revision: str
    parts: list[BackupPart] = field(default_factory=list)
    pending_jobs: int = 0
    pending_outbox: int = 0
    #: How many attempts the coordinated snapshot needed. 0 means the backup
    #: was taken without a barrier, so its parts are individually valid but
    #: nothing establishes that they describe the same moment.
    attempts: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "version": self.version, "created_at": self.created_at,
            "schema_revision": self.schema_revision,
            "pending_jobs": self.pending_jobs,
            "pending_outbox": self.pending_outbox,
            "attempts": self.attempts,
            "coordinated": self.attempts > 0,
            "parts": [{"name": p.name, "relative_path": p.relative_path,
                       "sha256": p.sha256, "byte_count": p.byte_count}
                      for p in self.parts],
        }

    @classmethod
    def from_json(cls, payload: dict) -> BackupManifest:
        manifest = cls(version=int(payload["version"]),
                       created_at=int(payload["created_at"]),
                       schema_revision=str(payload.get("schema_revision", "")),
                       pending_jobs=int(payload.get("pending_jobs", 0)),
                       pending_outbox=int(payload.get("pending_outbox", 0)),
                       attempts=int(payload.get("attempts", 0)))
        manifest.parts = [BackupPart(p["name"], p["relative_path"], p["sha256"],
                                     int(p["byte_count"]))
                          for p in payload.get("parts", [])]
        return manifest

    def part(self, name: str) -> BackupPart | None:
        return next((p for p in self.parts if p.name == name), None)


class BackupError(RuntimeError):
    pass


def _copy_tree(source: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for item in sorted(source.rglob("*")):
        if item.is_dir():
            continue
        target = destination / item.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied.append(target)
    return copied


def _tree_hash(root: Path) -> tuple[str, int]:
    """Hash a directory by its sorted relative paths and contents.

    Path-sensitive on purpose: two trees with identical bytes under different
    names are different backups, and a rename is exactly the kind of corruption
    a content-only hash would miss.
    """
    digest_input: list[str] = []
    total = 0
    for item in sorted(root.rglob("*")):
        if item.is_dir():
            continue
        body = item.read_bytes()
        total += len(body)
        digest_input.append(f"{item.relative_to(root).as_posix()}:"
                            f"{content_hash(body)}")
    return content_hash("\n".join(digest_input)), total


def _snapshot_database(source: Path, destination: Path) -> bytes:
    """Copy a live SQLite database with SQLite's consistent backup API.

    The legacy unit fixtures use marker bytes rather than SQLite files; those
    remain byte-copied so older backup formats stay testable.  Production
    databases and LangGraph checkpoints always take the transactional path,
    which includes committed WAL content instead of copying only the main file.
    """
    if source.read_bytes()[:16] != b"SQLite format 3\x00":
        shutil.copy2(source, destination)
        return destination.read_bytes()

    source_uri = f"file:{source.resolve()}?mode=ro"
    with (sqlite3.connect(source_uri, uri=True) as source_db,
          sqlite3.connect(destination) as destination_db):
        source_db.backup(destination_db)
    return destination.read_bytes()


def create_backup(*, database: Path, vault: Path, journal: Path | None,
                  destination: Path, created_at: int,
                  schema_revision: str, pending_jobs: int = 0,
                  pending_outbox: int = 0,
                  checkpoints: Path | None = None,
                  barrier: SnapshotBarrier | None = None,
                  drain: Callable[[], object] | None = None,
                  attempts: int = MAX_ATTEMPTS) -> BackupManifest:
    """Write one verifiable domain/checkpoint/vault backup (O09/LG11).

    Pass `barrier` to take the three stores under one coordinated snapshot.
    Without it the parts are still individually valid and hashed, but nothing
    establishes that they describe the *same moment* — so the shipped command
    always supplies one, and only unit fixtures omit it.
    """
    if destination.exists() and any(destination.iterdir()):
        raise BackupError(
            f"backup destination {destination} is not empty; use a fresh directory")
    destination.mkdir(parents=True, exist_ok=True)

    if barrier is not None:
        stores = {"database": database}
        if checkpoints is not None and checkpoints.exists():
            stores["checkpoints"] = checkpoints
        trees = ({"vault": lambda: _tree_hash(vault)[0]}
                 if vault.exists() else {})
        attempted = coordinated_snapshot(
            barrier=barrier, databases=stores, trees=trees, drain=drain,
            attempts=attempts, reason="backup",
            copy=lambda: _write_parts(
                manifest=_fresh_manifest(created_at, schema_revision,
                                         pending_jobs, pending_outbox),
                database=database, vault=vault, journal=journal,
                checkpoints=checkpoints, destination=destination))
        logger.info("Coordinated snapshot took %d attempt(s)", len(attempted))
        manifest = read_manifest(destination)
        manifest.attempts = len(attempted)
        (destination / MANIFEST_NAME).write_text(
            json.dumps(manifest.to_json(), indent=2, sort_keys=True))
        return manifest
    return _write_parts(
        manifest=_fresh_manifest(created_at, schema_revision, pending_jobs,
                                 pending_outbox),
        database=database, vault=vault, journal=journal,
        checkpoints=checkpoints, destination=destination)


def _fresh_manifest(created_at: int, schema_revision: str, pending_jobs: int,
                    pending_outbox: int) -> BackupManifest:
    return BackupManifest(version=MANIFEST_VERSION, created_at=created_at,
                          schema_revision=schema_revision,
                          pending_jobs=pending_jobs,
                          pending_outbox=pending_outbox)


def _write_parts(*, manifest: BackupManifest, database: Path, vault: Path,
                 journal: Path | None, checkpoints: Path | None,
                 destination: Path) -> BackupManifest:
    """Copy every part and write the manifest. Re-runnable within a retry."""
    manifest.parts.clear()

    if not database.exists():
        raise BackupError(f"database not found: {database}")
    db_bytes = _snapshot_database(database, destination / "loop.db")
    manifest.parts.append(BackupPart("database", "loop.db",
                                     content_hash(db_bytes), len(db_bytes)))

    if checkpoints is not None and checkpoints.exists():
        checkpoint_target = destination / "graph-checkpoints.sqlite"
        checkpoint_bytes = _snapshot_database(checkpoints, checkpoint_target)
        secure_checkpoint_file(checkpoint_target)
        manifest.parts.append(BackupPart(
            "checkpoints", checkpoint_target.name,
            content_hash(checkpoint_bytes), len(checkpoint_bytes)))

    if vault.exists():
        _copy_tree(vault, destination / "vault")
        digest, size = _tree_hash(destination / "vault")
        manifest.parts.append(BackupPart("vault", "vault", digest, size))

    if journal is not None and journal.exists():
        journal_bytes = journal.read_bytes()
        (destination / "journal.json").write_bytes(journal_bytes)
        manifest.parts.append(BackupPart("journal", "journal.json",
                                         content_hash(journal_bytes),
                                         len(journal_bytes)))

    (destination / MANIFEST_NAME).write_text(
        json.dumps(manifest.to_json(), indent=2, sort_keys=True))
    return manifest


def read_manifest(backup_dir: Path) -> BackupManifest:
    path = backup_dir / MANIFEST_NAME
    if not path.exists():
        raise BackupError(f"no backup manifest in {backup_dir}")
    return BackupManifest.from_json(json.loads(path.read_text()))


#: Parts a restorable backup must carry. A manifest without the database
#: describes something that cannot be restored, however well its other parts
#: hash.
REQUIRED_PARTS = ("database",)


def _confined(backup_dir: Path, relative: str) -> Path | None:
    """Resolve a manifest path inside the backup, or None if it escapes.

    The manifest is data, and a backup can arrive from anywhere. A part naming
    `../../.ssh/authorized_keys` would otherwise be written there by restore —
    the manifest choosing the destination rather than the operator.
    """
    if Path(relative).is_absolute():
        return None
    root = backup_dir.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def verify_backup(backup_dir: Path) -> list[str]:
    """Validate structure, paths and hashes. Empty means restorable.

    Hash equality alone is not enough: a tampered manifest can be internally
    consistent, name paths outside the backup, or omit the database entirely.
    """
    manifest = read_manifest(backup_dir)
    problems: list[str] = []

    if manifest.version > MANIFEST_VERSION:
        problems.append(
            f"manifest version {manifest.version} is newer than this build "
            f"understands ({MANIFEST_VERSION})")

    present = {part.name for part in manifest.parts}
    for required in REQUIRED_PARTS:
        if required not in present:
            problems.append(f"{required}: required part is missing from the manifest")

    for part in manifest.parts:
        # Checked before resolving: `Path.resolve` follows the link, so a
        # symlink would otherwise be reported as whatever it points at.
        literal = backup_dir / part.relative_path
        if literal.is_symlink() or (literal.is_dir()
                                    and any(child.is_symlink()
                                            for child in literal.rglob("*"))):
            problems.append(f"{part.name}: contains a symlink")
            continue

        target = _confined(backup_dir, part.relative_path)
        if target is None:
            problems.append(
                f"{part.name}: path {part.relative_path!r} escapes the backup")
            continue
        if not target.exists():
            problems.append(f"{part.name}: missing from the backup")
            continue
        if target.is_dir():
            digest, _ = _tree_hash(target)
        else:
            digest = content_hash(target.read_bytes())
        if digest != part.sha256:
            problems.append(f"{part.name}: hash does not match the manifest")
    return problems


@dataclass
class RestoreResult:
    database: Path
    checkpoints: Path | None
    vault: Path | None
    journal: Path | None
    pending_jobs: int
    pending_outbox: int
    manifest: BackupManifest


def restore_backup(backup_dir: Path, *, target: Path,
                   verify: bool = True) -> RestoreResult:
    """Restore into an isolated target directory (O09).

    Refuses a tampered backup, and refuses a non-empty target. Overwriting an
    existing installation during a restore is how a recovery attempt destroys
    the thing it was trying to recover.
    """
    if verify:
        problems = verify_backup(backup_dir)
        if problems:
            raise BackupError(
                "This backup does not match its manifest: " + "; ".join(problems))

    manifest = read_manifest(backup_dir)
    if target.exists() and any(target.iterdir()):
        raise BackupError(
            f"restore target {target} is not empty; restore into a fresh "
            f"directory so the current installation is left intact")
    target.mkdir(parents=True, exist_ok=True)

    database = target / "loop.db"
    shutil.copy2(backup_dir / "loop.db", database)

    checkpoint_target: Path | None = None
    checkpoint_part = manifest.part("checkpoints")
    if checkpoint_part is not None:
        checkpoint_target = target / "graph-checkpoints.sqlite"
        shutil.copy2(backup_dir / checkpoint_part.relative_path,
                     checkpoint_target)
        secure_checkpoint_file(checkpoint_target)

    vault_target: Path | None = None
    if manifest.part("vault") is not None:
        vault_target = target / "vault"
        _copy_tree(backup_dir / "vault", vault_target)

    journal_target: Path | None = None
    if manifest.part("journal") is not None:
        journal_target = target / "journal.json"
        shutil.copy2(backup_dir / "journal.json", journal_target)

    return RestoreResult(database=database, checkpoints=checkpoint_target,
                         vault=vault_target,
                         journal=journal_target,
                         pending_jobs=manifest.pending_jobs,
                         pending_outbox=manifest.pending_outbox,
                         manifest=manifest)
