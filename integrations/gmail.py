"""Gmail integration — read the inbox and send (only after approval).

Uses ``google-api-python-client`` over the shared OAuth credentials in
:mod:`integrations.google_auth`.

Two design notes:

* **Parsing is separated from transport.** ``_to_message`` and the header
  helpers are module-level pure functions, so the awkward parts (MIME headers,
  RFC-2822 dates, base64url bodies) are tested directly without a Google
  service, a token, or a network.
* **Sending is never implicit.** :meth:`GmailClient.send` builds and posts a
  message and nothing else calls it. Approval is enforced above, by the autonomy
  gate's ``EMAIL_SEND`` action, which is capped at ``approve`` by default.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage as MIMEMessage
from email.utils import parsedate_to_datetime
from typing import Any

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: Gmail's own label for messages the account owner sent.
SENT_LABEL = "SENT"


@dataclass
class EmailMessage:
    """A normalised email message."""

    message_id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    sent_at: datetime | None = None
    labels: list[str] = field(default_factory=list)
    from_me: bool = False

    @property
    def is_unread(self) -> bool:
        return "UNREAD" in self.labels


@dataclass
class EmailThread:
    """A thread, reduced to what follow-up tracking needs."""

    thread_id: str
    subject: str
    messages: list[EmailMessage] = field(default_factory=list)

    @property
    def last_message(self) -> EmailMessage | None:
        return self.messages[-1] if self.messages else None

    @property
    def last_from_me(self) -> EmailMessage | None:
        """The most recent message the user sent, if any."""
        for message in reversed(self.messages):
            if message.from_me:
                return message
        return None

    @property
    def awaiting_reply(self) -> bool:
        """True when the user sent the most recent message in the thread.

        That is the whole signal for "they haven't got back to me": if anyone
        else had replied, their message would be last.
        """
        last = self.last_message
        return bool(last and last.from_me)

    def waiting_since(self) -> datetime | None:
        """When the user's last message went out."""
        last = self.last_from_me
        return last.sent_at if last else None


def _header(headers: list[dict], name: str) -> str:
    """Case-insensitively read one header value from Gmail's header list."""
    target = name.lower()
    for header in headers or []:
        if str(header.get("name", "")).lower() == target:
            return str(header.get("value", "") or "")
    return ""


def _addresses(raw: str) -> list[str]:
    """Split a comma-separated address header into individual addresses."""
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _parse_date(raw: str) -> datetime | None:
    """Parse an RFC-2822 ``Date:`` header into an aware UTC datetime."""
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_message(raw: dict) -> EmailMessage:
    """Normalise one Gmail API message resource."""
    payload = raw.get("payload") or {}
    headers = payload.get("headers") or []
    labels = [str(label) for label in (raw.get("labelIds") or [])]

    return EmailMessage(
        message_id=str(raw.get("id") or ""),
        thread_id=str(raw.get("threadId") or ""),
        sender=_header(headers, "From"),
        subject=_header(headers, "Subject"),
        snippet=str(raw.get("snippet") or ""),
        to=_addresses(_header(headers, "To")),
        cc=_addresses(_header(headers, "Cc")),
        sent_at=_parse_date(_header(headers, "Date")),
        labels=labels,
        from_me=SENT_LABEL in labels,
    )


def build_mime(*, to: list[str], subject: str, body: str,
               cc: list[str] | None = None,
               in_reply_to: str | None = None) -> str:
    """Build a base64url-encoded RFC-2822 message for the Gmail API."""
    message = MIMEMessage()
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    if in_reply_to:
        # Threads the reply correctly in the recipient's client, not just ours.
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


