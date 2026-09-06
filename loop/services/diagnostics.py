"""Diagnostic export and log redaction (runtime §10, agent-stack §5).

Diagnostics exist to be shared — pasted into an issue, sent to whoever is
helping. That is exactly why the export is built by *allowing* named fields
rather than by stripping known-bad ones: a denylist fails open, and the first
field someone adds without thinking about it ships the user's message bodies to
a bug tracker.

So this module answers "what may leave" with a fixed list of metadata keys.
Anything not on it is dropped, whatever it is called.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: The only keys a diagnostic export may carry (runtime §10 audit contract).
ALLOWED_KEYS = frozenset({
    "actor", "event_id", "operation_id", "run_id", "action", "decision",
    "reason_code", "target_id", "backend", "latency_ms", "model_calls",
    "total_tokens", "prompt_hash", "input_hash", "schema_version",
    "graph_version", "pack_version", "health", "error_code", "queue_depth",
    "connector_id", "state", "attempt", "duration_ms", "timestamp", "count",
})

#: Substrings that mark a key as content- or credential-bearing.
FORBIDDEN_HINTS = ("prompt", "body", "message", "content", "text", "token",
                   "secret", "key", "password", "authorization", "cookie",
                   "profile", "email", "address", "chat_id", "credential")

_ALLOWED_HASH_KEYS = {"prompt_hash", "input_hash"}

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"(?i)\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
)


def is_allowed_key(key: str) -> bool:
    """A key must be explicitly allowed and free of content-bearing hints.

    `prompt_hash` is allowed although it contains "prompt": a hash identifies a
    call without revealing it, which is the whole point of recording one.
    """
    if key in _ALLOWED_HASH_KEYS:
        return True
    lowered = key.lower()
    if any(hint in lowered for hint in FORBIDDEN_HINTS):
        return False
    return key in ALLOWED_KEYS


def redact_value(value: Any) -> Any:
    """Mask anything that looks like a credential or an address in free text."""
    if not isinstance(value, str):
        return value
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def sanitize(record: dict[str, Any]) -> dict[str, Any]:
    """Keep only allowed metadata, redacting what survives."""
    return {key: redact_value(value) for key, value in sorted(record.items())
            if is_allowed_key(key)}


@dataclass
class DiagnosticExport:
    records: list[dict[str, Any]] = field(default_factory=list)
    dropped_keys: list[str] = field(default_factory=list)

    @property
    def key_set(self) -> set[str]:
        return {key for record in self.records for key in record}


def build_export(records: list[dict[str, Any]]) -> DiagnosticExport:
    """Build a shareable diagnostic export (A21)."""
    export = DiagnosticExport()
    dropped: set[str] = set()

    for record in records:
        cleaned = sanitize(record)
        dropped.update(key for key in record if key not in cleaned)
        export.records.append(cleaned)

    export.dropped_keys = sorted(dropped)
    return export
