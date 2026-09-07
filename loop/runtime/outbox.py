"""Notification queue and delivery outbox (runtime §8).

Delivery truth is the point of this module. Three states are kept strictly apart
because conflating them is how an assistant starts lying:

* **queued** — committed locally. Nothing has been sent.
* **sent** — the provider acknowledged it. There is a receipt.
* **unknown** — we asked the provider and never learned the outcome. The message
  may or may not have arrived.

``unknown`` is a first-class terminal-ish state, not a retry cue. Exactly-once
external delivery is not achievable over a transport that can accept a request
and then drop the response, so on an uncertain send Loop records `unknown`,
surfaces it, and does **not** resend blindly (D06). Resending would risk a
duplicate message to the owner; claiming "sent" would be a false statement about
the world. Only a provider with real idempotency keys may safely retry.

The notification and its outbox row are written **in the same transaction as the
result that caused them** (D04), so a crash between the state change and the
dispatch cannot lose the notification — on restart it is simply still pending.

Before any send, the outbox re-checks the current state of the subject
(`preflight`). A reminder for a task completed thirty seconds ago must not go out
just because it was already queued (T08, D11).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.errors import ErrorCode
from loop.core.ids import new_id
from loop.runtime.jobs import RETRY_DELAYS, retry_delay

logger = logging.getLogger(__name__)

#: Notification categories (runtime §8). Only the executor assigns these.
CATEGORIES = frozenset({"reply", "reminder", "requested_routine", "discretionary",
                        "digest", "approval", "health"})

#: Categories that are never discretionary and always honour their time.
NON_DISCRETIONARY = frozenset({"reply", "reminder", "approval", "requested_routine"})


class SendOutcome(str):
    """Marker type for readability in the transport protocol."""


@dataclass(frozen=True)
class SendResult:
    """What a transport reports after attempting a send."""

    status: str                       # delivered | rejected | unknown
    receipt: str | None = None        # provider message id, when acknowledged
    error_code: ErrorCode | None = None
    retryable: bool = False


class Transport(Protocol):
    """A delivery channel. Injected, never constructed inside the outbox."""

    def send(self, *, channel: str, payload: dict[str, Any],
             dedupe_key: str) -> SendResult:
        ...


@dataclass
class Notification:
    """A delivery candidate, before and after it is sent."""

    id: str
    category: str
    subject_ref: str
    occurrence_key: str
    destination_id: str
    payload: dict[str, Any]
    state: str
    not_before: int
    expires_at: int | None = None
    acknowledgement_at: int | None = None


@dataclass
class OutboxItem:
    """One transport attempt record."""

    id: str
    notification_id: str | None
    channel: str
    payload: dict[str, Any]
    dedupe_key: str
    state: str
    attempts: int
    retry_at: int | None = None
    provider_receipt: str | None = None
    last_error_code: str | None = None


#: A preflight check returns True when the send should still proceed.
PreflightCheck = Callable[[Notification], bool]


class NotificationOutbox:
    """Owns notification candidates and their delivery attempts."""

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None,
                 transports: dict[str, Transport] | None = None,
                 preflight: PreflightCheck | None = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._transports = transports or {}
        self._preflight = preflight

    # ------------------------------------------------------------------ #
    # Enqueue
    # ------------------------------------------------------------------ #
    def enqueue(self, *, category: str, subject_ref: str, occurrence_key: str,
                destination_id: str, payload: dict[str, Any],
                channel: str = "telegram", not_before: int | None = None,
                expires_at: int | None = None,
                session: Session | None = None) -> str | None:
        """Queue a notification plus its outbox row.

        Pass ``session`` to commit this together with the state change that
        caused it — that shared transaction is what makes D04 hold.

        Returns ``None`` when this exact occurrence is already queued: the
        uniqueness constraint collapses duplicate candidates for one subject and
        occurrence into a single message.
        """
        if category not in CATEGORIES:
            raise ValueError(f"unknown notification category {category!r}")

        now = to_micros(self._clock.now())
        notification_id = new_id()
        outbox_id = new_id()
        # The dedupe key ties the transport attempt to the logical occurrence,
        # so a replay cannot produce a second message.
        dedupe_key = f"{category}:{subject_ref}:{occurrence_key}:{destination_id}"

        def _write(active: Session) -> str | None:
            try:
                active.execute(text(
                    "INSERT INTO notifications (id, version, created_at, "
                    "updated_at, category, subject_ref, occurrence_key, "
                    "destination_id, payload_json, sensitivity_mode, not_before, "
                    "expires_at, state) VALUES (:id, 1, :now, :now, :category, "
                    ":subject_ref, :occurrence_key, :destination_id, :payload, "
                    "'full', :not_before, :expires_at, 'pending')"
                ), {
                    "id": notification_id, "now": now, "category": category,
                    "subject_ref": subject_ref, "occurrence_key": occurrence_key,
                    "destination_id": destination_id,
                    "payload": json.dumps(payload, sort_keys=True),
                    "not_before": not_before if not_before is not None else now,
                    "expires_at": expires_at,
                })
                active.execute(text(
                    "INSERT INTO outbox (id, version, created_at, updated_at, "
                    "notification_id, channel, payload_json, dedupe_key, state, "
                    "attempts, retry_at) VALUES (:id, 1, :now, :now, :nid, "
                    ":channel, :payload, :dedupe_key, 'pending', 0, :retry_at)"
                ), {
                    "id": outbox_id, "now": now, "nid": notification_id,
                    "channel": channel,
                    "payload": json.dumps(payload, sort_keys=True),
                    "dedupe_key": dedupe_key,
                    "retry_at": not_before if not_before is not None else now,
                })
            except IntegrityError:
                logger.info("Notification for %s/%s already queued",
                            subject_ref, occurrence_key)
                return None
            return notification_id

        if session is not None:
            return _write(session)
        with self._sessions() as own:
            try:
                result = _write(own)
            except IntegrityError:
                own.rollback()
                return None
            if result is None:
                own.rollback()
                return None
            own.commit()
            return result

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def get_notification(self, notification_id: str) -> Notification | None:
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT id, category, subject_ref, occurrence_key, "
                "destination_id, payload_json, state, not_before, expires_at, "
                "acknowledgement_at FROM notifications WHERE id = :id"
            ), {"id": notification_id}).first()
        return _to_notification(row) if row else None

    def due_items(self, *, limit: int = 50) -> list[OutboxItem]:
        """Outbox rows ready for a send attempt."""
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            rows = session.execute(text(
                "SELECT id, notification_id, channel, payload_json, dedupe_key, "
                "state, attempts, retry_at, provider_receipt, last_error_code "
                "FROM outbox WHERE state IN ('pending', 'retry_wait') "
                "AND (retry_at IS NULL OR retry_at <= :now) "
                "ORDER BY retry_at LIMIT :limit"), {"now": now, "limit": limit}
            ).all()
        return [_to_outbox(row) for row in rows]

    def get_item(self, item_id: str) -> OutboxItem | None:
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT id, notification_id, channel, payload_json, dedupe_key, "
                "state, attempts, retry_at, provider_receipt, last_error_code "
                "FROM outbox WHERE id = :id"), {"id": item_id}).first()
        return _to_outbox(row) if row else None

    # ------------------------------------------------------------------ #
    # Cancellation
    # ------------------------------------------------------------------ #
    def cancel_for_subject(self, subject_ref: str, *,
                           session: Session | None = None) -> int:
        """Cancel pending notifications for a subject.

        Used when a task is completed or cancelled. Rows already ``sending`` or
        ``delivered`` are untouched: a transmitted message is history and cannot
        be recalled (T08, D11).
        """
        now = to_micros(self._clock.now())

        def _cancel(active: Session) -> int:
            result: CursorResult[Any] = active.execute(text(  # type: ignore[assignment]
                "UPDATE outbox SET state = 'cancelled', updated_at = :now, "
                "version = version + 1 WHERE state IN ('pending', 'retry_wait') "
                "AND notification_id IN (SELECT id FROM notifications "
                "WHERE subject_ref = :subject)"), {"now": now, "subject": subject_ref})
            active.execute(text(
                "UPDATE notifications SET state = 'cancelled', updated_at = :now, "
                "version = version + 1 WHERE subject_ref = :subject "
                "AND state = 'pending'"), {"now": now, "subject": subject_ref})
            return int(result.rowcount)

        if session is not None:
            return _cancel(session)
        with self._sessions() as own:
            count = _cancel(own)
            own.commit()
            return count

    # ------------------------------------------------------------------ #
    # Delivery
    # ------------------------------------------------------------------ #
    def defer_for_subject(self, subject_ref: str, *, until: int,
                          session: Session | None = None) -> int:
        """Push this subject's *undelivered* messages to a later time.

        What `/snooze` actually does. Only `pending` and `retry_wait` rows move:
        a row already `sending` may be in flight at the provider, and one
        already `delivered` is history — rescheduling either would either
        duplicate a message or pretend a sent one was not.

        The notification's own `not_before` moves with the outbox row, so a
        later policy check sees the deferred time rather than the original.
        """
        def _defer(active: Session) -> int:
            result: CursorResult[Any] = active.execute(text(  # type: ignore[assignment]
                "UPDATE outbox SET retry_at = :until, updated_at = :now, "
                "version = version + 1 "
                "WHERE state IN ('pending', 'retry_wait') "
                "AND notification_id IN (SELECT id FROM notifications "
                "WHERE subject_ref = :subject)"),
                {"until": until, "now": to_micros(self._clock.now()),
                 "subject": subject_ref})
            active.execute(text(
                "UPDATE notifications SET not_before = :until, updated_at = :now,"
                " version = version + 1 WHERE subject_ref = :subject "
                "AND state = 'pending'"),
                {"until": until, "now": to_micros(self._clock.now()),
                 "subject": subject_ref})
            return int(result.rowcount or 0)

        if session is not None:
            return _defer(session)
        with self._sessions() as own:
            moved = _defer(own)
            own.commit()
            return moved

    def dispatch(self, item: OutboxItem) -> str:
        """Attempt one delivery. Returns the resulting outbox state.

        The preflight check runs immediately before the transport call, so state
        that changed while the item sat in the queue still takes effect.
        """
        notification = (self.get_notification(item.notification_id)
                        if item.notification_id else None)

        if notification is not None:
            now = to_micros(self._clock.now())
            if notification.state == "cancelled":
                self._set_item_state(item, "cancelled")
                return "cancelled"
            if notification.expires_at is not None and notification.expires_at < now:
                self._set_item_state(item, "cancelled")
                self._set_notification_state(notification.id, "expired")
                return "cancelled"
            if self._preflight is not None and not self._preflight(notification):
                # The subject changed underneath us — the task was completed or
                # the routine paused. Suppress rather than send a stale message.
                self._set_item_state(item, "cancelled")
                self._set_notification_state(notification.id, "cancelled")
                return "cancelled"

        transport = self._transports.get(item.channel)
        if transport is None:
            return self._fail(item, ErrorCode.UNAVAILABLE, retryable=False)

        self._set_item_state(item, "sending")
        try:
            result = transport.send(channel=item.channel, payload=item.payload,
                                    dedupe_key=item.dedupe_key)
        except Exception as exc:  # noqa: BLE001 - a transport crash is unknown
            # We do not know whether the provider received it. Treat exactly
            # like an uncertain send: never resend blindly.
            logger.exception("Transport %s raised", item.channel)
            self._mark_unknown(item, str(exc))
            return "unknown"

        if result.status == "delivered":
            self._mark_delivered(item, notification, result.receipt)
            return "delivered"
        if result.status == "unknown":
            self._mark_unknown(item, None)
            return "unknown"
        return self._fail(item, result.error_code or ErrorCode.UNAVAILABLE,
                          retryable=result.retryable)

    # ------------------------------------------------------------------ #
    # State transitions
    # ------------------------------------------------------------------ #
    def _set_item_state(self, item: OutboxItem, state: str) -> None:
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE outbox SET state = :state, updated_at = :now, "
                "version = version + 1 WHERE id = :id"),
                {"state": state, "now": now, "id": item.id})
            session.commit()
        item.state = state

    def _set_notification_state(self, notification_id: str, state: str) -> None:
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE notifications SET state = :state, updated_at = :now, "
                "version = version + 1 WHERE id = :id"),
                {"state": state, "now": now, "id": notification_id})
            session.commit()

    def _mark_delivered(self, item: OutboxItem, notification: Notification | None,
                        receipt: str | None) -> None:
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE outbox SET state = 'delivered', provider_receipt = :receipt, "
                "attempts = attempts + 1, updated_at = :now, version = version + 1 "
                "WHERE id = :id"), {"receipt": receipt, "now": now, "id": item.id})
            if notification is not None:
                session.execute(text(
                    "UPDATE notifications SET state = 'sent', updated_at = :now, "
                    "version = version + 1 WHERE id = :id"),
                    {"now": now, "id": notification.id})
            session.commit()
        item.state = "delivered"
        item.provider_receipt = receipt

    def _mark_unknown(self, item: OutboxItem, detail: str | None) -> None:
        """Record an uncertain send. Never retried automatically (D06)."""
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE outbox SET state = 'unknown', attempts = attempts + 1, "
                "last_error_code = :code, retry_at = NULL, updated_at = :now, "
                "version = version + 1 WHERE id = :id"),
                {"code": ErrorCode.UNKNOWN_EFFECT.value, "now": now, "id": item.id})
            if item.notification_id:
                session.execute(text(
                    "UPDATE notifications SET state = 'unknown', updated_at = :now, "
                    "version = version + 1 WHERE id = :id"),
                    {"now": now, "id": item.notification_id})
            session.commit()
        item.state = "unknown"
        if detail:
            logger.warning("Delivery outcome unknown for %s: %s", item.id, detail)

    def _fail(self, item: OutboxItem, code: ErrorCode, *, retryable: bool) -> str:
        now = to_micros(self._clock.now())
        attempts = item.attempts + 1
        exhausted = attempts >= len(RETRY_DELAYS) + 1
        state = "retry_wait" if (retryable and not exhausted) else "failed"
        retry_at = (now + retry_delay(attempts) * 1_000_000
                    if state == "retry_wait" else None)

        with self._sessions() as session:
            session.execute(text(
                "UPDATE outbox SET state = :state, attempts = :attempts, "
                "last_error_code = :code, retry_at = :retry_at, updated_at = :now, "
                "version = version + 1 WHERE id = :id"),
                {"state": state, "attempts": attempts, "code": code.value,
                 "retry_at": retry_at, "now": now, "id": item.id})
            if state == "failed" and item.notification_id:
                session.execute(text(
                    "UPDATE notifications SET state = 'failed', updated_at = :now, "
                    "version = version + 1 WHERE id = :id"),
                    {"now": now, "id": item.notification_id})
            session.commit()
        item.state = state
        item.attempts = attempts
        return state

    def acknowledge(self, notification_id: str) -> None:
        """Record a real acknowledgement. Never inferred from silence."""
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE notifications SET acknowledgement_at = :now, "
                "updated_at = :now, version = version + 1 WHERE id = :id"),
                {"now": now, "id": notification_id})
            session.commit()


def _to_notification(row: Any) -> Notification:
    return Notification(
        id=row[0], category=row[1], subject_ref=row[2], occurrence_key=row[3],
        destination_id=row[4], payload=json.loads(row[5] or "{}"), state=row[6],
        not_before=row[7], expires_at=row[8], acknowledgement_at=row[9],
    )


def _to_outbox(row: Any) -> OutboxItem:
    return OutboxItem(
        id=row[0], notification_id=row[1], channel=row[2],
        payload=json.loads(row[3] or "{}"), dedupe_key=row[4], state=row[5],
        attempts=row[6], retry_at=row[7], provider_receipt=row[8],
        last_error_code=row[9],
    )


@dataclass
class FakeTransport:
    """A scripted transport for tests. Never touches a network."""

    results: list[SendResult] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)
    default: SendResult = field(
        default_factory=lambda: SendResult(status="delivered", receipt="msg-1"))
    raises: Exception | None = None

    def send(self, *, channel: str, payload: dict[str, Any],
             dedupe_key: str) -> SendResult:
        self.sent.append({"channel": channel, "payload": payload,
                          "dedupe_key": dedupe_key})
        if self.raises is not None:
            raise self.raises
        if self.results:
            return self.results.pop(0)
        return self.default
