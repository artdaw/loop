"""The vNext HTTP API (interfaces §4), exercised through a real ASGI dispatch.

Uses FastAPI's `TestClient` — the HTTP-layer equivalent of Typer's
`CliRunner` used for the CLI — against a real temporary database, not by
calling route functions directly in Python.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import loop.interfaces.http as http_module
from loop.interfaces.http import app

TOKEN = "test-token"


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'loop.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OLLAMA_DEFAULT_MODEL", "llama3.1:8b")
    monkeypatch.setenv("CAPABILITY_PATHS", json.dumps([str(tmp_path / "packs")]))
    monkeypatch.setenv("API_BEARER_TOKEN", TOKEN)
    monkeypatch.chdir(tmp_path)
    app.state.application = None
    return tmp_path


@pytest.fixture
def client(env) -> TestClient:
    return TestClient(app)


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Health stays open
# --------------------------------------------------------------------------- #
def test_health_live_needs_no_token():
    client = TestClient(app)
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_health_ready_reports_ready_against_a_real_db(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


# --------------------------------------------------------------------------- #
# Auth (interfaces §4: bearer token required even on localhost)
# --------------------------------------------------------------------------- #
def test_content_routes_reject_no_token(client):
    response = client.get("/api/v1/tasks")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_required"


def test_content_routes_reject_a_wrong_token(client):
    response = client.get("/api/v1/tasks", headers=auth("wrong"))
    assert response.status_code == 401


def test_a_blank_configured_token_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'loop.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CAPABILITY_PATHS", json.dumps([str(tmp_path / "packs")]))
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    app.state.application = None
    client = TestClient(app)

    response = client.get("/api/v1/tasks", headers=auth("anything"))
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Envelope shape (interfaces §4)
# --------------------------------------------------------------------------- #
def test_success_envelope_has_data_request_id_and_warnings(client):
    response = client.get("/api/v1/status", headers=auth())
    body = response.json()
    assert set(body) == {"data", "request_id", "warnings"}
    assert body["warnings"] == []


def test_error_envelope_matches_the_structured_taxonomy(client):
    response = client.post(
        "/api/v1/tasks/nonexistent/complete", headers=auth(),
        json={"expected_version": 1})
    assert response.status_code in (400, 404)
    body = response.json()
    assert set(body["error"]) == {"code", "message", "details"}
    assert "request_id" in body


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
def test_a_task_can_be_created_and_listed(client):
    create = client.post("/api/v1/tasks", headers=auth(),
                         json={"title": "water the fern"})
    assert create.status_code == 201
    task_id = create.json()["data"]["id"]

    listed = client.get("/api/v1/tasks", headers=auth())
    ids = [t["id"] for t in listed.json()["data"]["items"]]
    assert task_id in ids


def test_a_task_can_be_completed(client):
    create = client.post("/api/v1/tasks", headers=auth(),
                         json={"title": "water the fern"})
    task_id = create.json()["data"]["id"]

    complete = client.post(f"/api/v1/tasks/{task_id}/complete", headers=auth(),
                           json={"expected_version": 1})
    assert complete.status_code == 200
    assert complete.json()["data"]["status"] == "done"


def test_completing_with_a_stale_version_is_a_conflict(client):
    create = client.post("/api/v1/tasks", headers=auth(),
                         json={"title": "water the fern"})
    task_id = create.json()["data"]["id"]
    client.post(f"/api/v1/tasks/{task_id}/complete", headers=auth(),
               json={"expected_version": 1})

    stale = client.post(f"/api/v1/tasks/{task_id}/complete", headers=auth(),
                        json={"expected_version": 1})
    assert stale.status_code == 409


# --------------------------------------------------------------------------- #
# Capabilities, against a real shipped pack directory
# --------------------------------------------------------------------------- #
def _install_a_real_pack(env: Path) -> None:
    from tests.vnext.pack_fixtures import build_plant_care_pack

    build_plant_care_pack(env / "packs")


def test_capability_list_discovers_a_real_pack_directory(client, env):
    _install_a_real_pack(env)
    response = client.get("/api/v1/capabilities", headers=auth())
    keys = [c["pack_key"] for c in response.json()["data"]["items"]]
    assert any("plantcare" in k for k in keys)


def test_capability_enable_and_get_through_the_real_routes(client, env):
    _install_a_real_pack(env)
    enable = client.post("/api/v1/capabilities/plantcare/enable", headers=auth(),
                         json={"version": "1.0.0"})
    assert enable.status_code == 200
    assert enable.json()["data"]["enabled"] is True

    fetched = client.get("/api/v1/capabilities/plantcare", headers=auth())
    assert fetched.json()["data"]["enabled"] is True


def test_capability_disable_through_the_real_route(client, env):
    _install_a_real_pack(env)
    client.post("/api/v1/capabilities/plantcare/enable", headers=auth(),
               json={"version": "1.0.0"})

    disable = client.post("/api/v1/capabilities/plantcare/disable", headers=auth())
    assert "plantcare" in " ".join(disable.json()["data"]["disabled"])

    fetched = client.get("/api/v1/capabilities/plantcare", headers=auth())
    assert fetched.json()["data"]["enabled"] is False


def test_enabling_an_unknown_pack_is_a_real_error(client):
    response = client.post("/api/v1/capabilities/nonexistent/enable",
                           headers=auth(), json={"version": "1.0.0"})
    assert response.status_code == 400


def test_getting_an_unknown_capability_is_404_shaped_as_invalid_input(client):
    response = client.get("/api/v1/capabilities/nonexistent", headers=auth())
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"


# --------------------------------------------------------------------------- #
# Generic invocation (agent-stack §2: no new route per pack)
# --------------------------------------------------------------------------- #
def test_invoke_runs_an_enabled_capability_by_name(client, env, monkeypatch):
    _install_a_real_pack(env)
    client.post("/api/v1/capabilities/plantcare/enable", headers=auth(),
               json={"version": "1.0.0"})

    from langchain_core.messages import AIMessage

    from tests.vnext.test_capability_runners import ToolCallingFakeChatModel

    real_application = http_module._application

    def patched() -> object:
        application = real_application()
        from loop.ai.model_gateway import ModelGateway

        application.invoker.gateway = ModelGateway(
            settings=application.settings,
            local_model=ToolCallingFakeChatModel(messages=iter(
                [AIMessage(content=json.dumps({"answer": "watered",
                                              "sources": []}))])))
        return application

    monkeypatch.setattr(http_module, "_application", patched)
    app.state.application = None

    response = client.post("/api/v1/capabilities/plantcare.advise/invoke",
                           headers=auth(), json={"arguments": {"query": "fern"}})
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "succeeded"
    assert body["summary"]["answer"] == "watered"


def test_invoking_a_disabled_operation_is_a_real_error(client):
    response = client.post("/api/v1/capabilities/nonexistent.op/invoke",
                           headers=auth(), json={"arguments": {}})
    assert response.status_code >= 400


# --------------------------------------------------------------------------- #
# Captures, questions and routines (M5)
# --------------------------------------------------------------------------- #
@pytest.fixture
def vault_client(env: Path, monkeypatch) -> TestClient:
    from tests.vnext.vault_fixtures import build_minimal_vault

    build_minimal_vault(env / "vault")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(env / "vault"))
    app.state.application = None
    return TestClient(app)


ROUTINE_DOCUMENT = """---
schema_version: 1
id: rain-check
title: Rain check
trigger:
  kind: at
  timezone: Europe/Berlin
