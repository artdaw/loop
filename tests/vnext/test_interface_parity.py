"""One service, three transports (EX07, TR24, WF24, O07 — M7).

Interfaces §5's rule is that a surface is a *transport, not a behaviour*: if
the CLI, the HTTP API and the Telegram bot each implement "complete a task"
themselves, they will drift, and the one used least often will be the one that
is wrong. Every other test in this suite exercises one surface at a time and
therefore cannot see that drift at all.

So these tests do the same thing three ways and compare the outcomes against
each other — not against a fixture. A parity test that asserts three hardcoded
expected strings passes happily while all three surfaces are wrong together.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from telegram import Chat, Message, Update, User
from typer.testing import CliRunner

import loop.interfaces.http as http_module
from loop.app import build_application
from loop.interfaces.cli import app as cli_app
from loop.interfaces.http import UI_COOKIE
from loop.interfaces.telegram import LoopTelegramBot

OWNER = 42
TOKEN = "parity-token"
runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> Path:
    """One database and one vault, reached by all three interfaces."""
    from tests.vnext.vault_fixtures import build_minimal_vault

    vault = build_minimal_vault(tmp_path / "vault")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'loop.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault.root))
    monkeypatch.setenv("OLLAMA_DEFAULT_MODEL", "llama3.1:8b")
    monkeypatch.setenv("CAPABILITY_PATHS",
                       json.dumps([str(Path.cwd() / "packs")]))
    monkeypatch.setenv("API_BEARER_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "parity-bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(OWNER))
    monkeypatch.setenv("TELEGRAM_USER_ID", str(OWNER))
    monkeypatch.chdir(tmp_path)
    http_module.app.state.application = None
    return tmp_path


@pytest.fixture
def api(env) -> TestClient:
    return TestClient(http_module.app)


@pytest.fixture
def ui(env) -> TestClient:
    """A browser-shaped client: the session cookie set once, as a browser does.

    Kept separate from `api` so the unauthenticated tests still have a client
    with no cookie at all.
    """
    client = TestClient(http_module.app)
    client.cookies.set(UI_COOKIE, TOKEN)
    return client


@pytest.fixture
def bot(env) -> LoopTelegramBot:
    class FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        async def initialize(self) -> None: ...
        async def shutdown(self) -> None: ...

        async def send_message(self, *, chat_id: int, text: str) -> None:
            self.sent.append((chat_id, text))

        async def get_updates(self, *, offset, timeout, allowed_updates):
            del offset, timeout, allowed_updates
            return []

    instance = LoopTelegramBot(build_application())
    instance.bot = FakeBot()
    return instance


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


async def say(bot: LoopTelegramBot, text: str, *, update_id: int) -> str:
    user = User(id=OWNER, first_name="Owner", is_bot=False)
    chat = Chat(id=OWNER, type="private")
    message = Message(message_id=update_id, date=dt.datetime.now(dt.UTC),
                      chat=chat, from_user=user, text=text)
    await bot._handle_update(Update(update_id=update_id, message=message))
    sent: list[tuple[int, str]] = bot.bot.sent      # type: ignore[attr-defined]
    return sent[-1][1] if sent else ""


# --------------------------------------------------------------------------- #
# EX07 — one generic invocation route, three transports
# --------------------------------------------------------------------------- #
async def test_the_same_capability_call_returns_the_same_output_everywhere(
        env, api, bot):
    """The pack is invoked by name; no surface has a command of its own."""
    arguments = {"query": "lead time"}

    cli = runner.invoke(cli_app, ["do", "vault.search", json.dumps(arguments)])
    http = api.post("/api/v1/capabilities/vault.search/invoke",
                    json={"arguments": arguments}, headers=auth())
    chat = await say(bot, f"/do vault.search {json.dumps(arguments)}",
                     update_id=1)

    assert cli.exit_code == 0
    assert http.status_code == 200

    # Each surface formats for its medium; the *answer* must be one answer.
    cli_body = json.loads(cli.stdout)
    http_body = http.json()["data"]
    chat_body = json.loads(chat)
    gaps = {cli_body.get("knowledge_gap"), http_body.get("knowledge_gap"),
            chat_body.get("knowledge_gap")}
    assert len(gaps) == 1, f"surfaces disagree about the result: {gaps}"


async def test_an_unknown_operation_fails_the_same_way_on_every_surface(
        env, api, bot):
    cli = runner.invoke(cli_app, ["do", "nope.missing", "{}"])
    http = api.post("/api/v1/capabilities/nope.missing/invoke",
                    json={"arguments": {}}, headers=auth())
    chat = await say(bot, "/do nope.missing {}", update_id=1)

    assert cli.exit_code != 0
    assert http.status_code == 503
    assert http.json()["error"]["code"] == "unavailable"
    # All three name the operation rather than reporting a generic failure.
    assert "nope.missing" in cli.stderr
    assert "nope.missing" in http.json()["error"]["message"]
    assert "nope.missing" in chat


def test_malformed_arguments_are_refused_before_anything_runs(env, api, bot):
    cli = runner.invoke(cli_app, ["do", "vault.search", "not-json"])
    http = api.post("/api/v1/capabilities/vault.search/invoke",
                    json={"arguments": "not-an-object"}, headers=auth())

    assert cli.exit_code == 2
    assert "JSON" in cli.stderr
    assert http.status_code == 422


# --------------------------------------------------------------------------- #
# TR24 — equivalent requests and replays across surfaces
# --------------------------------------------------------------------------- #
async def test_a_task_created_on_one_surface_is_the_same_task_on_the_others(
        env, api, bot):
    """Not three copies: one row, seen three ways."""
    created = api.post("/api/v1/tasks", json={"title": "call the shop"},
                       headers=auth()).json()["data"]

    listed = runner.invoke(cli_app, ["task", "list"])
    chat = await say(bot, "/tasks", update_id=1)

    assert created["id"] in listed.stdout
    assert created["id"] in chat
    assert "call the shop" in listed.stdout
    assert "call the shop" in chat


async def test_completing_on_one_surface_is_visible_on_the_others(env, api, bot):
    created = api.post("/api/v1/tasks", json={"title": "one effect"},
                       headers=auth()).json()["data"]

    await say(bot, f"/done {created['id']}", update_id=1)

    remaining = api.get("/api/v1/tasks?status=ready", headers=auth())
    assert created["id"] not in [t["id"] for t in
                                 remaining.json()["data"]["items"]]
    listed = runner.invoke(cli_app, ["task", "list", "--status", "ready"])
    assert created["id"] not in listed.stdout


async def test_a_replayed_request_produces_one_logical_effect(env, api, bot):
    """Each transport deduplicates its own replays, by its own identity."""
    api.post("/api/v1/tasks", json={"title": "http once"},
             headers={**auth(), "Idempotency-Key": "k1"})
    api.post("/api/v1/tasks", json={"title": "http once"},
             headers={**auth(), "Idempotency-Key": "k1"})

    await say(bot, "/task telegram once", update_id=7)
    await say(bot, "/task telegram once", update_id=7)       # same update id

    titles = [t["title"] for t in
              api.get("/api/v1/tasks", headers=auth()).json()["data"]["items"]]
    assert titles.count("http once") == 1
    assert titles.count("telegram once") == 1


def test_a_stale_version_is_a_conflict_on_every_surface(env, api):
    created = api.post("/api/v1/tasks", json={"title": "versioned"},
                       headers=auth()).json()["data"]
    api.post(f"/api/v1/tasks/{created['id']}/complete",
             json={"expected_version": created["version"]}, headers=auth())

    http = api.post(f"/api/v1/tasks/{created['id']}/complete",
                    json={"expected_version": created["version"]}, headers=auth())
    cli = runner.invoke(cli_app, ["task", "complete", created["id"],
                                  str(created["version"])])

    assert http.status_code == 409
    assert http.json()["error"]["code"] == "conflict"
    # The CLI's exit code comes from the same taxonomy, so the two cannot
    # disagree about what a stale write means.
    assert cli.exit_code == 5


# --------------------------------------------------------------------------- #
# WF24 — one weather bundle, however it is asked for
# --------------------------------------------------------------------------- #
async def test_every_surface_asks_weather_the_same_question(env, api, bot):
    """No location is configured, so all three must ask rather than guess.

    Identical *refusals* are as much parity as identical answers: a surface
    that guessed a city here would be the one that is wrong, and nothing
    else in the suite compares them.
    """
    cli = runner.invoke(cli_app, ["do", "weather.prepare", "{}"])
    http = api.post("/api/v1/capabilities/weather.prepare/invoke",
                    json={"arguments": {}}, headers=auth())
    chat = await say(bot, "/weather", update_id=1)

    assert cli.exit_code != 0
    assert http.status_code == 400
    assert "location" in cli.stderr.lower()
    assert "location" in http.json()["error"]["message"].lower()
    assert "location" in chat.lower()


def test_the_error_envelope_is_the_same_shape_for_every_failure(env, api):
    """interfaces §4: one envelope, so clients cannot special-case routes."""
    failures = [
        api.get("/api/v1/questions?q=x", headers=auth()),          # may be 200
        api.post("/api/v1/tasks/missing/complete",
                 json={"expected_version": 1}, headers=auth()),    # 404
        api.post("/api/v1/capabilities/nope/invoke",
                 json={"arguments": {}}, headers=auth()),          # 503
    ]

    for response in failures:
        body = response.json()
        if response.status_code >= 400:
            assert set(body) == {"error", "request_id"}
            assert set(body["error"]) >= {"code", "message"}
        else:
            assert set(body) == {"data", "request_id", "warnings"}


# --------------------------------------------------------------------------- #
# O07 — the HTML surface reaches the same service, under its own protections
# --------------------------------------------------------------------------- #
def test_the_web_page_lists_the_same_tasks_the_api_does(env, api, ui):
    created = api.post("/api/v1/tasks", json={"title": "shared row"},
                       headers=auth()).json()["data"]

    page = ui.get("/ui/tasks")

    assert page.status_code == 200
    assert "shared row" in page.text
    assert created["id"] in page.text


def test_a_form_post_applies_and_redirects(env, api, ui):
    """303 so that refreshing the result page does not repeat the mutation."""
    created = api.post("/api/v1/tasks", json={"title": "finish me"},
                       headers=auth()).json()["data"]
    page = ui.get("/ui/tasks").text
    token = page.split('name=\'csrf_token\' value=\'')[1].split("'")[0]

    response = ui.post(f"/ui/tasks/{created['id']}/complete",
                       data={"csrf_token": token,
                             "expected_version": created["version"]},
                       follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/tasks"
    # The same row the API sees — one object, not a second copy.
    remaining = api.get("/api/v1/tasks?status=ready", headers=auth())
    assert created["id"] not in [t["id"] for t in
                                 remaining.json()["data"]["items"]]


def test_a_form_without_a_csrf_token_is_refused(env, api, ui):
    created = api.post("/api/v1/tasks", json={"title": "protected"},
                       headers=auth()).json()["data"]

    response = ui.post(f"/ui/tasks/{created['id']}/complete",
                       data={"csrf_token": "",
                             "expected_version": created["version"]},
                       follow_redirects=False)

    assert response.status_code == 401
    listed = api.get("/api/v1/tasks?status=ready", headers=auth())
    assert created["id"] in [t["id"] for t in listed.json()["data"]["items"]]


def test_a_csrf_token_from_another_session_is_refused(env, api, ui):
    from loop.api.service import issue_csrf_token

    created = api.post("/api/v1/tasks", json={"title": "protected"},
                       headers=auth()).json()["data"]
    forged = issue_csrf_token("someone-elses-session", secret=TOKEN)

    response = ui.post(f"/ui/tasks/{created['id']}/complete",
                       data={"csrf_token": forged,
                             "expected_version": created["version"]},
                       follow_redirects=False)

    assert response.status_code == 401


def test_the_web_surface_is_not_open_just_because_it_is_local(env, api):
    """"It only listens on localhost" is not authentication (O07)."""
    assert api.get("/ui/tasks").status_code == 401

    created = api.post("/api/v1/tasks", json={"title": "unprotected?"},
                       headers=auth()).json()["data"]
    response = api.post(f"/ui/tasks/{created['id']}/complete",
                        data={"csrf_token": "anything", "expected_version": 1},
                        follow_redirects=False)
    assert response.status_code == 401


def test_a_stale_form_cannot_overwrite_a_newer_edit(env, api, ui):
    """The form carries expected_version, so the API's conflict rule applies."""
    created = api.post("/api/v1/tasks", json={"title": "raced"},
                       headers=auth()).json()["data"]
    page = ui.get("/ui/tasks").text
    token = page.split('name=\'csrf_token\' value=\'')[1].split("'")[0]
    api.post(f"/api/v1/tasks/{created['id']}/complete",
             json={"expected_version": created["version"]}, headers=auth())

    response = ui.post(f"/ui/tasks/{created['id']}/complete",
                       data={"csrf_token": token,
                             "expected_version": created["version"]},
                       follow_redirects=False)

    assert response.status_code == 409
