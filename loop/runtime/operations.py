"""Operation ledger and approvals (runtime §10, agent-stack §4).

Every effect passes through an operation with a **stable idempotency key**
derived from persisted identifiers, not from a timestamp or a fresh UUID. That
key is what makes replay safe: after a crash the ledger is consulted, a completed
operation returns its recorded result, and the effect is not repeated (A04, LG07).

The status ladder is deliberately fine-grained:

``proposed`` → ``awaiting_approval`` → ``authorized`` → ``running`` → ``committed``

with ``failed`` and ``unknown`` as separate terminal outcomes. The distinction
between *authorized* and *committed* is the one that matters most: a gate
allowing an action is not evidence the action happened. Recording "executed"
because the check passed is how an audit log starts lying (A17).

Approvals are single-use and bound to four things at once — the operation, the
exact input hash, the actor, and an expiry. Any of them changing invalidates the
approval, so a replayed button, a different sender, or an edited payload cannot
execute (A16).

Some actions need no approval at all. A direct, authenticated "remind me at 9"
*is* the authority for that precise reversible operation; asking again because a
generic default says `approve` is friction without safety (A14).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.errors import ApprovalRequired, Conflict, InvalidInput
from loop.core.ids import content_hash, input_hash, new_id

logger = logging.getLogger(__name__)

#: How long an approval stays valid unless the action expires sooner.
DEFAULT_APPROVAL_TTL_SECONDS = 24 * 3600

#: Actions a direct authenticated request already authorises (A14). These are
#: precise, reversible, and affect only the owner's own local state.
SELF_AUTHORISING_ACTIONS = frozenset({
    "task.create", "task.update", "task.complete",
    "reminder.schedule", "reminder.snooze", "reminder.cancel",
    "vault.capture", "preference.record",
})

TERMINAL_STATUSES = frozenset({"committed", "failed", "cancelled"})


def operation_key(*, root_id: str, work_item_id: str, step_id: str,
                  action: str) -> str:
    """A stable key derived from persisted identifiers.

    Deliberately excludes the clock and any random value: a replay after a crash
    must reproduce the *same* key, or the ledger cannot recognise the operation
    it already performed.
    """
    return content_hash(f"{root_id}|{work_item_id}|{step_id}|{action}")[:32]


@dataclass
class Operation:
    """One proposed or performed effect."""

    id: str
    action: str
    target: str
    input_hash: str
    idempotency_key: str
    status: str = "proposed"
    authority_ref: str | None = None
    policy_revision: str | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def committed(self) -> bool:
        return self.status == "committed"


@dataclass
class Approval:
    """Single-use authority bound to an operation, payload, actor and expiry."""

    id: str
    operation_id: str
    input_hash: str
    destination_id: str
    expires_at: int
    actor_id: str | None = None
    resolved_at: int | None = None
    resolution: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.resolution is not None


class OperationLedger:
    """Records operations and enforces approval binding."""

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()

    # ------------------------------------------------------------------ #
    # Proposing
    # ------------------------------------------------------------------ #
    def propose(self, *, action: str, target: str, payload: dict[str, Any],
                idempotency_key: str, authority_ref: str | None = None,
                policy_revision: str | None = None) -> Operation:
        """Record an intended effect, or return the existing one.

        Returning the existing operation — rather than raising — is what makes
        replay after a crash a no-op instead of an error (LG07).
        """
        digest = input_hash(payload)
        existing = self.get_by_key(idempotency_key)
        if existing is not None:
            if existing.input_hash != digest:
                raise Conflict(
                    "This operation key was already used with a different payload.",
                    details={"idempotency_key": idempotency_key,
                             "action": action})
            return existing

        operation = Operation(
            id=new_id(), action=action, target=target, input_hash=digest,
            idempotency_key=idempotency_key,
            status="authorized" if action in SELF_AUTHORISING_ACTIONS
            else "proposed",
            authority_ref=authority_ref, policy_revision=policy_revision)

        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO operations (id, version, created_at, updated_at, "
                "action, target, input_hash, expected_version_or_hash, "
                "policy_revision, authority_ref, status, idempotency_key, "
                "result_json, error_code) VALUES (:id, 1, :now, :now, :action, "
                ":target, :input_hash, NULL, :policy_revision, :authority_ref, "
                ":status, :key, NULL, NULL)"),
                {"id": operation.id, "now": now, "action": action,
                 "target": target, "input_hash": digest,
                 "policy_revision": policy_revision,
                 "authority_ref": authority_ref, "status": operation.status,
                 "key": idempotency_key})
            session.commit()
        return operation

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def get(self, operation_id: str) -> Operation | None:
        return self._fetch("id = :value", operation_id)

    def get_by_key(self, idempotency_key: str) -> Operation | None:
        return self._fetch("idempotency_key = :value", idempotency_key)

    def _fetch(self, clause: str, value: str) -> Operation | None:
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT id, action, target, input_hash, idempotency_key, status, "
                "authority_ref, policy_revision, result_json, error_code "
                f"FROM operations WHERE {clause}"), {"value": value}).first()
        if row is None:
            return None
        return Operation(
            id=row[0], action=row[1], target=row[2], input_hash=row[3],
            idempotency_key=row[4], status=row[5], authority_ref=row[6],
            policy_revision=row[7],
            result=json.loads(row[8]) if row[8] else None, error_code=row[9])

    # ------------------------------------------------------------------ #
    # Approval
    # ------------------------------------------------------------------ #
    def request_approval(self, operation: Operation, *, destination_id: str,
                         actor_id: str | None = None,
                         ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS
                         ) -> Approval:
        """Create a single-use approval bound to this exact operation."""
        now = to_micros(self._clock.now())
        approval = Approval(
            id=new_id(), operation_id=operation.id,
            input_hash=operation.input_hash, destination_id=destination_id,
            expires_at=now + ttl_seconds * 1_000_000, actor_id=actor_id)

        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO approvals (id, version, created_at, updated_at, "
                "operation_id, input_hash, destination_id, expires_at, "
                "resolved_at, resolution, actor_id) VALUES (:id, 1, :now, :now, "
                ":operation_id, :input_hash, :destination_id, :expires_at, "
                "NULL, NULL, :actor_id)"),
                {"id": approval.id, "now": now, "operation_id": operation.id,
                 "input_hash": operation.input_hash,
                 "destination_id": destination_id,
                 "expires_at": approval.expires_at, "actor_id": actor_id})
            session.execute(text(
                "UPDATE operations SET status = 'awaiting_approval', "
                "updated_at = :now, version = version + 1 WHERE id = :id"),
                {"now": now, "id": operation.id})
            session.commit()
        return approval

    def get_approval(self, approval_id: str) -> Approval | None:
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT id, operation_id, input_hash, destination_id, "
                "expires_at, actor_id, resolved_at, resolution "
                "FROM approvals WHERE id = :id"), {"id": approval_id}).first()
        if row is None:
            return None
        return Approval(id=row[0], operation_id=row[1], input_hash=row[2],
                        destination_id=row[3], expires_at=row[4],
                        actor_id=row[5], resolved_at=row[6], resolution=row[7])

    def resolve_approval(self, approval_id: str, *, resolution: str,
                         actor_id: str, current_input_hash: str) -> Approval:
        """Approve or reject, revalidating every binding first (A16).

        All four checks happen here rather than at the call site, because a call
        site that forgets one is indistinguishable from one that does not.
        """
        approval = self.get_approval(approval_id)
        if approval is None:
            raise InvalidInput(f"No approval {approval_id!r}")

        if approval.is_resolved:
            # A replayed button is not a second decision. The existing outcome
            # is returned, so no second effect can follow.
            logger.info("Approval %s already resolved as %s", approval_id,
                        approval.resolution)
            return approval

        now = to_micros(self._clock.now())
        if now > approval.expires_at:
            raise ApprovalRequired(
                "This approval has expired; request a fresh one.",
                details={"approval_id": approval_id})
        if approval.actor_id is not None and approval.actor_id != actor_id:
            raise ApprovalRequired(
                "This approval belongs to a different person.",
                details={"approval_id": approval_id})
        if approval.input_hash != current_input_hash:
            raise Conflict(
                "The action changed since it was shown for approval.",
                details={"approval_id": approval_id})

        with self._sessions() as session:
            session.execute(text(
                "UPDATE approvals SET resolution = :resolution, "
                "resolved_at = :now, updated_at = :now, version = version + 1 "
                "WHERE id = :id"),
                {"resolution": resolution, "now": now, "id": approval_id})
            status = "authorized" if resolution == "approved" else "cancelled"
            session.execute(text(
                "UPDATE operations SET status = :status, updated_at = :now, "
                "version = version + 1 WHERE id = :id"),
                {"status": status, "now": now, "id": approval.operation_id})
            session.commit()

        approval.resolution = resolution
        approval.resolved_at = now
        return approval

    # ------------------------------------------------------------------ #
    # Execution lifecycle
    # ------------------------------------------------------------------ #
    def require_authorized(self, operation: Operation) -> None:
        """Raise unless this operation may execute now."""
        current = self.get(operation.id)
        if current is None:
            raise InvalidInput(f"Unknown operation {operation.id!r}")
        if current.status != "authorized":
            raise ApprovalRequired(
                f"Operation is {current.status!r}; it is not authorised to run.",
                details={"operation_id": operation.id, "status": current.status})

    def mark_running(self, operation: Operation) -> None:
        self._set_status(operation.id, "running")

    def mark_committed(self, operation: Operation, *,
                       result: dict[str, Any]) -> None:
        """Record success — only after the effect is actually verified (A17)."""
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE operations SET status = 'committed', result_json = :result, "
                "updated_at = :now, version = version + 1 WHERE id = :id"),
                {"result": json.dumps(result, sort_keys=True), "now": now,
                 "id": operation.id})
            session.commit()

    def mark_failed(self, operation: Operation, *, error_code: str) -> None:
        self._set_status(operation.id, "failed", error_code=error_code)

    def mark_unknown(self, operation: Operation, *,
                     error_code: str = "unknown_effect") -> None:
        """The effect may or may not have happened. Never guess either way."""
        self._set_status(operation.id, "unknown", error_code=error_code)

    def _set_status(self, operation_id: str, status: str,
                    error_code: str | None = None) -> None:
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE operations SET status = :status, error_code = :code, "
                "updated_at = :now, version = version + 1 WHERE id = :id"),
                {"status": status, "code": error_code, "now": now,
                 "id": operation_id})
            session.commit()

    # ------------------------------------------------------------------ #
    # Replay
    # ------------------------------------------------------------------ #
    def replay(self, idempotency_key: str) -> Operation | None:
        """Return a completed operation's recorded result, if any (LG07).

        Consulted before performing an effect after a restart: a committed
        operation is reused, never repeated.
        """
        existing = self.get_by_key(idempotency_key)
        if existing is None or not existing.committed:
            return None
        return existing
