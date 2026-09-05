"""Structured failure taxonomy (runtime contract §10).

Every failure returned to an interface carries one of these codes plus a
public-safe explanation and a correlation ID. Raw credentials, source bodies,
prompts and provider authorization headers never appear in the message.

The codes are a closed set on purpose: callers, the CLI exit-code mapping and
the HTTP error envelope all switch on them, so an ad-hoc string would silently
become an ``internal_error`` to every consumer.
"""

from __future__ import annotations

import enum
from typing import Any


class ErrorCode(str, enum.Enum):
    INVALID_INPUT = "invalid_input"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNAVAILABLE = "unavailable"
    AUTH_REQUIRED = "auth_required"
    PRIVACY_BLOCKED = "privacy_blocked"
    APPROVAL_REQUIRED = "approval_required"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    VALIDATION_FAILED = "validation_failed"
    UNKNOWN_EFFECT = "unknown_effect"
    INTERNAL_ERROR = "internal_error"


#: CLI exit codes (interfaces §3). 0 succeeded/accepted; 1 unexpected.
EXIT_CODES: dict[ErrorCode, int] = {
    ErrorCode.INVALID_INPUT: 2,
    ErrorCode.VALIDATION_FAILED: 2,
    ErrorCode.NEEDS_CLARIFICATION: 3,
    ErrorCode.APPROVAL_REQUIRED: 3,
    ErrorCode.AUTH_REQUIRED: 3,
    ErrorCode.UNAVAILABLE: 4,
    ErrorCode.RATE_LIMITED: 4,
    ErrorCode.TIMEOUT: 4,
    ErrorCode.PRIVACY_BLOCKED: 4,
    ErrorCode.BUDGET_EXHAUSTED: 4,
    ErrorCode.CONFLICT: 5,
    ErrorCode.UNKNOWN_EFFECT: 5,
    ErrorCode.INTERNAL_ERROR: 1,
}


#: HTTP status per code (interfaces §4). The API and CLI must agree on what a
#: failure means, so both mappings live beside the taxonomy rather than being
#: re-derived per interface.
HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.NEEDS_CLARIFICATION: 400,
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.PRIVACY_BLOCKED: 403,
    ErrorCode.APPROVAL_REQUIRED: 403,
    ErrorCode.CONFLICT: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.UNAVAILABLE: 503,
    ErrorCode.BUDGET_EXHAUSTED: 503,
    ErrorCode.UNKNOWN_EFFECT: 500,
    ErrorCode.INTERNAL_ERROR: 500,
}


class LoopError(Exception):
    """Base class carrying a structured code and safe details."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, *, details: dict[str, Any] | None = None,
                 correlation_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.correlation_id = correlation_id

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, 1)

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)

    def to_envelope(self, request_id: str) -> dict[str, Any]:
        """The HTTP error envelope from interfaces §4."""
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
            },
            "request_id": request_id,
        }


class InvalidInput(LoopError):
    """Malformed or unusable input."""
    code = ErrorCode.INVALID_INPUT


class NeedsClarification(LoopError):
    """A blocking detail is missing; the work is retained meanwhile."""
    code = ErrorCode.NEEDS_CLARIFICATION


class Unavailable(LoopError):
    """A required service or connector is not reachable right now."""
    code = ErrorCode.UNAVAILABLE


class AuthRequired(LoopError):
    """Credentials or pairing are missing or rejected."""
    code = ErrorCode.AUTH_REQUIRED


class PrivacyBlocked(LoopError):
    """The privacy gate forbids this route or destination."""
    code = ErrorCode.PRIVACY_BLOCKED


class ApprovalRequired(LoopError):
    """An unexpired, matching approval is needed before executing."""
    code = ErrorCode.APPROVAL_REQUIRED


class Conflict(LoopError):
    """A version, uniqueness or concurrency conflict. Never last-write-wins."""
    code = ErrorCode.CONFLICT


class RateLimited(LoopError):
    """A provider asked us to slow down."""
    code = ErrorCode.RATE_LIMITED


class Timeout(LoopError):
    """A deadline elapsed before the work completed."""
    code = ErrorCode.TIMEOUT


class BudgetExhausted(LoopError):
    """The request's model/tool/time budget is spent; results are partial."""
    code = ErrorCode.BUDGET_EXHAUSTED


class ValidationFailed(LoopError):
    """Schema or reference validation rejected the payload."""
    code = ErrorCode.VALIDATION_FAILED


class UnknownEffect(LoopError):
    """An external effect may or may not have happened; never assume either."""
    code = ErrorCode.UNKNOWN_EFFECT


class InternalError(LoopError):
    """An unexpected failure."""
    code = ErrorCode.INTERNAL_ERROR
