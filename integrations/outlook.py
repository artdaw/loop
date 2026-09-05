"""Outlook integration — read inbox and send (with approval) via Microsoft Graph.

Uses ``msgraph-sdk`` with OAuth 2.0 (Azure app registration). Client id/secret
and tenant come from settings.

Phase 1 scope:
    - Authenticate against Microsoft Graph.
    - list_unread(): return recent unread messages.
    - Provide send() used only after user approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import Settings, get_settings


@dataclass
class OutlookMessage:
    """A normalised Outlook message."""

    message_id: str
    conversation_id: str
    sender: str
    subject: str
    preview: str = ""
    to: list[str] = field(default_factory=list)


class OutlookClient:
    """Thin wrapper around Microsoft Graph mail endpoints."""

    SCOPES = ["Mail.Read", "Mail.Send"]

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = None
        # TODO(phase1): build a GraphServiceClient in _ensure_client().

    def _ensure_client(self) -> None:
        """Authenticate and build the Graph client."""
        # TODO(phase1): use azure.identity with settings.outlook_client_id/secret
        #               and settings.outlook_tenant_id.
        raise NotImplementedError("OutlookClient._ensure_client is a Phase 1 stub.")

    def list_unread(self, *, max_results: int = 25) -> list[OutlookMessage]:
        """Return recent unread messages from the inbox."""
        # TODO(phase1): GET /me/mailFolders/inbox/messages?$filter=isRead eq false.
        raise NotImplementedError("OutlookClient.list_unread is a Phase 1 stub.")

    def send(self, *, to: list[str], subject: str, body: str) -> str:
        """Send an email (only after user approval). Returns the message id."""
        # TODO(phase1): POST /me/sendMail with the message payload.
        raise NotImplementedError("OutlookClient.send is a Phase 1 stub.")
