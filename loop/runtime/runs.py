"""Run mappings and checkpoint durability (agent-stack §4).

Two databases, deliberately separate:

* **Domain SQLite** holds plans, work items, operations and outcomes. These are
  the authoritative business records.
* **The graph checkpointer** holds execution *position* — which node is next,
  what the graph state contains — plus references back to domain rows.

They cannot share a transaction, so the run mapping below is the join between
them: root run ID, thread ID, pinned graph and pack versions, registry hash,
policy revision, privacy label and lifecycle status. Without that row a restart
knows a thread exists but not what it was allowed to do (LG06).

The invariants this module exists to hold:

* **A checkpoint is never evidence of an effect.** Delivery truth lives in the
  outbox and the operation ledger; a graph that resumed past a send node proves
  only that the node ran (LG07).
* **Versions are pinned per run.** A pack upgraded mid-run does not silently
  change what a paused run resumes into — it pauses with an actionable status
  instead (LG10).
* **Checkpoints contain private content.** They are local, permission-restricted,
  and purged under the same retention policy as their source (LG11).
* **Cancellation and disablement are rechecked on resume**, not trusted from the
  state captured before the pause (LG09).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.errors import Conflict, InvalidInput, Unavailable
from loop.core.ids import content_hash, new_id
from loop.core.privacy import PrivacyLabel

logger = logging.getLogger(__name__)

#: Default checkpoint database location (agent-stack §4).
CHECKPOINT_FILENAME = "graph-checkpoints.sqlite"

#: Owner-only permissions: checkpoints can contain private content.
CHECKPOINT_MODE = 0o600


class RunStatus(str, Enum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: A pinned graph or pack version is no longer available (LG10).
    VERSION_UNAVAILABLE = "version_unavailable"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.SUCCEEDED, RunStatus.FAILED,
                        RunStatus.CANCELLED)

    @property
    def is_resumable(self) -> bool:
        return self in (RunStatus.AWAITING_APPROVAL, RunStatus.PAUSED,
                        RunStatus.RUNNING)


@dataclass
class RunMapping:
    """The join between a domain run and its graph thread."""

    id: str
    root_event_id: str
    thread_id: str
    graph_version: str
    state_schema_version: str
    registry_revision: int
    registry_hash: str
    policy_revision: str | None
    privacy: PrivacyLabel
    status: RunStatus = RunStatus.RUNNING
    pinned_packs: dict[str, str] = field(default_factory=dict)
    checkpoint_ref: str | None = None
    pause_reason: str = ""
    #: The run whose tool call started this one, if this is a child. A dynamic
    #: graph invoked through a tool is invisible to static graph inspection
    #: (agent-stack §2), so this column is what lets status and cancellation
    #: find it at all.
    parent_run_id: str | None = None

    @property
    def is_pinned_to(self) -> dict[str, str]:
        return dict(self.pinned_packs)


class RunStore:
    """Persists run mappings alongside the domain database."""

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS run_mappings ("
                "id VARCHAR(36) PRIMARY KEY, root_event_id VARCHAR(36) NOT NULL, "
                "thread_id VARCHAR(64) NOT NULL UNIQUE, "
                "graph_version VARCHAR(32) NOT NULL, "
                "state_schema_version VARCHAR(32) NOT NULL, "
                "registry_revision INTEGER NOT NULL, "
                "registry_hash VARCHAR(64) NOT NULL, "
                "policy_revision VARCHAR(64), privacy TEXT NOT NULL, "
                "status VARCHAR(24) NOT NULL, pinned_packs TEXT NOT NULL, "
                "checkpoint_ref VARCHAR(128), pause_reason TEXT NOT NULL "
                "DEFAULT '', parent_run_id VARCHAR(36), "
                "created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL)"))
            session.commit()
            self._ensure_parent_run_id_column(session)

    def _ensure_parent_run_id_column(self, session: Session) -> None:
        """Add the column to a database created before it existed.

        `run_mappings` is created with `IF NOT EXISTS`, so an older database
        keeps its original columns forever unless a new one is added
        explicitly — the same trap the vNext migrations guard against for the
        SQLAlchemy-mapped tables.
        """
        columns = {row[1] for row in session.execute(
            text("PRAGMA table_info(run_mappings)")).all()}
        if "parent_run_id" not in columns:
            session.execute(text(
                "ALTER TABLE run_mappings ADD COLUMN parent_run_id VARCHAR(36)"))
            session.commit()

    # ------------------------------------------------------------------ #
    # Creation
    # ------------------------------------------------------------------ #
    def create(self, *, root_event_id: str, graph_version: str,
               state_schema_version: str, registry_revision: int,
               registry_hash: str, privacy: PrivacyLabel,
               pinned_packs: dict[str, str] | None = None,
               policy_revision: str | None = None,
               thread_id: str | None = None,
               parent_run_id: str | None = None) -> RunMapping:
        """Record a run and its thread.

        The thread belongs to **one root run**, not to a whole conversation
        (agent-stack §4): sharing a thread across requests would let one
        request's cancellation or approval leak into another's execution.
        """
        mapping = RunMapping(
            id=new_id(), root_event_id=root_event_id,
            thread_id=thread_id or f"run-{new_id()}",
            graph_version=graph_version,
            state_schema_version=state_schema_version,
            registry_revision=registry_revision, registry_hash=registry_hash,
            policy_revision=policy_revision, privacy=privacy,
            pinned_packs=dict(pinned_packs or {}),
            parent_run_id=parent_run_id)

        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO run_mappings (id, root_event_id, thread_id, "
                "graph_version, state_schema_version, registry_revision, "
                "registry_hash, policy_revision, privacy, status, pinned_packs, "
                "checkpoint_ref, pause_reason, parent_run_id, created_at, "
                "updated_at) VALUES (:id, :root, :thread, :graph, :schema, "
                ":revision, :hash, :policy, :privacy, :status, :packs, NULL, "
                "'', :parent, :now, :now)"),
                {"id": mapping.id, "root": root_event_id,
                 "thread": mapping.thread_id, "graph": graph_version,
                 "schema": state_schema_version, "revision": registry_revision,
                 "hash": registry_hash, "policy": policy_revision,
                 "privacy": json.dumps(privacy.to_json()),
                 "status": mapping.status.value,
                 "packs": json.dumps(mapping.pinned_packs, sort_keys=True),
                 "parent": parent_run_id, "now": now})
            session.commit()
        return mapping

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def get(self, run_id: str) -> RunMapping | None:
        return self._fetch("id = :value", run_id)

    def by_thread(self, thread_id: str) -> RunMapping | None:
        return self._fetch("thread_id = :value", thread_id)

    _COLUMNS = ("id, root_event_id, thread_id, graph_version, "
               "state_schema_version, registry_revision, registry_hash, "
               "policy_revision, privacy, status, pinned_packs, "
               "checkpoint_ref, pause_reason, parent_run_id")

    def _fetch(self, clause: str, value: str) -> RunMapping | None:
        with self._sessions() as session:
            row = session.execute(text(
                f"SELECT {self._COLUMNS} FROM run_mappings "
                f"WHERE {clause}"), {"value": value}).first()
        if row is None:
            return None
        return self._row_to_mapping(row)

    def _row_to_mapping(self, row: Any) -> RunMapping:
        return RunMapping(
            id=row[0], root_event_id=row[1], thread_id=row[2],
            graph_version=row[3], state_schema_version=row[4],
            registry_revision=row[5], registry_hash=row[6],
            policy_revision=row[7],
            privacy=PrivacyLabel.from_json(json.loads(row[8])),
            status=RunStatus(row[9]), pinned_packs=json.loads(row[10]),
            checkpoint_ref=row[11], pause_reason=row[12], parent_run_id=row[13])

    def children_of(self, parent_run_id: str) -> list[RunMapping]:
        """Runs a tool call started under this run (agent-stack §2).

        This is the lookup that makes a dynamic subgraph findable at all: the
        parent's own graph inspection has no static edge to it.
        """
        with self._sessions() as session:
            rows = session.execute(text(
                f"SELECT {self._COLUMNS} FROM run_mappings "
                "WHERE parent_run_id = :parent ORDER BY created_at"),
                {"parent": parent_run_id}).all()
        return [self._row_to_mapping(row) for row in rows]

    def resumable(self) -> list[RunMapping]:
        """Runs a restarted process should reconcile."""
        with self._sessions() as session:
            rows = session.execute(text(
                "SELECT id FROM run_mappings WHERE status IN "
                "('running', 'awaiting_approval', 'paused')")).all()
        return [m for m in (self.get(r[0]) for r in rows) if m]

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def set_status(self, run_id: str, status: RunStatus, *,
                   reason: str = "") -> None:
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE run_mappings SET status = :status, pause_reason = :reason, "
                "updated_at = :now WHERE id = :id"),
                {"status": status.value, "reason": reason, "now": now,
                 "id": run_id})
            session.commit()

    def set_checkpoint(self, run_id: str, checkpoint_ref: str) -> None:
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE run_mappings SET checkpoint_ref = :ref, updated_at = :now "
                "WHERE id = :id"),
                {"ref": checkpoint_ref, "now": now, "id": run_id})
            session.commit()

    def cancel(self, run_id: str) -> bool:
        """Cancel a run. Checked again on resume, never assumed (LG09)."""
        mapping = self.get(run_id)
        if mapping is None or mapping.status.is_terminal:
            return False
        self.set_status(run_id, RunStatus.CANCELLED, reason="cancelled by owner")
        return True

    # ------------------------------------------------------------------ #
    # Version pinning (LG10)
    # ------------------------------------------------------------------ #
    def check_versions(self, run_id: str, *, available_packs: dict[str, str],
                       available_graph_versions: set[str]) -> RunStatus:
        """Verify a paused run can still resume into what it was pinned to.

        An unavailable version pauses with an actionable status. Deserialising
        into a *different* graph would silently change the meaning of state the
        run captured earlier.
        """
        mapping = self.get(run_id)
        if mapping is None:
            raise InvalidInput(f"Unknown run {run_id!r}")

        missing: list[str] = []
        if mapping.graph_version not in available_graph_versions:
            missing.append(f"graph {mapping.graph_version}")
        for pack_id, version in mapping.pinned_packs.items():
            if available_packs.get(pack_id) != version:
                missing.append(f"{pack_id}@{version}")

        if missing:
            self.set_status(run_id, RunStatus.VERSION_UNAVAILABLE,
                            reason="pinned versions unavailable: "
                                   + ", ".join(missing))
            return RunStatus.VERSION_UNAVAILABLE
        return mapping.status

    def require_resumable(self, run_id: str, *,
                          disabled_packs: set[str] | None = None) -> RunMapping:
        """Raise unless this run may continue right now (LG09, LG10).

        Rechecked at resume time rather than trusted from the pre-pause state:
        the whole point of a pause is that the world may have changed during it.
        """
        mapping = self.get(run_id)
        if mapping is None:
            raise InvalidInput(f"Unknown run {run_id!r}")
        if mapping.status is RunStatus.CANCELLED:
            raise Conflict("This run was cancelled and will not resume.",
                           details={"run_id": run_id})
        if mapping.status is RunStatus.VERSION_UNAVAILABLE:
            raise Unavailable(
                f"Run is paused: {mapping.pause_reason}",
                details={"run_id": run_id, "pinned": mapping.pinned_packs})
        if not mapping.status.is_resumable:
            raise Conflict(f"Run is {mapping.status.value} and cannot resume.",
                           details={"run_id": run_id})

        blocked = set(disabled_packs or set()) & set(mapping.pinned_packs)
        if blocked:
            raise Unavailable(
                "A capability this run depends on has been disabled.",
                details={"run_id": run_id, "disabled": sorted(blocked)})
        return mapping


# --------------------------------------------------------------------------- #
# Checkpoint storage (LG11)
# --------------------------------------------------------------------------- #
def checkpoint_path(data_dir: Path) -> Path:
    return Path(data_dir) / CHECKPOINT_FILENAME


def secure_checkpoint_file(path: Path) -> None:
    """Restrict a checkpoint database to the owner.

    Checkpoints can contain private content, so world-readable permissions
    would defeat the privacy label on everything inside them.
    """
    try:
        if path.exists():
            os.chmod(path, CHECKPOINT_MODE)
    except OSError:  # pragma: no cover - platform dependent
        logger.warning("Could not restrict permissions on %s", path)


@dataclass
class BackupManifest:
    """Describes a paired backup of both databases (LG11)."""

    created_at: int
    domain_hash: str
    checkpoint_hash: str
    domain_file: str
    checkpoint_file: str
    schema_revision: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"created_at": self.created_at,
                "domain_hash": self.domain_hash,
                "checkpoint_hash": self.checkpoint_hash,
                "domain_file": self.domain_file,
                "checkpoint_file": self.checkpoint_file,
                "schema_revision": self.schema_revision}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> BackupManifest:
        return cls(created_at=int(payload["created_at"]),
                   domain_hash=str(payload["domain_hash"]),
                   checkpoint_hash=str(payload["checkpoint_hash"]),
                   domain_file=str(payload["domain_file"]),
                   checkpoint_file=str(payload["checkpoint_file"]),
                   schema_revision=str(payload.get("schema_revision", "")))


class PairedBackup:
    """Backs up and restores the domain and checkpoint databases together.

    Backing up one without the other produces a restore where execution
    position and business records disagree — a run that believes it is mid-flight
    against a database that never saw it, or vice versa. The manifest records
    both hashes so a mismatched pair is detected before it is restored.
    """

    def __init__(self, *, domain_path: Path, checkpoint_path: Path,
                 clock: Clock | None = None) -> None:
        self.domain_path = Path(domain_path)
        self.checkpoint_path = Path(checkpoint_path)
        self._clock = clock or SystemClock()

    def backup(self, target_dir: Path, *, schema_revision: str = "") -> BackupManifest:
        """Copy both databases and write a manifest describing the pair."""
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)

        domain_copy = target / self.domain_path.name
        checkpoint_copy = target / self.checkpoint_path.name

        shutil.copy2(self.domain_path, domain_copy)
        if self.checkpoint_path.exists():
            shutil.copy2(self.checkpoint_path, checkpoint_copy)
        else:
            checkpoint_copy.write_bytes(b"")
        secure_checkpoint_file(checkpoint_copy)

        manifest = BackupManifest(
            created_at=to_micros(self._clock.now()),
            domain_hash=content_hash(domain_copy.read_bytes()),
            checkpoint_hash=content_hash(checkpoint_copy.read_bytes()),
            domain_file=domain_copy.name,
            checkpoint_file=checkpoint_copy.name,
            schema_revision=schema_revision)
        (target / "manifest.json").write_text(
            json.dumps(manifest.to_json(), indent=2), encoding="utf-8")
        return manifest

    def verify(self, source_dir: Path) -> BackupManifest:
        """Check a backup's contents against its manifest before restoring."""
        source = Path(source_dir)
        manifest_file = source / "manifest.json"
        if not manifest_file.is_file():
            raise InvalidInput(f"No backup manifest in {source}")

        manifest = BackupManifest.from_json(
            json.loads(manifest_file.read_text(encoding="utf-8")))

        problems: list[str] = []
        domain = source / manifest.domain_file
        checkpoint = source / manifest.checkpoint_file
        if not domain.is_file():
            problems.append("domain database missing")
        elif content_hash(domain.read_bytes()) != manifest.domain_hash:
            problems.append("domain database hash mismatch")
        if not checkpoint.is_file():
            problems.append("checkpoint database missing")
        elif content_hash(checkpoint.read_bytes()) != manifest.checkpoint_hash:
            problems.append("checkpoint database hash mismatch")

        if problems:
            raise Conflict("Backup is inconsistent and will not be restored.",
                           details={"problems": problems})
        return manifest

    def restore(self, source_dir: Path, *, dry_run: bool = True
                ) -> dict[str, Any]:
        """Restore both databases, verifying consistency first."""
        manifest = self.verify(source_dir)
        if dry_run:
            return {"dry_run": True, "manifest": manifest.to_json(),
                    "would_restore": [str(self.domain_path),
                                      str(self.checkpoint_path)]}

        source = Path(source_dir)
        self.domain_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / manifest.domain_file, self.domain_path)
        shutil.copy2(source / manifest.checkpoint_file, self.checkpoint_path)
        secure_checkpoint_file(self.checkpoint_path)
        return {"dry_run": False, "manifest": manifest.to_json(),
                "restored": [str(self.domain_path), str(self.checkpoint_path)]}


# --------------------------------------------------------------------------- #
# Forgetting (LG11)
# --------------------------------------------------------------------------- #
def runs_affected_by(store: RunStore, *, origin: str) -> list[RunMapping]:
    """Runs whose privacy label carries a given origin.

    Forgetting a source must invalidate the runs derived from it; leaving a
    checkpoint holding the content that was just deleted defeats the deletion.
    """
    return [mapping for mapping in store.resumable()
            if origin in mapping.privacy.origins]
