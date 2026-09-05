"""Authenticated event intake (runtime §2 steps 1–2, §5; interfaces §2).

The first stage of the runtime cycle: accept an event, verify it came from the
owner, deduplicate it, and **commit it before acknowledging**. Committing first
is what makes an acknowledgement honest — if the process dies immediately after
replying, the event is already durable and the work still happens.

Three failure modes this guards against:

* **Replay.** Telegram redelivers updates. ``UNIQUE(origin, idempotency_key)``
  collapses them to one row, and the caller learns from ``created`` whether this
  delivery is the one that should produce a user-visible acknowledgement (T02).
* **Key reuse with a different body.** The same idempotency key with different
  content is a client bug or an attack, never a retry. It is a conflict with no
  second effect, never a silent overwrite (T15).
* **Impersonation.** For Telegram, *both* the chat and the sender's user ID must
  match the bound owner. An unbound instance refuses everyone rather than
  adopting whoever speaks first (interfaces §2).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.errors import AuthRequired, Conflict
from loop.core.ids import input_hash, new_id
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.core.settings import Settings

logger = logging.getLogger(__name__)

#: Origins that carry an external identity needing verification. Anything else
#: (the CLI, internal timers) already runs as the authenticated owner.
_IDENTITY_CHECKED_ORIGINS = frozenset({"telegram"})

#: Default destination for owner-facing replies.
OWNER_TELEGRAM = "owner:telegram"


@dataclass
class InboundMessage:
    """One inbound delivery, before it becomes a stored event."""

    origin: str
    origin_id: str
    idempotency_key: str
    kind: str = "message.received"
    payload: dict[str, Any] = field(default_factory=dict)
    actor_chat_id: str | None = None
    actor_user_id: str | None = None
    occurred_at: int | None = None
    #: A privacy label supplied by the caller. It may *narrow* the derived
    #: label; it can never widen it.
    claimed_privacy: dict[str, Any] | None = None


@dataclass(frozen=True)
class AcceptResult:
    """The outcome of accepting a delivery."""

    event_id: str
    accepted: bool
    created: bool
    received_at: int
    privacy: PrivacyLabel

    @property
    def duplicate(self) -> bool:
        """True when this delivery was a replay of an already-stored event."""
        return self.accepted and not self.created


class EventIntake:
    """Accepts, authenticates and deduplicates inbound events."""

    def __init__(self, *, sessions: sessionmaker[Session], settings: Settings,
                 clock: Clock | None = None) -> None:
        self._sessions = sessions
        self._settings = settings
        self._clock = clock or SystemClock()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def accept(self, message: InboundMessage) -> AcceptResult:
        """Authenticate, deduplicate and durably store one delivery."""
        self._verify_identity(message)

        now = to_micros(self._clock.now())
        occurred = message.occurred_at if message.occurred_at is not None else now
        privacy = self._derive_privacy(message)
        body_hash = input_hash(message.payload)

        existing = self._find_existing(message)
        if existing is not None:
            return self._resolve_duplicate(message, existing, body_hash, privacy)

        event_id = new_id()
        try:
            with self._sessions() as session:
                session.execute(text(
                    "INSERT INTO events (id, version, created_at, updated_at, kind, "
                    "schema_version, origin, origin_id, idempotency_key, "
                    "occurred_at, received_at, root_id, causation_id, hop_count, "
                    "payload_json, state, privacy) "
                    "VALUES (:id, 1, :now, :now, :kind, 1, :origin, :origin_id, "
                    ":key, :occurred, :now, :id, NULL, 0, :payload, 'accepted', "
                    ":privacy)"
                ), {
                    "id": event_id,
                    "now": now,
                    "kind": message.kind,
                    "origin": message.origin,
                    "origin_id": message.origin_id,
                    "key": message.idempotency_key,
                    "occurred": occurred,
                    "payload": json.dumps(message.payload, sort_keys=True),
                    "privacy": json.dumps(privacy.to_json()),
                })
                session.commit()
        except IntegrityError:
            # A concurrent delivery won the race. Treat it exactly like a
            # replay: re-read and return the stored result.
            logger.info("Concurrent duplicate for %s/%s", message.origin,
                        message.idempotency_key)
            existing = self._find_existing(message)
            if existing is None:  # pragma: no cover - defensive
                raise
            return self._resolve_duplicate(message, existing, body_hash, privacy)

        return AcceptResult(event_id=event_id, accepted=True, created=True,
                            received_at=now, privacy=privacy)

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #
    def _verify_identity(self, message: InboundMessage) -> None:
        """Reject anything not provably from the bound owner."""
        if message.origin not in _IDENTITY_CHECKED_ORIGINS:
            return

        expected_chat = self._settings.telegram_chat_id.strip()
        expected_user = self._settings.telegram_user_id.strip()

        if not expected_chat or not expected_user:
            # interfaces §2: an unbound instance must not accept the first
            # arbitrary /start as its owner. Refusing everyone is the only safe
            # behaviour until pairing has happened out of band.
            raise AuthRequired(
                "This instance is not paired yet. Set TELEGRAM_CHAT_ID and "
                "TELEGRAM_USER_ID, or complete pairing, before accepting messages."
            )

        if str(message.actor_chat_id or "") != expected_chat:
            raise AuthRequired("Message rejected: unrecognised chat.")
        if str(message.actor_user_id or "") != expected_user:
            raise AuthRequired("Message rejected: unrecognised sender.")

    # ------------------------------------------------------------------ #
    # Privacy
    # ------------------------------------------------------------------ #
    def _derive_privacy(self, message: InboundMessage) -> PrivacyLabel:
        """Classify a delivery, letting a caller narrow but never widen."""
        if message.origin == "telegram":
            derived = PrivacyLabel(
                model_scope=ModelScope.LOCAL_ONLY,
                origins=frozenset({"telegram_private"}),
                allowed_destinations=frozenset({OWNER_TELEGRAM}),
                sensitive=True,
            )
        else:
            derived = PrivacyLabel(
                model_scope=ModelScope.LOCAL_ONLY,
                origins=frozenset({message.origin}),
                allowed_destinations=frozenset({OWNER_TELEGRAM}),
            )

        if message.claimed_privacy:
            return derived.downgraded_by(
                PrivacyLabel.from_json(message.claimed_privacy))
        return derived

    # ------------------------------------------------------------------ #
    # Deduplication
    # ------------------------------------------------------------------ #
    def _find_existing(self, message: InboundMessage) -> tuple[str, str, str] | None:
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT id, payload_json, privacy FROM events "
                "WHERE origin = :origin AND idempotency_key = :key"
            ), {"origin": message.origin, "key": message.idempotency_key}).first()
        return (row[0], row[1], row[2]) if row else None

    def _resolve_duplicate(self, message: InboundMessage,
                           existing: tuple[str, str, str], body_hash: str,
                           privacy: PrivacyLabel) -> AcceptResult:
        """Return the stored result, or refuse a changed body (T15)."""
        event_id, stored_payload, stored_privacy = existing
        stored_hash = input_hash(json.loads(stored_payload))

        if stored_hash != body_hash:
            # The message names the key, never the bodies: an error surfaced to
            # a client must not leak either version's content.
            raise Conflict(
                f"Idempotency key {message.idempotency_key!r} was already used "
                "with a different body.",
                details={"origin": message.origin,
                         "idempotency_key": message.idempotency_key,
                         "existing_event_id": event_id},
            )

        with self._sessions() as session:
            received = session.execute(
                text("SELECT received_at FROM events WHERE id = :id"),
                {"id": event_id}).scalar()

        return AcceptResult(
            event_id=event_id, accepted=True, created=False,
            received_at=int(received or 0),
            privacy=PrivacyLabel.from_json(json.loads(stored_privacy)),
        )
