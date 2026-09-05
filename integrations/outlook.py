"""Outlook / Microsoft 365 mail via Microsoft Graph.

Mirrors :mod:`integrations.gmail` deliberately — same normalised
:class:`~integrations.gmail.EmailMessage` and :class:`EmailThread` types, same
follow-up signal — so :mod:`specialists.email` can treat both providers
identically and Loop's follow-up logic lives in one place rather than being
reimplemented per provider.

Graph calls go through :class:`~integrations.ms_graph.GraphClient`; see that
module for why this is delegated (``/me``) REST rather than the SDK.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from config.settings import Settings, get_settings
from integrations.gmail import EmailMessage, EmailThread

logger = logging.getLogger(__name__)

#: Message fields Loop actually reads — keeps responses small.
MESSAGE_FIELDS = (
    "id,conversationId,subject,from,toRecipients,ccRecipients,"
    "sentDateTime,isRead,bodyPreview"
)


def _recipients(entries: Any) -> list[str]:
    """Pull plain addresses out of Graph's nested recipient objects."""
    addresses: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        address = (entry.get("emailAddress") or {}).get("address")
        if address:
            addresses.append(str(address))
    return addresses


def _sender(raw: dict) -> str:
    """Graph nests the sender two levels deep, and omits it on drafts."""
    holder = raw.get("from") or raw.get("sender") or {}
    if not isinstance(holder, dict):
        return ""
    return str((holder.get("emailAddress") or {}).get("address") or "")


def _parse_datetime(raw: Any) -> datetime | None:
    """Parse a Graph ISO-8601 timestamp into an aware UTC datetime."""
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_message(raw: dict, *, my_addresses: set[str] | None = None) -> EmailMessage:
    """Normalise one Graph message into Loop's shared EmailMessage."""
    sender = _sender(raw)
    mine = {address.lower() for address in (my_addresses or set())}

    labels: list[str] = []
    if not raw.get("isRead", True):
        labels.append("UNREAD")

    from_me = bool(sender and sender.lower() in mine)
    if from_me:
        # Keep the label vocabulary identical to Gmail's so downstream code
        # (and the email specialist) never has to branch on provider.
        labels.append("SENT")

    return EmailMessage(
        message_id=str(raw.get("id") or ""),
        thread_id=str(raw.get("conversationId") or ""),
        sender=sender,
        subject=str(raw.get("subject") or ""),
        snippet=str(raw.get("bodyPreview") or ""),
        to=_recipients(raw.get("toRecipients")),
        cc=_recipients(raw.get("ccRecipients")),
        sent_at=_parse_datetime(raw.get("sentDateTime")),
        labels=labels,
        from_me=from_me,
    )


def build_graph_message(*, to: list[str], subject: str, body: str,
                        cc: list[str] | None = None) -> dict:
    """Build the JSON body Graph's ``sendMail`` expects."""
    def _to_recipients(addresses: list[str]) -> list[dict]:
        return [{"emailAddress": {"address": address}} for address in addresses]

    message: dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": _to_recipients(to),
    }
    if cc:
        message["ccRecipients"] = _to_recipients(cc)
    return {"message": message, "saveToSentItems": True}


