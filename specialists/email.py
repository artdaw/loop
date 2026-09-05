"""Email Specialist — inbox monitoring and follow-up tracking.

Monitors Gmail and Outlook, tags threads, and flags conversations that have
gone unanswered. When a thread sent by the user gets no reply within a
configurable window, it drafts a follow-up and asks for approval via chat.

Phase 1 scope:
    - Pull unread / recent threads from the Gmail and Outlook integrations.
    - Detect threads last sent by the user with no reply after the window.
    - Draft a follow-up (via LLMRouter) and record a FollowUp in memory.
    - Emit an approval request for the delivery layer.

Autonomous sending is out of scope for v1 — sending always requires approval.
"""

from __future__ import annotations

from dataclasses import dataclass

from config.settings import Settings, get_settings
from core.vector_store import VectorStore


@dataclass
class FlaggedThread:
    """A thread surfaced by the Email Specialist."""

    thread_id: str
    subject: str
    tag: str  # needs-reply | waiting-for-reply | FYI | action-required


class EmailSpecialist:
    """Focused sub-agent for everything email."""

    def __init__(self, settings: Settings | None = None,
                 vector_store: VectorStore | None = None) -> None:
        self.settings = settings or get_settings()
        self.vectors = vector_store or VectorStore(self.settings)
        # TODO(phase1): accept Gmail + Outlook connectors and the LLMRouter.
        # TODO(phase1): accept the MemoryStore for follow-up persistence.

    def index_email(self, subject: str, summary: str, sender: str, date: str) -> str:
        """Index an email summary into the ChromaDB emails collection.

        Called after a thread is summarised so it becomes semantically
        searchable via ``loop find`` and the web dashboard. Email summaries are
        non-private work data, so this uses the local embedding model and stores
        vectors locally.
        """
        return self.vectors.index_email(subject, summary, sender, date)

    def scan_inboxes(self) -> list[FlaggedThread]:
        """Fetch recent threads and classify them into tags."""
        # TODO(phase1): call gmail + outlook connectors; classify each thread.
        raise NotImplementedError("EmailSpecialist.scan_inboxes is a Phase 1 stub.")

    def find_follow_ups_due(self) -> list[FlaggedThread]:
        """Return threads sent by the user with no reply past the window."""
        # TODO(phase1): compare last_sent_at against settings.follow_up_window_hours.
        raise NotImplementedError("EmailSpecialist.find_follow_ups_due is a Phase 1 stub.")

    def draft_follow_up(self, thread_id: str) -> str:
        """Draft a follow-up message for a stalled thread (needs approval)."""
        # TODO(phase1): summarise the thread locally, then draft via LLMRouter.
        raise NotImplementedError("EmailSpecialist.draft_follow_up is a Phase 1 stub.")

    def send_follow_up(self, thread_id: str, body: str) -> None:
        """Send an approved follow-up via the originating provider."""
        # TODO(phase1): dispatch through the correct connector (Gmail/Outlook).
        raise NotImplementedError("EmailSpecialist.send_follow_up is a Phase 1 stub.")
