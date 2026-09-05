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


def _error(name: str, code: ErrorCode) -> type[LoopError]:
    return type(name, (LoopError,), {"code": code})


InvalidInput = _error("InvalidInput", ErrorCode.INVALID_INPUT)
NeedsClarification = _error("NeedsClarification", ErrorCode.NEEDS_CLARIFICATION)
Unavailable = _error("Unavailable", ErrorCode.UNAVAILABLE)
AuthRequired = _error("AuthRequired", ErrorCode.AUTH_REQUIRED)
PrivacyBlocked = _error("PrivacyBlocked", ErrorCode.PRIVACY_BLOCKED)
ApprovalRequired = _error("ApprovalRequired", ErrorCode.APPROVAL_REQUIRED)
Conflict = _error("Conflict", ErrorCode.CONFLICT)
RateLimited = _error("RateLimited", ErrorCode.RATE_LIMITED)
Timeout = _error("Timeout", ErrorCode.TIMEOUT)
BudgetExhausted = _error("BudgetExhausted", ErrorCode.BUDGET_EXHAUSTED)
ValidationFailed = _error("ValidationFailed", ErrorCode.VALIDATION_FAILED)
UnknownEffect = _error("UnknownEffect", ErrorCode.UNKNOWN_EFFECT)
InternalError = _error("InternalError", ErrorCode.INTERNAL_ERROR)
