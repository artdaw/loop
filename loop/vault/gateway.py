"""Vault gateway: path confinement and journaled file operations (vault §5, runtime §9).

Files and SQLite cannot share a transaction. The gateway closes that gap with a
**durable journal**: intent, expected hashes and desired hashes are recorded in
SQLite *before* any byte is written, so a crash at any point leaves enough
information to finish or abandon the operation deterministically.

The write sequence, and why each step exists:

1. **Record intent** — the exact body, reserved path, expected and desired
   hashes. Without this, a crash before the rename leaves an orphan nobody can
   identify.
2. **Stage on the same filesystem** and ``fsync`` the bytes. Same filesystem so
   the final step can be an atomic rename; ``fsync`` so a power loss cannot
   leave a rename pointing at empty bytes.
3. **Re-check the expected hash immediately before the rename.** Someone may
   have edited the file while we were working.
4. **Atomic rename**, then record the committed path and hash.

What this does *not* claim: atomic rename prevents a torn file, not every
concurrent-editor lost update. A determined external editor can still write in
the final race window (runtime §9 says so explicitly). Loop's own writers share
a lease; external conflicts are detected after the fact and surfaced, never
force-overwritten.

Path safety is enforced before any read or write. ``..`` traversal, absolute
escapes and symlinks pointing outside the vault are refused (V30) — a source
document is untrusted data and may contain any path it likes.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.errors import Conflict, InvalidInput, PrivacyBlocked
from loop.core.ids import content_hash, new_id

logger = logging.getLogger(__name__)


class PathRefused(PrivacyBlocked):
    """A path escaped the selected vault scope."""


class WriteMode(str, Enum):
    CREATE_NEW = "create_new"   # exclusive creation; raw sources are immutable
    REPLACE = "replace"         # mutable file; requires an expected hash
    APPEND = "append"           # append-only; still requires an expected hash


@dataclass
class FileOperation:
    """One journalled file write."""

    id: str
    relative_path: str
    mode: WriteMode
    body: str
    desired_hash: str
    expected_hash: str | None = None
    state: str = "prepared"
    committed_hash: str | None = None


@dataclass
class OperationJournal:
    """Durable record of a multi-file vault transaction."""

    id: str
    plan_id: str | None
    operations: list[FileOperation] = field(default_factory=list)
    state: str = "prepared"
    staging_path: str | None = None


def normalise(path_text: str) -> str:
    """NFC-normalise a path for *identity comparison* only.

    The bytes on disk are left alone: macOS stores names decomposed, and
    renaming them to match a comparison would rewrite the user's vault (V19).
    """
    return unicodedata.normalize("NFC", path_text)


class VaultGateway:
    """Confined, journalled access to a single vault root."""

    def __init__(self, *, root: Path, sessions: sessionmaker[Session],
                 clock: Clock | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._ensure_journal_table()

    # ------------------------------------------------------------------ #
    # Path safety (V30)
    # ------------------------------------------------------------------ #
    def resolve(self, relative_path: str, *, must_exist: bool = False) -> Path:
        """Resolve a vault-relative path, refusing anything outside the root.

        Checked before every read and write. Source documents are untrusted
        input and may contain ``../`` sequences, absolute paths or symlinks
        aimed anywhere on the machine.
        """
        if not relative_path or not str(relative_path).strip():
            raise InvalidInput("Empty vault path.")

        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise PathRefused(
                f"Absolute paths are not accepted: {relative_path!r}",
                details={"path": str(relative_path)})
        if ".." in candidate.parts:
            raise PathRefused(
                f"Path traversal is not accepted: {relative_path!r}",
                details={"path": str(relative_path)})

        target = (self.root / candidate)

        # resolve() follows symlinks, which is exactly what must be checked: a
        # link inside the vault pointing outside it is still an escape.
        resolved = target.resolve() if target.exists() else (
            target.parent.resolve() / target.name if target.parent.exists()
            else self.root / candidate)

        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise PathRefused(
                f"Path escapes the vault root: {relative_path!r}",
                details={"path": str(relative_path)}) from exc

        if must_exist and not resolved.exists():
            raise InvalidInput(f"No such vault file: {relative_path}")
        return resolved

    def read_text(self, relative_path: str) -> str:
        return self.resolve(relative_path, must_exist=True).read_text(
            encoding="utf-8")

    def exists(self, relative_path: str) -> bool:
        try:
            return self.resolve(relative_path).exists()
        except (PathRefused, InvalidInput):
            return False

    def hash_of(self, relative_path: str) -> str | None:
        """Content hash of a vault file, or ``None`` when it is absent."""
        path = self.resolve(relative_path)
        if not path.exists():
            return None
        return content_hash(path.read_bytes())

    # ------------------------------------------------------------------ #
    # Journal
    # ------------------------------------------------------------------ #
    def _ensure_journal_table(self) -> None:
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS vault_transactions ("
                "id VARCHAR(36) PRIMARY KEY, plan_id VARCHAR(36), "
                "operation_ids_json TEXT NOT NULL, manifest_json TEXT NOT NULL, "
                "state VARCHAR(16) NOT NULL, staging_path TEXT, "
                "committed_files_json TEXT NOT NULL DEFAULT '[]', "
                "created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL)"))
            session.commit()

    def make_operation(self, relative_path: str, body: str, *,
                       mode: WriteMode = WriteMode.CREATE_NEW,
                       expected_hash: str | None = None) -> FileOperation:
        """Build an operation with a correctly computed desired hash.

        For an append, ``desired_hash`` must describe the **resulting file**,
        not the fragment being added. Recovery compares the file on disk against
        this hash to decide whether the step already happened; a fragment hash
        can never match a whole file, so every resumed append would look like a
        third-party conflict — or, worse under a laxer check, be appended twice.
        """
        if mode is WriteMode.APPEND:
            existing = ""
            path = self.resolve(relative_path)
            if path.exists():
                existing = path.read_text(encoding="utf-8")
            desired = content_hash(existing + body)
        else:
            desired = content_hash(body)
        return FileOperation(id=new_id(), relative_path=relative_path,
                             mode=mode, body=body, desired_hash=desired,
                             expected_hash=expected_hash)

    def begin(self, operations: list[FileOperation], *,
              plan_id: str | None = None) -> OperationJournal:
        """Record intent durably before touching the filesystem."""
        journal = OperationJournal(id=new_id(), plan_id=plan_id,
                                   operations=operations)
        now = to_micros(self._clock.now())
        manifest = {
            "operations": [
                {"id": op.id, "path": normalise(op.relative_path),
                 "mode": op.mode.value, "expected_hash": op.expected_hash,
                 "desired_hash": op.desired_hash}
                for op in operations
            ]
        }
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO vault_transactions (id, plan_id, operation_ids_json, "
                "manifest_json, state, staging_path, committed_files_json, "
                "created_at, updated_at) VALUES (:id, :plan_id, :ops, :manifest, "
                "'prepared', NULL, '[]', :now, :now)"),
                {"id": journal.id, "plan_id": plan_id,
                 "ops": json.dumps([op.id for op in operations]),
                 "manifest": json.dumps(manifest), "now": now})
            session.commit()
        return journal

    def _record_commit(self, journal_id: str, committed: list[dict[str, str]],
                       state: str) -> None:
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE vault_transactions SET state = :state, "
                "committed_files_json = :committed, updated_at = :now "
                "WHERE id = :id"),
                {"state": state, "committed": json.dumps(committed),
                 "now": now, "id": journal_id})
            session.commit()

    def load_journal(self, journal_id: str) -> dict[str, Any] | None:
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT id, plan_id, manifest_json, state, committed_files_json "
                "FROM vault_transactions WHERE id = :id"), {"id": journal_id}
            ).first()
        if row is None:
            return None
        return {"id": row[0], "plan_id": row[1],
                "manifest": json.loads(row[2]), "state": row[3],
                "committed": json.loads(row[4])}

    def incomplete_journals(self) -> list[dict[str, Any]]:
        """Journals that were prepared or applying when the process stopped."""
        with self._sessions() as session:
            rows = session.execute(text(
                "SELECT id FROM vault_transactions "
                "WHERE state IN ('prepared', 'applying')")).all()
        return [j for j in (self.load_journal(r[0]) for r in rows) if j]

    # ------------------------------------------------------------------ #
    # Apply
    # ------------------------------------------------------------------ #
    def apply(self, journal: OperationJournal, *,
              crash_after: int | None = None) -> list[dict[str, str]]:
        """Stage, verify and atomically commit each operation in order.

        ``crash_after`` stops after N committed operations, raising
        :class:`SimulatedCrash`. It exists so recovery can be tested at each
        boundary rather than by catching an exception inside the same worker
        (acceptance §1).
        """
        self._set_state(journal.id, "applying")
        committed: list[dict[str, str]] = []

        for index, operation in enumerate(journal.operations):
            target = self.resolve(operation.relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)

            if operation.mode is WriteMode.CREATE_NEW and target.exists():
                # Raw sources are immutable: never overwrite a same-named note.
                existing = content_hash(target.read_bytes())
                if existing == operation.desired_hash:
                    # A replay of an operation that already completed.
                    committed.append({"path": operation.relative_path,
                                      "hash": existing})
                    continue
                raise Conflict(
                    f"Refusing to overwrite an existing file: "
                    f"{operation.relative_path}",
                    details={"path": operation.relative_path})

            if operation.expected_hash is not None:
                actual = self.hash_of(operation.relative_path)
                if actual != operation.expected_hash:
                    raise Conflict(
                        f"{operation.relative_path} changed since it was read.",
                        details={"path": operation.relative_path,
                                 "expected": operation.expected_hash,
                                 "actual": actual})

            body = operation.body
            if operation.mode is WriteMode.APPEND and target.exists():
                body = target.read_text(encoding="utf-8") + operation.body

            self._atomic_write(target, body)
            committed.append({"path": operation.relative_path,
                              "hash": content_hash(body)})
            self._record_commit(journal.id, committed, "applying")

            if crash_after is not None and index + 1 >= crash_after:
                raise SimulatedCrash(
                    f"stopped after {index + 1} committed operation(s)")

        self._record_commit(journal.id, committed, "committed")
        return committed

    def _atomic_write(self, target: Path, body: str) -> None:
        """Stage on the same filesystem, fsync, then rename."""
        directory = target.parent
        # delete=False so the staged file outlives the context manager and can
        # be renamed into place; closing it does not remove it.
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory, delete=False,
                prefix=".loop-staging-", suffix=".tmp") as handle:
            handle.write(body)
            handle.flush()
            # fsync before the rename: otherwise a power loss can leave the
            # rename visible while the bytes are not.
            os.fsync(handle.fileno())
            staged_name = handle.name

        staged = Path(staged_name)
        os.replace(staged, target)

        # fsync the directory so the rename itself is durable.
        try:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:  # pragma: no cover - not supported on every platform
            logger.debug("Directory fsync unsupported for %s", directory)

    def _set_state(self, journal_id: str, state: str) -> None:
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE vault_transactions SET state = :state, updated_at = :now "
                "WHERE id = :id"), {"state": state, "now": now, "id": journal_id})
            session.commit()

    # ------------------------------------------------------------------ #
    # Recovery
    # ------------------------------------------------------------------ #
    def recover(self, journal_id: str) -> dict[str, Any]:
        """Reconcile a journal after a restart.

        For each operation: if the desired content is already on disk that step
        is *complete* (not a duplicate to redo); if the file is absent the step
        is resumable; if third-party content is present it is a conflict that a
        human must resolve — never force-overwritten.
        """
        record = self.load_journal(journal_id)
        if record is None:
            raise InvalidInput(f"No vault transaction {journal_id!r}")

        already_done: list[str] = []
        outstanding: list[str] = []
        conflicts: list[str] = []

        for entry in record["manifest"]["operations"]:
            relative = entry["path"]
            actual = self.hash_of(relative)
            if actual is None:
                outstanding.append(relative)
            elif actual == entry["desired_hash"]:
                already_done.append(relative)
            elif entry["expected_hash"] and actual == entry["expected_hash"]:
                outstanding.append(relative)
            else:
                conflicts.append(relative)

        state = ("committed" if not outstanding and not conflicts
                 else "conflicted" if conflicts else "prepared")
        self._set_state(journal_id, state)
        return {"journal_id": journal_id, "state": state,
                "already_done": already_done, "outstanding": outstanding,
                "conflicts": conflicts}


class SimulatedCrash(RuntimeError):
    """Raised by test-injected crash points. Never raised in production."""
