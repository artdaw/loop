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

import logging
from dataclasses import dataclass
from typing import Any

from config.settings import Settings, get_settings
from core.vector_store import VectorStore

# Keywords that raise an email's urgency score.
_URGENCY_KEYWORDS: tuple[str, ...] = (
    "urgent", "asap", "as soon as possible", "deadline", "today", "eod",
    "immediately", "critical", "important", "time-sensitive", "reminder",
)

logger = logging.getLogger(__name__)

#: Maps a triage action onto the coarser tag the dashboard and briefing show.
_TAG_FOR_ACTION: dict[str, str] = {
    "reply_now": "action-required",
    "reply_later": "needs-reply",
    "delegate": "action-required",
    "read_only": "FYI",
    "archive": "FYI",
}

# Valid triage actions.
TRIAGE_ACTIONS: tuple[str, ...] = (
    "reply_now", "reply_later", "read_only", "delegate", "archive",
)


@dataclass
class FlaggedThread:
    """A thread surfaced by the Email Specialist."""

    thread_id: str
    subject: str
    tag: str  # needs-reply | waiting-for-reply | FYI | action-required


@dataclass
class TriageScore:
    """The triage outcome for a single email."""

    urgency: int      # 1..5
    importance: int   # 1..5
    action: str       # one of TRIAGE_ACTIONS
    project: str | None = None  # matched project slug, or None (Phase 4)

    @property
    def score(self) -> int:
        """Composite ranking score (urgency * importance, 1..25)."""
        return self.urgency * self.importance


