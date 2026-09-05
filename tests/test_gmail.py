"""The Gmail connector.

Parsing is tested directly against real Gmail API payload shapes; transport is
tested through a fake service with the googleapiclient resource shape. No
network, no token, no OAuth.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from integrations.gmail import (
    EmailThread,
    GmailClient,
    _addresses,
    _header,
    _parse_date,
    _to_message,
    build_mime,
)


def _raw(message_id="m1", thread_id="t1", *, headers=None, labels=None,
         snippet="") -> dict:
    return {
        "id": message_id,
        "threadId": thread_id,
        "snippet": snippet,
        "labelIds": labels or [],
        "payload": {"headers": headers or []},
    }


def _h(name, value):
    return {"name": name, "value": value}


# --------------------------------------------------------------------------- #
# Header + date parsing
# --------------------------------------------------------------------------- #
def test_header_lookup_is_case_insensitive():
    headers = [_h("From", "a@b.c"), _h("SUBJECT", "Hello")]
    assert _header(headers, "from") == "a@b.c"
    assert _header(headers, "subject") == "Hello"


def test_missing_header_returns_empty_string():
    assert _header([], "From") == ""


def test_addresses_splits_and_strips():
    assert _addresses("a@b.c, d@e.f ,  g@h.i") == ["a@b.c", "d@e.f", "g@h.i"]


def test_addresses_of_empty_header_is_empty_list():
    assert _addresses("") == []


def test_parses_an_rfc2822_date_to_utc():
    parsed = _parse_date("Tue, 01 Sep 2026 10:30:00 +0200")
    assert parsed.tzinfo is not None
    assert parsed.astimezone(UTC).hour == 8


def test_unparseable_date_returns_none():
    assert _parse_date("not a date") is None
    assert _parse_date("") is None


# --------------------------------------------------------------------------- #
# Message normalisation
# --------------------------------------------------------------------------- #
def test_normalises_a_message():
    raw = _raw(headers=[
        _h("From", "anna@acme.com"),
        _h("To", "me@artdaw.com"),
        _h("Cc", "bob@acme.com, carol@acme.com"),
        _h("Subject", "Contract review"),
        _h("Date", "Tue, 01 Sep 2026 10:30:00 +0000"),
    ], labels=["INBOX", "UNREAD"], snippet="Could you look at...")

    message = _to_message(raw)

    assert message.message_id == "m1"
    assert message.sender == "anna@acme.com"
    assert message.subject == "Contract review"
    assert message.cc == ["bob@acme.com", "carol@acme.com"]
    assert message.is_unread is True
    assert message.from_me is False


def test_sent_label_marks_a_message_as_from_me():
    assert _to_message(_raw(labels=["SENT"])).from_me is True


def test_missing_fields_do_not_raise():
    message = _to_message({"id": "x"})
    assert message.subject == ""
    assert message.sent_at is None


# --------------------------------------------------------------------------- #
# Thread logic — the follow-up signal
# --------------------------------------------------------------------------- #
def _msg(from_me: bool, hours_ago: int):
    raw = _raw(labels=["SENT"] if from_me else ["INBOX"], headers=[
        _h("Date", (datetime.now(UTC)
                    - timedelta(hours=hours_ago)).strftime("%a, %d %b %Y %H:%M:%S %z")),
    ])
    return _to_message(raw)


def test_thread_awaits_reply_when_my_message_is_last():
    thread = EmailThread("t1", "s", [_msg(False, 50), _msg(True, 40)])
    assert thread.awaiting_reply is True


def test_thread_does_not_await_reply_when_they_replied_last():
    thread = EmailThread("t1", "s", [_msg(True, 50), _msg(False, 40)])
    assert thread.awaiting_reply is False


def test_empty_thread_is_not_awaiting_reply():
    assert EmailThread("t1", "s", []).awaiting_reply is False


def test_waiting_since_is_my_last_message():
    mine = _msg(True, 40)
    thread = EmailThread("t1", "s", [_msg(False, 50), mine])
    assert thread.waiting_since() == mine.sent_at


# --------------------------------------------------------------------------- #
# MIME building
# --------------------------------------------------------------------------- #
def test_build_mime_is_base64url_and_round_trips():
    encoded = build_mime(to=["a@b.c"], subject="Hi", body="Hello there")
    decoded = base64.urlsafe_b64decode(encoded).decode()

    assert "To: a@b.c" in decoded
    assert "Subject: Hi" in decoded
    assert "Hello there" in decoded


def test_build_mime_includes_cc_and_threading_headers():
    encoded = build_mime(to=["a@b.c"], subject="Re: Hi", body="x",
                         cc=["d@e.f"], in_reply_to="<abc@mail>")
    decoded = base64.urlsafe_b64decode(encoded).decode()

    assert "Cc: d@e.f" in decoded
    assert "In-Reply-To: <abc@mail>" in decoded
    assert "References: <abc@mail>" in decoded


# --------------------------------------------------------------------------- #
# Transport, via a fake googleapiclient service
# --------------------------------------------------------------------------- #
class FakeExec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeMessages:
    def __init__(self, listing, by_id, sent_log):
        self._listing = listing
        self._by_id = by_id
        self._sent_log = sent_log
        self.last_query = None

    def list(self, *, userId, q, maxResults):
        self.last_query = q
        return FakeExec(self._listing)

    def get(self, *, userId, id, format=None, metadataHeaders=None):
        return FakeExec(self._by_id[id])

    def send(self, *, userId, body):
        self._sent_log.append(body)
        return FakeExec({"id": "sent-1"})


class FakeThreads:
    def __init__(self, threads):
        self._threads = threads

    def get(self, *, userId, id, format=None, metadataHeaders=None):
        return FakeExec(self._threads[id])


class FakeUsers:
    def __init__(self, messages, threads):
        self._messages = messages
        self._threads = threads

    def messages(self):
        return self._messages

    def threads(self):
        return self._threads


class FakeService:
    def __init__(self, *, listing=None, by_id=None, threads=None):
        self.sent: list[dict] = []
        self._messages = FakeMessages(listing or {}, by_id or {}, self.sent)
        self._threads = FakeThreads(threads or {})

    def users(self):
        return FakeUsers(self._messages, self._threads)


def test_list_unread_returns_normalised_messages(settings):
    service = FakeService(
        listing={"messages": [{"id": "m1"}]},
        by_id={"m1": _raw(headers=[_h("Subject", "Hello")], labels=["UNREAD"])},
    )
    client = GmailClient(settings, service=service)

    messages = client.list_unread()

    assert len(messages) == 1
    assert messages[0].subject == "Hello"


def test_list_unread_uses_an_unread_query(settings):
    service = FakeService(listing={"messages": []})
    GmailClient(settings, service=service).list_unread()

    assert "is:unread" in service._messages.last_query


def test_empty_listing_returns_empty_list(settings):
    client = GmailClient(settings, service=FakeService(listing={}))
    assert client.list_unread() == []


def test_get_thread_orders_messages_by_date(settings):
    older = _raw("m1", headers=[_h("Date", "Tue, 01 Sep 2026 09:00:00 +0000"),
                                _h("Subject", "First")])
    newer = _raw("m2", headers=[_h("Date", "Tue, 01 Sep 2026 12:00:00 +0000")])
    service = FakeService(threads={"t1": {"id": "t1", "messages": [newer, older]}})

    thread = GmailClient(settings, service=service).get_thread("t1")

    assert [m.message_id for m in thread.messages] == ["m1", "m2"]
    assert thread.subject == "First"


def test_threads_awaiting_reply_finds_a_stalled_thread(settings):
    old = (datetime.now(UTC) - timedelta(days=5)).strftime(
        "%a, %d %b %Y %H:%M:%S %z")
    mine = _raw("m1", "t1", headers=[_h("Date", old), _h("Subject", "Ping")],
                labels=["SENT"])
    service = FakeService(
        listing={"messages": [{"id": "m1"}]},
        by_id={"m1": mine},
        threads={"t1": {"id": "t1", "messages": [mine]}},
    )

    stalled = GmailClient(settings, service=service).threads_awaiting_reply()

    assert len(stalled) == 1
    assert stalled[0].thread_id == "t1"


def test_threads_answered_are_not_flagged(settings):
    old = (datetime.now(UTC) - timedelta(days=5)).strftime(
        "%a, %d %b %Y %H:%M:%S %z")
    newer = (datetime.now(UTC) - timedelta(days=4)).strftime(
        "%a, %d %b %Y %H:%M:%S %z")
    mine = _raw("m1", "t1", headers=[_h("Date", old)], labels=["SENT"])
    theirs = _raw("m2", "t1", headers=[_h("Date", newer)], labels=["INBOX"])
    service = FakeService(
        listing={"messages": [{"id": "m1"}]},
        by_id={"m1": mine},
        threads={"t1": {"id": "t1", "messages": [mine, theirs]}},
    )

    assert GmailClient(settings, service=service).threads_awaiting_reply() == []


def test_recent_threads_inside_the_window_are_not_flagged_yet(settings):
    """Loop must not nag about mail sent ten minutes ago."""
    just_now = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S %z")
    mine = _raw("m1", "t1", headers=[_h("Date", just_now)], labels=["SENT"])
    service = FakeService(
        listing={"messages": [{"id": "m1"}]},
        by_id={"m1": mine},
        threads={"t1": {"id": "t1", "messages": [mine]}},
    )

    assert GmailClient(settings, service=service).threads_awaiting_reply() == []


def test_send_posts_a_message_and_returns_the_id(settings):
    service = FakeService()
    client = GmailClient(settings, service=service)

    message_id = client.send(to=["a@b.c"], subject="Hi", body="Hello")

    assert message_id == "sent-1"
    assert "raw" in service.sent[0]


def test_send_threads_the_reply_when_given_a_thread_id(settings):
    service = FakeService()
    GmailClient(settings, service=service).send(
        to=["a@b.c"], subject="Re", body="x", thread_id="t1")

    assert service.sent[0]["threadId"] == "t1"


def test_send_refuses_an_empty_recipient_list(settings):
    client = GmailClient(settings, service=FakeService())
    with pytest.raises(ValueError):
        client.send(to=[], subject="Hi", body="Hello")