class OutlookClient:
    """Read the inbox and send (after approval) via Microsoft Graph."""

    def __init__(self, settings: Settings | None = None,
                 graph: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._graph = graph
        self._my_addresses: set[str] | None = None

    def _get_graph(self) -> Any:
        if self._graph is None:
            from integrations.ms_graph import GraphClient

            self._graph = GraphClient(self.settings)
        return self._graph

    @property
    def configured(self) -> bool:
        return bool(self._get_graph().configured)

    # ------------------------------------------------------------------ #
    # Identity — needed to tell "sent by me" from "sent to me"
    # ------------------------------------------------------------------ #
    def my_addresses(self) -> set[str]:
        """The signed-in user's own addresses, cached for the process."""
        if self._my_addresses is None:
            addresses: set[str] = set()
            try:
                profile = self._get_graph().get("/me", **{"$select": "mail,userPrincipalName"})
                for key in ("mail", "userPrincipalName"):
                    value = profile.get(key)
                    if value:
                        addresses.add(str(value).lower())
            except Exception:  # noqa: BLE001 - identity is best-effort
                logger.exception("Could not read the Outlook profile")
            self._my_addresses = addresses
        return self._my_addresses

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def list_unread(self, *, max_results: int = 25) -> list[EmailMessage]:
        """Return recent unread inbox messages."""
        response = self._get_graph().get(
            "/me/mailFolders/inbox/messages",
            **{
                "$filter": "isRead eq false",
                "$top": max_results,
                "$orderby": "sentDateTime desc",
                "$select": MESSAGE_FIELDS,
            },
        )
        mine = self.my_addresses()
        return [_to_message(raw, my_addresses=mine)
                for raw in response.get("value") or []]

    def list_recent(self, *, days: int = 14,
                    max_results: int = 50) -> list[EmailMessage]:
        """Return recent inbox messages, read or not."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        response = self._get_graph().get(
            "/me/mailFolders/inbox/messages",
            **{
                "$filter": f"sentDateTime ge {since}",
                "$top": max_results,
                "$orderby": "sentDateTime desc",
                "$select": MESSAGE_FIELDS,
            },
        )
        mine = self.my_addresses()
        return [_to_message(raw, my_addresses=mine)
                for raw in response.get("value") or []]

    def get_thread(self, conversation_id: str) -> EmailThread:
        """Fetch every message in one conversation, oldest first."""
        response = self._get_graph().get(
            "/me/messages",
            **{
                "$filter": f"conversationId eq '{conversation_id}'",
                "$orderby": "sentDateTime asc",
                "$select": MESSAGE_FIELDS,
                "$top": 50,
            },
        )
        mine = self.my_addresses()
        messages = [_to_message(raw, my_addresses=mine)
                    for raw in response.get("value") or []]
        messages.sort(key=lambda m: m.sent_at or datetime.min.replace(
            tzinfo=UTC))
        subject = messages[0].subject if messages else ""
        return EmailThread(thread_id=conversation_id, subject=subject,
                           messages=messages)

    def threads_awaiting_reply(self, *, window_hours: int | None = None,
                               max_results: int = 25) -> list[EmailThread]:
        """Threads where the user sent the last message and nobody replied."""
        hours = (self.settings.follow_up_window_hours
                 if window_hours is None else window_hours)
        cutoff = datetime.now(UTC) - timedelta(hours=hours)

        response = self._get_graph().get(
            "/me/mailFolders/sentitems/messages",
            **{
                "$top": max_results,
                "$orderby": "sentDateTime desc",
                "$select": MESSAGE_FIELDS,
            },
        )

        seen: set[str] = set()
        stalled: list[EmailThread] = []
        for raw in response.get("value") or []:
            conversation_id = str(raw.get("conversationId") or "")
            if not conversation_id or conversation_id in seen:
                continue
            seen.add(conversation_id)

            try:
                thread = self.get_thread(conversation_id)
            except Exception:  # noqa: BLE001 - one bad thread must not stop the scan
                logger.exception("Could not read Outlook conversation %s",
                                 conversation_id)
                continue

            if not thread.awaiting_reply:
                continue
            waiting_since = thread.waiting_since()
            if waiting_since is not None and waiting_since <= cutoff:
                stalled.append(thread)

        return stalled

    # ------------------------------------------------------------------ #
    # Sending — only ever called after explicit approval
    # ------------------------------------------------------------------ #
    def send(self, *, to: list[str], subject: str, body: str,
             cc: list[str] | None = None, thread_id: str | None = None) -> str:
        """Send an email via Graph. Returns "" — sendMail has no response body."""
        if not to:
            raise ValueError("Refusing to send an email with no recipient.")

        self._get_graph().post(
            "/me/sendMail",
            build_graph_message(to=to, subject=subject, body=body, cc=cc),
        )
        return ""
