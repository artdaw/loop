"""The email specialist reading from real connectors.

Gmail and Outlook expose the same normalised types, so the specialist never
branches on provider — these tests use one fake shaped like both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from config.settings import Settings
from integrations.gmail import EmailMessage, EmailThread
from specialists.email import EmailSpecialist


class FakeMail:
    """Stands in for GmailClient or OutlookClient — same interface."""

    def __init__(self, unread=None, stalled=None, boom: bool = False) -> None:
        self._unread = unread or []
        self._stalled = stalled or []
        self.boom = boom

    def list_unread(self, *, max_results: int = 25):
        if self.boom:
            raise RuntimeError("mailbox unreachable")
        return list(self._unread)

    def threads_awaiting_reply(self, **kwargs):
        if self.boom:
            raise RuntimeError("mailbox unreachable")
        return list(self._stalled)


def _message(subject="Hello", sender="anna@acme.com", **kw) -> EmailMessage:
    return EmailMessage(message_id="m1", thread_id=kw.pop("thread_id", "t1"),
                        sender=sender, subject=subject, **kw)


def _thread(thread_id="t1", subject="Ping", hours_ago=72) -> EmailThread:
    mine = EmailMessage(
        message_id="m1", thread_id=thread_id, sender="me@artdaw.com",
        subject=subject, from_me=True, labels=["SENT"],
        sent_at=datetime.now(UTC) - timedelta(hours=hours_ago),
    )
    return EmailThread(thread_id=thread_id, subject=subject, messages=[mine])


def _spec(settings, **kw) -> EmailSpecialist:
    # A stand-in vector store: these tests never index anything.
    return EmailSpecialist(settings, vector_store=object(), **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# scan_inboxes
# --------------------------------------------------------------------------- #
def test_no_connectors_means_no_flagged_mail(settings):
    assert _spec(settings).scan_inboxes() == []


def test_scans_a_single_provider(settings):
    spec = _spec(settings, gmail=FakeMail(unread=[_message("Contract review")]))

    flagged = spec.scan_inboxes()

    assert len(flagged) == 1
    assert flagged[0].subject == "Contract review"


def test_scans_both_providers(settings):
    spec = _spec(settings,
                 gmail=FakeMail(unread=[_message("From Gmail", thread_id="g1")]),
                 outlook=FakeMail(unread=[_message("From Outlook", thread_id="o1")]))

    subjects = {t.subject for t in spec.scan_inboxes()}

    assert subjects == {"From Gmail", "From Outlook"}


def test_one_failing_provider_does_not_hide_the_other(settings):
    spec = _spec(settings,
                 gmail=FakeMail(unread=[_message("Survived")]),
                 outlook=FakeMail(boom=True))

    flagged = spec.scan_inboxes()

    assert [t.subject for t in flagged] == ["Survived"]
    assert spec.last_errors and "outlook" in spec.last_errors[0]


def test_a_clean_scan_records_no_errors(settings):
    spec = _spec(settings, gmail=FakeMail(unread=[]))
    spec.scan_inboxes()
    assert spec.last_errors == []


def test_urgent_mail_is_tagged_action_required(tmp_path):
    settings = Settings(obsidian_vault_path=str(tmp_path),  # type: ignore[call-arg]
                        vip_senders="boss@acme.com")
    urgent = _message("URGENT: deadline today", sender="boss@acme.com",
                      snippet="need this asap")
    spec = _spec(settings, gmail=FakeMail(unread=[urgent]))

    assert spec.scan_inboxes()[0].tag == "action-required"


# --------------------------------------------------------------------------- #
# find_follow_ups_due
# --------------------------------------------------------------------------- #
def test_finds_stalled_threads(settings):
    spec = _spec(settings, gmail=FakeMail(stalled=[_thread(subject="Ping")]))

    flagged = spec.find_follow_ups_due()

    assert len(flagged) == 1
    assert flagged[0].tag == "waiting-for-reply"


def test_a_failing_provider_is_recorded(settings):
    spec = _spec(settings, gmail=FakeMail(boom=True))

    assert spec.find_follow_ups_due() == []
    assert spec.last_errors


# --------------------------------------------------------------------------- #
# persist_follow_ups
# --------------------------------------------------------------------------- #
def test_persists_stalled_threads(settings, memory_store):
    spec = _spec(settings, memory=memory_store,
                 gmail=FakeMail(stalled=[_thread("t1", "Contract review")]))

    assert spec.persist_follow_ups() == 1

    stored = memory_store.list_open_follow_ups()
    assert [f.subject for f in stored] == ["Contract review"]
    assert stored[0].triage_score > 0


def test_rescanning_does_not_duplicate(settings, memory_store):
    spec = _spec(settings, memory=memory_store,
                 gmail=FakeMail(stalled=[_thread("t1", "Contract review")]))
    spec.persist_follow_ups()

    assert spec.persist_follow_ups() == 0
    assert len(memory_store.list_open_follow_ups()) == 1


def test_rescanning_does_not_resurrect_a_snoozed_thread(settings, memory_store):
    """A thread the user snoozed must stay hidden across scans."""
    spec = _spec(settings, memory=memory_store,
                 gmail=FakeMail(stalled=[_thread("t1", "Contract review")]))
    spec.persist_follow_ups()
    follow_up = memory_store.list_open_follow_ups()[0]
    memory_store.snooze_follow_up(follow_up.id, 24)

    assert spec.persist_follow_ups() == 0
    assert memory_store.list_open_follow_ups() == []


def test_persisting_without_memory_is_a_noop(settings):
    spec = _spec(settings, gmail=FakeMail(stalled=[_thread()]))
    assert spec.persist_follow_ups() == 0
