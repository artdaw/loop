"""Gmail integration — read inbox and send (with approval) via the Gmail API.

Uses the official ``google-api-python-client`` with OAuth 2.0. Credentials come
from a client-secrets file (settings.gmail_credentials_path); the resulting user
token is cached at settings.gmail_token_path.

Phase 1 scope:
    - OAuth bootstrap (load/refresh credentials).
    - list_unread(): return recent unread threads.
    - Provide send() used only after user approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import Settings, get_settings


@dataclass
class EmailMessage:
    """A normalised email message."""

    message_id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str = ""
    to: list[str] = field(default_factory=list)


class GmailClient:
    """Thin wrapper around the Gmail API."""

    SCOPES = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ]

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._service = None
        # TODO(phase1): lazily build the service in _ensure_service().

    def _ensure_service(self) -> None:
        """Load OAuth credentials and build the Gmail service object."""
        # TODO(phase1): use google_auth_oauthlib InstalledAppFlow with SCOPES;
        #               cache/refresh token at settings.gmail_token_path.
        raise NotImplementedError("GmailClient._ensure_service is a Phase 1 stub.")

    def list_unread(self, *, max_results: int = 25) -> list[EmailMessage]:
        """Return recent unread messages from the inbox."""
        # TODO(phase1): call users().messages().list(q="is:unread") + get().
        raise NotImplementedError("GmailClient.list_unread is a Phase 1 stub.")

    def send(self, *, to: list[str], subject: str, body: str,
             thread_id: str | None = None) -> str:
        """Send an email (only after user approval). Returns the message id."""
        # TODO(phase1): build a MIME message and call users().messages().send().
        raise NotImplementedError("GmailClient.send is a Phase 1 stub.")
