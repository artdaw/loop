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
from dataclasses import dataclass, field
from pathlib import Path

from loop.core.ids import content_hash

logger = logging.getLogger(__name__)

MANIFEST_NAME = "loop-backup.json"
MANIFEST_VERSION = 1


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

    def to_json(self) -> dict[str, object]:
        return {
            "version": self.version, "created_at": self.created_at,
            "schema_revision": self.schema_revision,
            "pending_jobs": self.pending_jobs,
            "pending_outbox": self.pending_outbox,
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
                       pending_outbox=int(payload.get("pending_outbox", 0)))
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


def create_backup(*, database: Path, vault: Path, journal: Path | None,
                  destination: Path, created_at: int,
                  schema_revision: str, pending_jobs: int = 0,
                  pending_outbox: int = 0) -> BackupManifest:
    """Write a coordinated backup of database, vault and journal (O09)."""
    destination.mkdir(parents=True, exist_ok=True)
    manifest = BackupManifest(version=MANIFEST_VERSION, created_at=created_at,
                              schema_revision=schema_revision,
                              pending_jobs=pending_jobs,
                              pending_outbox=pending_outbox)

    if not database.exists():
        raise BackupError(f"database not found: {database}")
    db_bytes = database.read_bytes()
    (destination / "loop.db").write_bytes(db_bytes)
    manifest.parts.append(BackupPart("database", "loop.db",
                                     content_hash(db_bytes), len(db_bytes)))

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


def verify_backup(backup_dir: Path) -> list[str]:
    """Re-hash every part. Returns the problems found, empty when intact."""
    manifest = read_manifest(backup_dir)
    problems: list[str] = []

    for part in manifest.parts:
        target = backup_dir / part.relative_path
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

    vault_target: Path | None = None
    if manifest.part("vault") is not None:
        vault_target = target / "vault"
        _copy_tree(backup_dir / "vault", vault_target)

    journal_target: Path | None = None
    if manifest.part("journal") is not None:
        journal_target = target / "journal.json"
        shutil.copy2(backup_dir / "journal.json", journal_target)

    return RestoreResult(database=database, vault=vault_target,
                         journal=journal_target,
                         pending_jobs=manifest.pending_jobs,
                         pending_outbox=manifest.pending_outbox,
                         manifest=manifest)