class GmailClient:
    """Thin wrapper around the Gmail API."""

    #: Kept for backwards compatibility; the real scope set lives in google_auth.
    SCOPES = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ]

    def __init__(self, settings: Settings | None = None,
                 service: Any | None = None,
                 auth: Any | None = None) -> None:
        self.settings = settings or get_settings()
        # Injectable for tests: a fake with the googleapiclient resource shape.
        self._service = service
        self._auth = auth

    # ------------------------------------------------------------------ #
    # Wiring
    # ------------------------------------------------------------------ #
    def _get_auth(self) -> Any:
        if self._auth is None:
            from integrations.google_auth import GoogleAuth

            self._auth = GoogleAuth(self.settings)
        return self._auth

    @property
    def configured(self) -> bool:
        """True when a client-secrets file is present (or a service injected)."""
        if self._service is not None:
            return True
        return bool(self._get_auth().configured)

    def _ensure_service(self) -> Any:
        """Build the Gmail service, running OAuth if a token is needed."""
        if self._service is None:
            self._service = self._get_auth().build("gmail", "v1")
        return self._service

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def _list_ids(self, query: str, *, max_results: int) -> list[str]:
        service = self._ensure_service()
        response = (
            service.users().messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return [str(item["id"]) for item in (response.get("messages") or [])
                if item.get("id")]

    def _get_message(self, message_id: str) -> EmailMessage:
        service = self._ensure_service()
        raw = (
            service.users().messages()
            .get(userId="me", id=message_id, format="metadata",
                 metadataHeaders=["From", "To", "Cc", "Subject", "Date",
                                  "Message-ID"])
            .execute()
        )
        return _to_message(raw)

    def list_unread(self, *, max_results: int = 25) -> list[EmailMessage]:
        """Return recent unread inbox messages."""
        ids = self._list_ids("is:unread in:inbox", max_results=max_results)
        return [self._get_message(message_id) for message_id in ids]

    def list_recent(self, *, days: int = 14,
                    max_results: int = 50) -> list[EmailMessage]:
        """Return recent inbox messages, read or not."""
        ids = self._list_ids(f"in:inbox newer_than:{days}d", max_results=max_results)
        return [self._get_message(message_id) for message_id in ids]

    def get_thread(self, thread_id: str) -> EmailThread:
        """Fetch one thread with its messages in order."""
        service = self._ensure_service()
        raw = (
            service.users().threads()
            .get(userId="me", id=thread_id, format="metadata",
                 metadataHeaders=["From", "To", "Cc", "Subject", "Date",
                                  "Message-ID"])
            .execute()
        )
        messages = [_to_message(item) for item in (raw.get("messages") or [])]
        messages.sort(key=lambda m: m.sent_at or datetime.min.replace(
            tzinfo=UTC))
        subject = messages[0].subject if messages else ""
        return EmailThread(thread_id=str(raw.get("id") or thread_id),
                           subject=subject, messages=messages)

    def threads_awaiting_reply(self, *, window_hours: int | None = None,
                               max_results: int = 25) -> list[EmailThread]:
        """Return threads where the user sent the last message and nobody replied.

        ``window_hours`` defaults to ``settings.follow_up_window_hours``: a
        thread only counts once it has been quiet for that long, otherwise Loop
        would nag about mail sent ten minutes ago.
        """
        hours = (self.settings.follow_up_window_hours
                 if window_hours is None else window_hours)
        cutoff = datetime.now(UTC) - timedelta(hours=hours)

        # Gmail's own search finds sent mail; the reply check needs the thread.
        ids = self._list_ids("in:sent newer_than:30d", max_results=max_results)

        seen: set[str] = set()
        stalled: list[EmailThread] = []
        for message_id in ids:
            try:
                message = self._get_message(message_id)
            except Exception:  # noqa: BLE001 - one bad message must not stop the scan
                logger.exception("Could not read Gmail message %s", message_id)
                continue
            if not message.thread_id or message.thread_id in seen:
                continue
            seen.add(message.thread_id)

            try:
                thread = self.get_thread(message.thread_id)
            except Exception:  # noqa: BLE001
                logger.exception("Could not read Gmail thread %s", message.thread_id)
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
             thread_id: str | None = None, cc: list[str] | None = None,
             in_reply_to: str | None = None) -> str:
        """Send an email. Returns the new message id.

        Nothing in Loop calls this without an approved
        :class:`~core.autonomy.ActionType` ``EMAIL_SEND`` decision.
        """
        if not to:
            raise ValueError("Refusing to send an email with no recipient.")

        service = self._ensure_service()
        payload: dict[str, Any] = {
            "raw": build_mime(to=to, subject=subject, body=body, cc=cc,
                              in_reply_to=in_reply_to),
        }
        if thread_id:
            payload["threadId"] = thread_id

        result = service.users().messages().send(userId="me", body=payload).execute()
        return str(result.get("id") or "")
