"""Shared mutation semantics for CLI, bot and HTTP (interfaces §5, O07, O08).

The rule this enforces is that a surface is a *transport*, not a behaviour. If
the HTTP route and the Telegram button reach the same service, they cannot drift
apart; if each implements "complete a task" itself, they will, and the one used
less often will be the one that is wrong.

Three semantics every mutation needs, in one place:

* **Idempotency.** Networks retry. A repeated request with the same key returns
  the first result rather than acting twice — a duplicated "send" is not
  recoverable by apologising afterwards.
* **Conflict.** `expected_version` mismatches are refused, so a stale form
  cannot silently overwrite a newer edit.
* **Not found.** A missing ID is 404 and stays 404, rather than being created
  helpfully.

HTML forms additionally need CSRF protection and a 303 redirect after a
successful POST, so a browser refresh does not repeat the mutation.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from loop.core.errors import Conflict, InvalidInput
from loop.core.ids import content_hash

logger = logging.getLogger(__name__)


class Outcome(str, Enum):
    APPLIED = "applied"
    REPLAYED = "replayed"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"

    @property
    def http_status(self) -> int:
        return {"applied": 200, "replayed": 200, "conflict": 409,
                "not_found": 404}[self.value]


@dataclass
class MutationResult:
    outcome: Outcome
    body: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.APPLIED, Outcome.REPLAYED)


@dataclass
class _Record:
    request_hash: str
    result: MutationResult


class IdempotencyStore:
    """Remembers what each idempotency key produced.

    The stored request hash matters: the same key with a *different* body is a
    client bug, and returning the first result for it would quietly discard the
    second request. Better to say so.
    """

    def __init__(self) -> None:
        self._records: dict[str, _Record] = {}

    def lookup(self, key: str, *, request: dict[str, Any]) -> MutationResult | None:
        record = self._records.get(key)
        if record is None:
            return None
        if record.request_hash != content_hash(repr(sorted(request.items()))):
            raise InvalidInput(
                "This idempotency key was already used for a different request.",
                details={"idempotency_key": key})
        return MutationResult(Outcome.REPLAYED, dict(record.result.body))

    def remember(self, key: str, *, request: dict[str, Any],
                 result: MutationResult) -> None:
        self._records[key] = _Record(
            content_hash(repr(sorted(request.items()))), result)


class ApplicationService:
    """One implementation of each mutation, shared by every surface."""

    def __init__(self) -> None:
        self.idempotency = IdempotencyStore()
        self._objects: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # Objects
    # ------------------------------------------------------------------ #
    def put(self, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = {"id": object_id, "version": 1, **payload}
        self._objects[object_id] = record
        return record

    def get(self, object_id: str) -> dict[str, Any] | None:
        return self._objects.get(object_id)

    # ------------------------------------------------------------------ #
    # Mutation
    # ------------------------------------------------------------------ #
    def mutate(self, object_id: str, *, expected_version: int,
               changes: dict[str, Any], idempotency_key: str = "",
               apply: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
               ) -> MutationResult:
        """Apply one change with idempotency, conflict and not-found handling."""
        request = {"object_id": object_id, "expected_version": expected_version,
                   "changes": repr(sorted(changes.items()))}

        if idempotency_key:
            replay = self.idempotency.lookup(idempotency_key, request=request)
            if replay is not None:
                return replay

        record = self._objects.get(object_id)
        if record is None:
            return MutationResult(Outcome.NOT_FOUND, error=f"no object {object_id!r}")

        if record["version"] != expected_version:
            return MutationResult(
                Outcome.CONFLICT,
                body={"current_version": record["version"]},
                error="the object changed since you read it")

        if apply is not None:
            apply(record, changes)
        else:
            record.update(changes)
        record["version"] += 1

        result = MutationResult(Outcome.APPLIED, body=dict(record))
        if idempotency_key:
            self.idempotency.remember(idempotency_key, request=request,
                                      result=result)
        return result


# --------------------------------------------------------------------------- #
# HTML forms (O07)
# --------------------------------------------------------------------------- #
class CsrfError(RuntimeError):
    pass


def issue_csrf_token(session_id: str, *, secret: str) -> str:
    """A token bound to the session, so another site's form cannot forge one."""
    return hmac.new(secret.encode(), session_id.encode(), "sha256").hexdigest()


def verify_csrf_token(token: str, *, session_id: str, secret: str) -> None:
    expected = issue_csrf_token(session_id, secret=secret)
    if not hmac.compare_digest(token or "", expected):
        raise CsrfError("The form token is missing or does not match this session.")


#: A successful HTML form POST redirects, so refresh does not repeat it.
POST_REDIRECT_STATUS = 303


def form_response_status(result: MutationResult) -> int:
    """HTML forms redirect on success and report status otherwise (O07)."""
    if result.outcome is Outcome.APPLIED:
        return POST_REDIRECT_STATUS
    if result.outcome is Outcome.REPLAYED:
        return POST_REDIRECT_STATUS
    return result.outcome.http_status


def require_authentication(token: str | None, *, expected: str) -> None:
    """Every mutating surface authenticates, including the local web UI.

    "It only listens on localhost" is not authentication: anything running on
    the machine, including a page open in the browser, can reach it.
    """
    if not token or not hmac.compare_digest(token, expected):
        raise Conflict("Authentication is required for this request.",
                       details={"reason": "unauthenticated"})
