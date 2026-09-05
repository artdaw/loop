"""Identifier and hash helpers (runtime contract §3)."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def new_id() -> str:
    """A UUID4 identifier."""
    return str(uuid.uuid4())


def content_hash(data: bytes | str) -> str:
    """SHA-256 hex digest of content."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def input_hash(payload: Any) -> str:
    """Stable hash of a JSON-serialisable payload.

    Keys are sorted so that two structurally identical inputs always produce
    the same hash. Operations use this to bind an approval to the exact input
    it was granted for: re-hashing a changed payload no longer matches, so a
    stale approval cannot authorise a different effect.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, default=str)
    return content_hash(encoded)
