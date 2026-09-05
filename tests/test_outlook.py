"""The Outlook mail and calendar connectors, and the shared Graph client.

Graph transport is exercised through ``httpx.MockTransport`` with a fake token
provider, so nothing here signs in or reaches the network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from config.settings import Settings
from core.exceptions import AuthRequiredError
from integrations.ms_graph import GraphClient
from integrations.outlook import (
    OutlookClient,
    _recipients,
    _sender,
    _to_message,
    build_graph_message,
)
from integrations.outlook_calendar import (
    OutlookCalendarClient,
    _parse_graph_time,
    _to_event,
)


class FakeToken:
    def token(self) -> str:
        return "fake-token"


def _graph(handler, **overrides) -> GraphClient:
    # _env_file is a pydantic-settings runtime kwarg, not a model field.
    settings = Settings(outlook_client_id="app-id", **overrides)  # type: ignore[call-arg]
    return GraphClient(settings, token_provider=FakeToken(),
                       transport=httpx.MockTransport(handler))


def _iso(hours_ago: int = 0) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


# --------------------------------------------------------------------------- #
# Graph client
# --------------------------------------------------------------------------- #
def test_graph_is_unconfigured_without_a_client_id():
    assert GraphClient(Settings(_env_file=None, outlook_client_id="")).configured is False


def test_graph_get_sends_the_bearer_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"value": []})

    _graph(handler).get("/me/messages")

    assert seen["auth"] == "Bearer fake-token"


def test_graph_get_raises_on_http_error():
    def handler(request):
        return httpx.Response(403, json={"error": "forbidden"})

    with pytest.raises(httpx.HTTPStatusError):
        _graph(handler).get("/me/messages")


def test_graph_unconfigured_get_raises_auth_required():
    client = GraphClient(Settings(_env_file=None, outlook_client_id=""))
    with pytest.raises(AuthRequiredError):
        client.get("/me/messages")


def test_graph_post_sends_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        seen["type"] = request.headers.get("Content-Type")
        return httpx.Response(202)

    _graph(handler).post("/me/sendMail", {"hello": "world"})

    assert '"hello": "world"' in seen["body"]
    assert seen["type"] == "application/json"


# --------------------------------------------------------------------------- #
# Mail normalisation
# --------------------------------------------------------------------------- #
def test_recipients_extracts_nested_addresses():
    entries = [{"emailAddress": {"address": "a@b.c"}},
               {"emailAddress": {"address": "d@e.f"}}]
    assert _recipients(entries) == ["a@b.c", "d@e.f"]


def test_recipients_tolerates_junk():
    assert _recipients([{"no": "address"}, "nonsense", None]) == []
    assert _recipients(None) == []


def test_sender_reads_the_nested_address():
    assert _sender({"from": {"emailAddress": {"address": "anna@acme.com"}}}) == \
        "anna@acme.com"


def test_sender_falls_back_to_the_sender_field():
    assert _sender({"sender": {"emailAddress": {"address": "x@y.z"}}}) == "x@y.z"


def test_sender_missing_returns_empty():
    assert _sender({}) == ""


def test_message_normalisation_matches_the_gmail_shape():
    raw = {
        "id": "AAA", "conversationId": "CCC", "subject": "Contract",
        "from": {"emailAddress": {"address": "anna@acme.com"}},
        "toRecipients": [{"emailAddress": {"address": "me@artdaw.com"}}],
        "ccRecipients": [{"emailAddress": {"address": "bob@acme.com"}}],
        "sentDateTime": "2026-09-01T10:00:00Z",
        "isRead": False,
        "bodyPreview": "Could you look at...",
    }

    message = _to_message(raw)

    assert message.message_id == "AAA"
    assert message.thread_id == "CCC"
    assert message.cc == ["bob@acme.com"]
    assert message.is_unread is True
    assert message.from_me is False


def test_my_own_address_marks_a_message_as_sent():
    raw = {"id": "A", "from": {"emailAddress": {"address": "Me@Artdaw.com"}}}
    message = _to_message(raw, my_addresses={"me@artdaw.com"})

    assert message.from_me is True
    assert "SENT" in message.labels  # same vocabulary as the Gmail connector


def test_read_message_has_no_unread_label():
    assert _to_message({"id": "A", "isRead": True}).is_unread is False


def test_build_graph_message_shape():
    payload = build_graph_message(to=["a@b.c"], subject="Hi", body="Hello",
                                  cc=["d@e.f"])

    assert payload["message"]["subject"] == "Hi"
    assert payload["message"]["toRecipients"][0]["emailAddress"]["address"] == "a@b.c"
    assert payload["message"]["ccRecipients"][0]["emailAddress"]["address"] == "d@e.f"
    assert payload["saveToSentItems"] is True


# --------------------------------------------------------------------------- #
# Mail transport
# --------------------------------------------------------------------------- #
def test_list_unread_returns_messages():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"mail": "me@artdaw.com"})
        return httpx.Response(200, json={"value": [
            {"id": "A", "subject": "Hello", "isRead": False},
        ]})

    client = OutlookClient(Settings(_env_file=None, outlook_client_id="x"),
                           graph=_graph(handler))
    messages = client.list_unread()

    assert [m.subject for m in messages] == ["Hello"]


def test_send_posts_to_sendmail():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(202)

    client = OutlookClient(Settings(_env_file=None, outlook_client_id="x"),
                           graph=_graph(handler))
    client.send(to=["a@b.c"], subject="Hi", body="Hello")

    assert seen["path"].endswith("/me/sendMail")


def test_send_refuses_an_empty_recipient_list():
    client = OutlookClient(Settings(_env_file=None, outlook_client_id="x"),
                           graph=_graph(lambda r: httpx.Response(202)))
    with pytest.raises(ValueError):
        client.send(to=[], subject="Hi", body="Hello")


def test_threads_awaiting_reply_flags_a_stalled_conversation():
    sent_old = _iso(hours_ago=200)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"mail": "me@artdaw.com"})
        if "sentitems" in request.url.path:
            return httpx.Response(200, json={"value": [
                {"id": "A", "conversationId": "C1", "sentDateTime": sent_old,
                 "from": {"emailAddress": {"address": "me@artdaw.com"}}},
            ]})
        return httpx.Response(200, json={"value": [
            {"id": "A", "conversationId": "C1", "subject": "Ping",
             "sentDateTime": sent_old,
             "from": {"emailAddress": {"address": "me@artdaw.com"}}},
        ]})

    client = OutlookClient(Settings(_env_file=None, outlook_client_id="x"),
                           graph=_graph(handler))
    stalled = client.threads_awaiting_reply()

    assert len(stalled) == 1
    assert stalled[0].thread_id == "C1"


def test_answered_conversations_are_not_flagged():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"mail": "me@artdaw.com"})
        if "sentitems" in request.url.path:
            return httpx.Response(200, json={"value": [
                {"id": "A", "conversationId": "C1", "sentDateTime": _iso(200),
                 "from": {"emailAddress": {"address": "me@artdaw.com"}}},
            ]})
        return httpx.Response(200, json={"value": [
            {"id": "A", "conversationId": "C1", "sentDateTime": _iso(200),
             "from": {"emailAddress": {"address": "me@artdaw.com"}}},
            {"id": "B", "conversationId": "C1", "sentDateTime": _iso(100),
             "from": {"emailAddress": {"address": "anna@acme.com"}}},
        ]})

    client = OutlookClient(Settings(_env_file=None, outlook_client_id="x"),
                           graph=_graph(handler))
    assert client.threads_awaiting_reply() == []


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #
def test_parses_graph_time_with_utc_zone():
    parsed = _parse_graph_time({"dateTime": "2026-09-05T09:30:00.0000000",
                                "timeZone": "UTC"})
    assert parsed.hour == 9
    assert parsed.tzinfo is not None


def test_parses_graph_time_with_offset():
    assert _parse_graph_time({"dateTime": "2026-09-05T09:30:00Z"}).hour == 9


def test_unparseable_graph_time_returns_none():
    assert _parse_graph_time({"dateTime": "nope"}) is None
    assert _parse_graph_time(None) is None
    assert _parse_graph_time({}) is None


def test_normalises_a_calendar_event():
    raw = {
        "id": "E1", "subject": "Design review",
        "start": {"dateTime": "2026-09-05T09:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-05T10:00:00", "timeZone": "UTC"},
        "location": {"displayName": "Room 3"},
        "attendees": [{"emailAddress": {"address": "a@b.c"}}],
    }

    event = _to_event(raw)

    assert event.title == "Design review"
    assert event.location == "Room 3"
    assert event.attendees == ["a@b.c"]


def test_event_without_id_or_start_is_dropped():
    assert _to_event({"subject": "no id"}) is None
    assert _to_event({"id": "E1"}) is None


def test_private_sensitivity_marks_an_event_personal():
    raw = {"id": "E1", "subject": "Therapy", "sensitivity": "private",
           "start": {"dateTime": "2026-09-05T09:00:00Z"}}
    assert _to_event(raw).is_personal is True


def test_private_flag_can_be_disabled():
    raw = {"id": "E1", "sensitivity": "private",
           "start": {"dateTime": "2026-09-05T09:00:00Z"}}
    assert _to_event(raw, treat_private_as_personal=False).is_personal is False


def test_normal_sensitivity_is_not_personal():
    raw = {"id": "E1", "sensitivity": "normal",
           "start": {"dateTime": "2026-09-05T09:00:00Z"}}
    assert _to_event(raw).is_personal is False


def test_todays_events_uses_calendar_view_to_expand_recurrence():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"value": []})

    client = OutlookCalendarClient(Settings(_env_file=None, outlook_client_id="x"),
                                    graph=_graph(handler))
    client.todays_events()

    assert seen["path"].endswith("/me/calendarView")


def test_cancelled_events_are_skipped():
    def handler(request):
        return httpx.Response(200, json={"value": [
            {"id": "E1", "subject": "Gone", "isCancelled": True,
             "start": {"dateTime": "2026-09-05T09:00:00Z"}},
            {"id": "E2", "subject": "Real",
             "start": {"dateTime": "2026-09-05T10:00:00Z"}},
        ]})

    client = OutlookCalendarClient(Settings(_env_file=None, outlook_client_id="x"),
                                    graph=_graph(handler))
    assert [e.title for e in client.todays_events()] == ["Real"]


def test_events_are_sorted_by_start():
    def handler(request):
        return httpx.Response(200, json={"value": [
            {"id": "E1", "subject": "Late",
             "start": {"dateTime": "2026-09-05T16:00:00Z"}},
            {"id": "E2", "subject": "Early",
             "start": {"dateTime": "2026-09-05T08:00:00Z"}},
        ]})

    client = OutlookCalendarClient(Settings(_env_file=None, outlook_client_id="x"),
                                    graph=_graph(handler))
    assert [e.title for e in client.todays_events()] == ["Early", "Late"]