steps:
  - capability: weather.prepare
    arguments:
      location_ref: home
notification:
  mode: each_occurrence
  destination: "4242"
---

Fires once.
"""


def test_every_m5_route_requires_the_bearer_token(vault_client):
    """A new route inheriting the auth dependency is not something to assume."""
    assert vault_client.post("/api/v1/captures", json={"body": "x"}
                             ).status_code == 401
    assert vault_client.get("/api/v1/questions?q=x").status_code == 401
    assert vault_client.post("/api/v1/compile", json={}).status_code == 401
    assert vault_client.get("/api/v1/routines").status_code == 401
    assert vault_client.post("/api/v1/routines/x/activate",
                             json={"activation_event_id": "e"}
                             ).status_code == 401


def test_a_capture_reports_the_outcome_it_actually_achieved(vault_client):
    response = vault_client.post("/api/v1/captures",
                                 json={"body": "Six week lead time."},
                                 headers=auth())

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["status"] == "saved"
    assert data["registered"] is True
    assert data["path"].startswith("0-raw/inbox/")
    assert data["message"].startswith("Saved to your vault")


def test_a_question_comes_back_with_its_citations(vault_client):
    vault_client.post("/api/v1/captures",
                      json={"body": "Anodised brackets arrive Thursday."},
                      headers=auth())

    response = vault_client.get("/api/v1/questions?q=anodised brackets",
                                headers=auth())

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["knowledge_gap"] is False
    assert data["citations"]
    # No compile has run, so the only honest label for this is uncompiled.
    assert all(c["uncompiled"] for c in data["citations"])


def test_a_question_with_nothing_behind_it_says_so(vault_client):
    response = vault_client.get("/api/v1/questions?q=quarterly revenue",
                                headers=auth())

    assert response.json()["data"]["knowledge_gap"] is True
    assert response.json()["data"]["citations"] == []


def test_vault_routes_report_unavailable_when_no_vault_is_configured(client):
    """Not an empty result: an unconfigured vault is a different fact."""
    response = client.get("/api/v1/questions?q=anything", headers=auth())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"


def test_compiling_with_no_model_reports_the_gap_rather_than_failing(vault_client,
                                                                     monkeypatch):
    monkeypatch.delenv("OLLAMA_DEFAULT_MODEL", raising=False)
    app.state.application = None
    client = TestClient(app)
    client.post("/api/v1/captures", json={"body": "Six week lead time."},
                headers=auth())

    response = client.post("/api/v1/compile", json={}, headers=auth())

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert items and items[0]["outcome"] == "deferred"
    assert "No model is configured" in items[0]["reason"]


def test_a_routine_can_be_listed_activated_and_paused_over_http(vault_client, env):
    from loop.app import build_application
    from loop.runtime.routines import parse_routine

    application = build_application()
    routine, problems = parse_routine(
        ROUTINE_DOCUMENT, known_capabilities=application.known_capabilities())
    assert problems == []
    application.routines.save(routine)

    listed = vault_client.get("/api/v1/routines", headers=auth()).json()["data"]
    assert listed["items"][0]["status"] == "proposed"
    assert listed["items"][0]["next_run"] is None

    activated = vault_client.post("/api/v1/routines/rain-check/activate",
                                  json={"activation_event_id": "evt-1"},
                                  headers=auth())
    assert activated.status_code == 200
    assert activated.json()["data"]["status"] == "active"
    assert activated.json()["data"]["next_run"] is not None

    paused = vault_client.post("/api/v1/routines/rain-check/pause", headers=auth())
    assert paused.json()["data"]["status"] == "paused"
    assert paused.json()["data"]["next_run"] is None


def test_activating_a_routine_requires_naming_the_authorising_event(vault_client):
    response = vault_client.post("/api/v1/routines/rain-check/activate",
                                 json={}, headers=auth())

    assert response.status_code == 422


def test_recapturing_the_same_note_is_reported_as_a_duplicate(vault_client):
    """The same words twice is one note, and the reply says which it was.

    A client that saw "saved" both times would show the owner two captures
    where the vault holds one.
    """
    body = {"body": "Six week lead time, confirmed."}
    first = vault_client.post("/api/v1/captures", json=body, headers=auth())

    second = vault_client.post("/api/v1/captures", json=body, headers=auth())

    assert first.json()["data"]["status"] == "saved"
    assert second.json()["data"]["status"] == "duplicate"
    assert second.json()["data"]["path"] == first.json()["data"]["path"]
    assert second.json()["data"]["message"].startswith("Already saved")


# --------------------------------------------------------------------------- #
# Idempotency, conflict and not-found on the real API (T15, O08 — M7)
# --------------------------------------------------------------------------- #
def test_a_retried_create_applies_once_and_replays_its_answer(vault_client):
    """A client retries when it never saw the reply — the task already exists."""
    body = {"title": "call the repair shop"}
    headers = {**auth(), "Idempotency-Key": "req-1"}

    first = vault_client.post("/api/v1/tasks", json=body, headers=headers)
    second = vault_client.post("/api/v1/tasks", json=body, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["data"]["id"] == first.json()["data"]["id"]
    assert any("replayed" in warning for warning in second.json()["warnings"])

    listed = vault_client.get("/api/v1/tasks", headers=auth()).json()["data"]
    assert len(listed["items"]) == 1, "the retry created a second task"


def test_the_same_key_with_a_different_body_is_a_conflict(vault_client):
    """T15. Replaying the first answer would discard the second request."""
    headers = {**auth(), "Idempotency-Key": "req-1"}
    vault_client.post("/api/v1/tasks", json={"title": "first"}, headers=headers)

    clash = vault_client.post("/api/v1/tasks", json={"title": "second"},
                              headers=headers)

    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "conflict"
    listed = vault_client.get("/api/v1/tasks", headers=auth()).json()["data"]
    assert [item["title"] for item in listed["items"]] == ["first"]


def test_idempotency_survives_a_restart(vault_client, env):
    """The record must outlive the process that made it.

    A retry happens exactly when the client saw no response, which includes the
    server dying after it committed. An in-memory record would have forgotten
    the mutation in the one case that produces retries.
    """
    from loop.interfaces.http import app as http_app

    headers = {**auth(), "Idempotency-Key": "req-1"}
    first = vault_client.post("/api/v1/tasks", json={"title": "survive"},
                              headers=headers)

    http_app.state.application = None          # a new process, same database
    restarted = TestClient(http_app)
    second = restarted.post("/api/v1/tasks", json={"title": "survive"},
                            headers=headers)

    assert second.json()["data"]["id"] == first.json()["data"]["id"]
    listed = restarted.get("/api/v1/tasks", headers=auth()).json()["data"]
    assert len(listed["items"]) == 1


def test_a_mutation_without_a_key_is_not_deduplicated(vault_client):
    """Opting out is allowed; it must not silently behave as if opted in."""
    body = {"title": "twice"}
    vault_client.post("/api/v1/tasks", json=body, headers=auth())
    vault_client.post("/api/v1/tasks", json=body, headers=auth())

    listed = vault_client.get("/api/v1/tasks", headers=auth()).json()["data"]
    assert len(listed["items"]) == 2


def test_an_unknown_task_is_not_found_and_stays_that_way(vault_client):
    response = vault_client.post("/api/v1/tasks/no-such-id/complete",
                                 json={"expected_version": 1}, headers=auth())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_replayed_completion_does_not_complete_twice(vault_client):
    created = vault_client.post("/api/v1/tasks", json={"title": "done once"},
                                headers=auth()).json()["data"]
    headers = {**auth(), "Idempotency-Key": "done-1"}

    first = vault_client.post(f"/api/v1/tasks/{created['id']}/complete",
                              json={"expected_version": created["version"]},
                              headers=headers)
    second = vault_client.post(f"/api/v1/tasks/{created['id']}/complete",
                               json={"expected_version": created["version"]},
                               headers=headers)

    assert first.status_code == 200
    # Without the key this would be a 409: the version moved on when it
    # completed. The replay returns the original answer instead.
    assert second.status_code == 200
    assert second.json()["data"]["version"] == first.json()["data"]["version"]


def test_an_empty_idempotency_key_is_no_key_not_a_shared_bucket(vault_client):
    """An empty header value must not collapse unrelated requests into one.

    A client that sets `Idempotency-Key:` with nothing after it has supplied no
    key. Storing under the empty string instead would make every such request
    share one record, so the second unrelated create would replay the first
    one's answer and quietly never happen.
    """
    headers = {**auth(), "Idempotency-Key": ""}

    vault_client.post("/api/v1/tasks", json={"title": "first"}, headers=headers)
    vault_client.post("/api/v1/tasks", json={"title": "second"}, headers=headers)

    listed = vault_client.get("/api/v1/tasks", headers=auth()).json()["data"]
    assert sorted(item["title"] for item in listed["items"]) == ["first", "second"]
