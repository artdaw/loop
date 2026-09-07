"""Buttons on a delivered message, and what pressing one is allowed to do.

interfaces §2 is precise about inline buttons, and every clause is about not
trusting the callback: *"Callback data uses compact opaque IDs; look up the
current operation and validate actor, expiry, subject state, and input hash.
Callback acknowledgement is not execution success. 'Dismiss' suppresses a
notification; it does not complete the underlying task."*

Callback data is attacker-controllable in the same sense a URL is — it comes
back from a client, and Telegram will deliver whatever is in it. So the button
carries **only an opaque id**. Everything the action needs is stored here and
looked up: which task, which occurrence, who may press it, and until when.
Encoding `task_id` and `snooze:60` into the button and acting on them would let
a replayed or edited callback act on a different task entirely.

Four checks on every press, each closing a specific hole:

* **Unknown id** — a fabricated or long-expired button.
* **Wrong actor** — the message may have been forwarded; only the owner it was
  issued for may act on it.
* **Expired** — a button from last week should not still snooze something.
* **Already consumed** — Telegram redelivers, and users double-tap. One press
  is one effect (T09's "one replacement occurrence").
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session, sessionmaker

from loop.core.clock import Clock, SystemClock, from_micros, to_micros
from loop.core.ids import new_id

logger = logging.getLogger(__name__)

#: How long a button stays live. A reminder's buttons are for now, not forever.
DEFAULT_TTL_HOURS = 48


class ActionKind(str, Enum):
    DONE = "done"
    SNOOZE = "snooze"
    DISMISS = "dismiss"


@dataclass(frozen=True)
class CallbackAction:
    """One issued button."""

    id: str
    kind: ActionKind
    subject_ref: str
    task_id: str
    occurrence_key: str
    actor: str
    expires_at: int
    consumed_at: int | None = None

    @property
    def consumed(self) -> bool:
        return self.consumed_at is not None


class ActionRefused(RuntimeError):
    """A press that must not take effect, with a reason worth showing."""


class CallbackActions:
    """Issues opaque button ids and rules on presses."""

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self._sessions() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS callback_actions ("
                " id TEXT PRIMARY KEY,"
                " kind TEXT NOT NULL,"
                " subject_ref TEXT NOT NULL,"
                " task_id TEXT NOT NULL,"
                " occurrence_key TEXT NOT NULL,"
                " actor TEXT NOT NULL,"
                " expires_at INTEGER NOT NULL,"
                " consumed_at INTEGER,"
                " created_at INTEGER NOT NULL)"))
            session.commit()

    def issue(self, *, kinds: list[ActionKind], subject_ref: str, task_id: str,
              occurrence_key: str, actor: str,
              ttl_hours: int = DEFAULT_TTL_HOURS,
              session: Session | None = None) -> dict[str, str]:
        """Mint one opaque id per button. Returns ``{kind: id}``.

        Pass ``session`` to commit the buttons with the notification that
        carries them. That is not only tidiness: opening a second connection
        inside the caller's write transaction deadlocks SQLite, and a button
        committed without its message would be pressable while referring to a
        reminder nobody ever received.
        """
        now = self._clock.now()
        expires = to_micros(now + dt.timedelta(hours=ttl_hours))
        issued: dict[str, str] = {}

        def _write(active: Session) -> None:
            for kind in kinds:
                action_id = new_id()
                active.execute(text(
                    "INSERT INTO callback_actions (id, kind, subject_ref,"
                    " task_id, occurrence_key, actor, expires_at, consumed_at,"
                    " created_at) VALUES (:id, :kind, :subject, :task, :occ,"
                    " :actor, :expires, NULL, :now)"), {
                        "id": action_id, "kind": kind.value,
                        "subject": subject_ref, "task": task_id,
                        "occ": occurrence_key, "actor": actor,
                        "expires": expires, "now": to_micros(now)})
                issued[kind.value] = action_id

        if session is not None:
            _write(session)
            return issued

        with self._sessions() as own:
            _write(own)
            own.commit()
        return issued

    def get(self, action_id: str) -> CallbackAction | None:
        with self._sessions() as session:
            row = session.execute(text(
                "SELECT id, kind, subject_ref, task_id, occurrence_key, actor,"
                " expires_at, consumed_at FROM callback_actions"
                " WHERE id = :id"), {"id": action_id}).first()
        if row is None:
            return None
        return CallbackAction(
            id=row[0], kind=ActionKind(row[1]), subject_ref=row[2],
            task_id=row[3], occurrence_key=row[4], actor=row[5],
            expires_at=row[6], consumed_at=row[7])

    def claim(self, action_id: str, *, actor: str) -> CallbackAction:
        """Validate and consume one press, or refuse it with a reason.

        Consuming is a conditional update, not a read followed by a write: two
        taps arriving together would both pass a read-then-check and both act.
        """
        action = self.get(action_id)
        if action is None:
            raise ActionRefused("That button is no longer valid.")
        if action.actor != actor:
            # The message may have been forwarded; only its owner may act.
            logger.warning("Callback %s pressed by %s, issued for %s",
                           action_id, actor, action.actor)
            raise ActionRefused("That button was not meant for you.")
        now = to_micros(self._clock.now())
        if now >= action.expires_at:
            raise ActionRefused(
                f"That button expired on "
                f"{from_micros(action.expires_at):%Y-%m-%d %H:%M}.")

        with self._sessions() as session:
            # Annotated as the other stores do: `execute` is typed as returning
            # `Result`, but a DML statement always returns a `CursorResult`.
            result: CursorResult[Any] = session.execute(text(  # type: ignore[assignment]
                "UPDATE callback_actions SET consumed_at = :now"
                " WHERE id = :id AND consumed_at IS NULL"),
                {"now": now, "id": action_id})
            session.commit()
        if not result.rowcount:
            raise ActionRefused("That button has already been used.")
        return action

    def for_subject(self, subject_ref: str) -> list[CallbackAction]:
        with self._sessions() as session:
            rows = session.execute(text(
                "SELECT id FROM callback_actions WHERE subject_ref = :subject"),
                {"subject": subject_ref}).all()
        return [action for action in
                (self.get(row[0]) for row in rows) if action is not None]