class EmailSpecialist:
    """Focused sub-agent for everything email."""

    def __init__(self, settings: Settings | None = None,
                 vector_store: VectorStore | None = None,
                 memory: Any | None = None,
                 matcher: Any | None = None,
                 gmail: Any | None = None,
                 outlook: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.vectors = vector_store or VectorStore(self.settings)
        # Optional MemoryStore for persisting triage scores onto follow-ups.
        self._memory = memory
        # Optional ProjectMatcher (Phase 4). Mail that belongs to an active
        # project is more important to you than mail that belongs to none.
        self.matcher = matcher
        # Mail connectors. Both expose the same normalised interface, so this
        # class never branches on provider.
        self._gmail = gmail
        self._outlook = outlook
        # Which providers failed on the last scan, so callers can distinguish
        # "no flagged mail" from "could not read the mailbox".
        self.last_errors: list[str] = []
        # TODO(phase1): accept Gmail + Outlook connectors and the LLMRouter.

    # ------------------------------------------------------------------ #
    # Triage scoring (Phase 3)
    # ------------------------------------------------------------------ #
    def triage_email(self, email: Any) -> TriageScore:
        """Score an email's urgency and importance and recommend an action.

        ``email`` may be any object/dict exposing the fields we read:
        ``subject``, ``sender``, ``snippet``/``body``, ``cc`` (list),
        ``thread_length`` (int), and ``sender_in_contacts`` (bool).

        - urgency (1-5): urgency keywords in subject/body + VIP/boss sender.
        - importance (1-5): thread length, CC count, sender in contacts, VIP.
        - action: reply_now | reply_later | read_only | delegate | archive.
        """
        subject = str(self._get(email, "subject", "") or "")
        sender = str(self._get(email, "sender", "") or "").lower()
        body = str(self._get(email, "body", "") or self._get(email, "snippet", "") or "")
        cc = self._get(email, "cc", []) or []
        cc_count = len(cc) if isinstance(cc, (list, tuple)) else int(cc or 0)
        thread_length = int(self._get(email, "thread_length", 1) or 1)
        in_contacts = bool(self._get(email, "sender_in_contacts", False))

        is_vip = any(vip in sender for vip in self.settings.vip_sender_list)
        haystack = f"{subject}\n{body}".lower()
        keyword_hits = sum(1 for kw in _URGENCY_KEYWORDS if kw in haystack)

        # --- Urgency (1..5) --------------------------------------------
        urgency = 1
        urgency += min(keyword_hits, 3)          # up to +3 for urgency keywords
        if is_vip:
            urgency += 1                          # VIP/boss bumps urgency
        urgency = max(1, min(urgency, 5))

        # --- Importance (1..5) -----------------------------------------
        importance = 1
        if is_vip:
            importance += 2
        if in_contacts:
            importance += 1
        if thread_length >= 3:
            importance += 1
        if cc_count >= 3:
            importance += 1
        project = self._match_project(f"{subject}\n{body}")
        if project:
            importance += 1                       # belongs to an active project
        importance = max(1, min(importance, 5))

        action = self._recommend_action(urgency, importance, is_vip=is_vip,
                                        cc_count=cc_count)
        return TriageScore(urgency=urgency, importance=importance, action=action,
                           project=project)

    def _match_project(self, text: str) -> str | None:
        """Best-effort project tagging; never blocks triage."""
        if self.matcher is None:
            return None
        try:
            return self.matcher.match_slug(text)
        except Exception:  # noqa: BLE001 - tagging is a nicety, not a gate
            logger.exception("Project matching failed during email triage")
            return None

    @staticmethod
    def _recommend_action(urgency: int, importance: int, *, is_vip: bool,
                          cc_count: int) -> str:
        """Map (urgency, importance) to a recommended action."""
        composite = urgency * importance
        if urgency >= 4 and importance >= 4:
            return "reply_now"
        if importance >= 4 and urgency <= 2:
            return "reply_later"
        if composite <= 3 and not is_vip:
            return "archive"
        if importance <= 2 and cc_count >= 3:
            # Broadly CC'd, low importance to me -> likely delegable/FYI.
            return "delegate" if urgency >= 3 else "read_only"
        if composite >= 9:
            return "reply_now"
        return "reply_later"

    def triage_and_persist(self, follow_up_id: int, email: Any) -> TriageScore:
        """Triage an email and persist its composite score onto a follow-up."""
        score = self.triage_email(email)
        if self._memory is not None:
            self._memory.set_follow_up_triage(follow_up_id, score.score)
        return score

    def briefing_order(self, follow_ups: list) -> list:
        """Return follow-ups sorted by triage score (urgency*importance), highest first."""
        return sorted(follow_ups, key=lambda f: getattr(f, "triage_score", 0) or 0,
                      reverse=True)

    @staticmethod
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        """Read ``key`` from an object attribute or a dict."""
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def index_email(self, subject: str, summary: str, sender: str, date: str) -> str:
        """Index an email summary into the ChromaDB emails collection.

        Called after a thread is summarised so it becomes semantically
        searchable via ``loop find`` and the web dashboard. Email summaries are
        non-private work data, so this uses the local embedding model and stores
        vectors locally.
        """
        return self.vectors.index_email(subject, summary, sender, date)

    def _providers(self) -> list[tuple[str, Any]]:
        """The configured mail connectors, as (name, client) pairs."""
        return [(name, client)
                for name, client in (("gmail", self._gmail), ("outlook", self._outlook))
                if client is not None]

    def scan_inboxes(self, *, max_results: int = 25) -> list[FlaggedThread]:
        """Fetch unread mail from every connected provider and classify it.

        Providers are polled independently: one mailbox failing must not hide
        the other's mail. Failures land in :attr:`last_errors`.
        """
        self.last_errors = []
        flagged: list[FlaggedThread] = []

        for name, client in self._providers():
            try:
                messages = client.list_unread(max_results=max_results)
            except Exception as exc:  # noqa: BLE001 - one provider must not sink the rest
                self.last_errors.append(f"{name}: {exc}")
                logger.exception("Could not read the %s inbox", name)
                continue

            for message in messages:
                score = self.triage_email(message)
                flagged.append(FlaggedThread(
                    thread_id=message.thread_id,
                    subject=message.subject,
                    tag=_TAG_FOR_ACTION.get(score.action, "FYI"),
                ))

        return flagged

    def find_follow_ups_due(self) -> list[FlaggedThread]:
        """Return threads the user sent that have gone unanswered too long.

        The window comes from ``settings.follow_up_window_hours``; the
        connectors apply it, so this method is provider-agnostic.
        """
        self.last_errors = []
        flagged: list[FlaggedThread] = []

        for name, client in self._providers():
            try:
                threads = client.threads_awaiting_reply()
            except Exception as exc:  # noqa: BLE001
                self.last_errors.append(f"{name}: {exc}")
                logger.exception("Could not scan %s for stalled threads", name)
                continue

            for thread in threads:
                flagged.append(FlaggedThread(
                    thread_id=thread.thread_id,
                    subject=thread.subject,
                    tag="waiting-for-reply",
                ))

        return flagged

    def persist_follow_ups(self) -> int:
        """Store stalled threads so the briefing and dashboard can show them.

        Existing rows are left alone — re-scanning must not resurrect a thread
        the user already snoozed or ignored. Returns the number newly recorded.
        """
        if self._memory is None:
            return 0

        known = {row.thread_id for row in
                 self._memory.list_open_follow_ups(include_snoozed=True)}
        recorded = 0

        for name, client in self._providers():
            try:
                threads = client.threads_awaiting_reply()
            except Exception as exc:  # noqa: BLE001
                self.last_errors.append(f"{name}: {exc}")
                continue

            for thread in threads:
                if thread.thread_id in known:
                    continue
                last = thread.last_from_me
                score = self.triage_email({
                    "subject": thread.subject,
                    "sender": last.sender if last else "",
                    "body": last.snippet if last else "",
                    "thread_length": len(thread.messages),
                })
                self._memory.add_follow_up(
                    thread_id=thread.thread_id,
                    subject=thread.subject,
                    sender=last.sender if last else "",
                    triage_score=score.score,
                )
                known.add(thread.thread_id)
                recorded += 1

        return recorded

    def draft_follow_up(self, thread_id: str) -> str:
        """Draft a follow-up message for a stalled thread (needs approval)."""
        # TODO(phase1): summarise the thread locally, then draft via LLMRouter.
        raise NotImplementedError("EmailSpecialist.draft_follow_up is a Phase 1 stub.")

    def send_follow_up(self, thread_id: str, body: str) -> None:
        """Send an approved follow-up via the originating provider."""
        # TODO(phase1): dispatch through the correct connector (Gmail/Outlook).
        raise NotImplementedError("EmailSpecialist.send_follow_up is a Phase 1 stub.")
