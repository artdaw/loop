"""The Wrike REST client.

Every test runs against ``httpx.MockTransport`` — no network, no API key
required, and the request assertions pin the wire format.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from config.settings import Settings
from core.exceptions import WrikeNotConfiguredError
from integrations.wrike import WrikeClient, WrikeTask


def _client(handler) -> WrikeClient:
    client = WrikeClient(Settings(wrike_api_key="test-key"))
    client._http = httpx.AsyncClient(
        base_url=WrikeClient.BASE_URL,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer test-key"},
    )
    return client


def test_unconfigured_client_reports_not_configured():
    assert WrikeClient(Settings(wrike_api_key="")).configured is False


def test_whitespace_key_is_not_configured():
    assert WrikeClient(Settings(wrike_api_key="   ")).configured is False


def test_configured_client_reports_configured():
    assert WrikeClient(Settings(wrike_api_key="abc")).configured is True


async def test_unconfigured_list_tasks_raises():
    client = WrikeClient(Settings(wrike_api_key=""))
    with pytest.raises(WrikeNotConfiguredError):
        await client.list_tasks()


async def test_unconfigured_create_task_raises():
    client = WrikeClient(Settings(wrike_api_key=""))
    with pytest.raises(WrikeNotConfiguredError):
        await client.create_task(title="nope")


async def test_list_tasks_normalises_the_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.url.path.endswith("/tasks")
        return httpx.Response(200, json={"data": [{
            "id": "IEAAB",
            "title": "Ship it",
            "status": "Active",
            "dates": {"due": "2026-09-10"},
            "updatedDate": "2026-09-01T10:00:00Z",
            "permalink": "https://www.wrike.com/open.htm?id=1",
        }]})

    tasks = await _client(handler).list_tasks()

    assert len(tasks) == 1
    assert tasks[0].task_id == "IEAAB"
    assert tasks[0].title == "Ship it"
    assert tasks[0].due == date(2026, 9, 10)
    assert tasks[0].status == "Active"
    assert tasks[0].permalink.endswith("id=1")


async def test_list_tasks_tolerates_missing_optional_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "X", "title": "Bare"}]})

    task = (await _client(handler).list_tasks())[0]

    assert task.due is None
    assert task.updated_at is None
    assert task.permalink == ""


async def test_list_tasks_skips_entries_without_an_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            {"title": "no id"},
            {"id": "OK", "title": "fine"},
        ]})

    tasks = await _client(handler).list_tasks()

    assert [t.task_id for t in tasks] == ["OK"]


async def test_list_tasks_passes_updated_since_filter():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    await _client(handler).list_tasks(updated_since=date(2026, 9, 1))

    assert "updatedDate" in seen


async def test_create_task_posts_and_returns_the_task():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"data": [
            {"id": "NEW1", "title": "Created", "status": "Active"},
        ]})

    task = await _client(handler).create_task(title="Created")

    assert captured["method"] == "POST"
    assert captured["params"]["title"] == "Created"
    assert task.task_id == "NEW1"


async def test_create_task_in_a_folder_uses_the_folder_endpoint():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"data": [{"id": "N", "title": "t"}]})

    await _client(handler).create_task(title="t", folder_id="FOLDER1")

    assert "folders/FOLDER1/tasks" in captured["path"]


async def test_create_task_sends_the_due_date():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"data": [{"id": "N", "title": "t"}]})

    await _client(handler).create_task(title="t", due=date(2026, 9, 30))

    assert "2026-09-30" in captured["dates"]


async def test_complete_task_puts_completed_status():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"data": [
            {"id": "W1", "title": "done", "status": "Completed"},
        ]})

    task = await _client(handler).complete_task("W1")

    assert captured["method"] == "PUT"
    assert captured["params"]["status"] == "Completed"
    assert task.status == "Completed"


async def test_update_task_changes_the_title():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"data": [{"id": "W1", "title": "renamed"}]})

    task = await _client(handler).update_task("W1", title="renamed")

    assert captured["title"] == "renamed"
    assert task.title == "renamed"


async def test_http_error_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "not_authorized"})

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).list_tasks()


async def test_empty_response_data_raises_for_single_task_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    with pytest.raises(RuntimeError):
        await _client(handler).create_task(title="nothing comes back")


def test_wrike_task_is_comparable_by_value():
    a = WrikeTask(task_id="1", title="t")
    b = WrikeTask(task_id="1", title="t")
    assert a == b
