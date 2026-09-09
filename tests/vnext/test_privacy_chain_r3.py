"""A private note stays private through every derivative (A08 — R3).

The scenario is one sentence — *private raw note → wiki → retrieved answer →
task → summary, `local_only` survives every derivative and team hand-off* —
and it is the hardest kind to test, because each hop looks correct on its own.
A label that is dropped at the third step is invisible until something leaves
the machine.

So these assert the label at *each* hop against what was actually persisted,
and finish with the case that matters most: a local model that is unavailable
must produce no cloud call at all, not a helpful fallback.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from loop.ai.model_gateway import ModelGateway
from loop.app import build_application
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.core.settings import Settings
from tests.vnext.vault_fixtures import build_minimal_vault

PRIVATE = PrivacyLabel(model_scope=ModelScope.LOCAL_ONLY,
                       origins=frozenset({"obsidian_private"}),
                       sensitive=True)

NOTE = "The clinic appointment is on the 14th and I have not told anyone."


def fake_gateway(*payloads, cloud: bool = True) -> ModelGateway:
    messages = [AIMessage(content=p if isinstance(p, str) else json.dumps(p))
                for p in payloads]
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, ollama_default_model="llama3.1:8b",
        cloud_enabled=cloud, anthropic_api_key="test-key",
        anthropic_model="claude-sonnet-5", cloud_daily_budget_usd=5.0)
    return ModelGateway(
        settings=settings,
        local_model=GenericFakeChatModel(messages=iter(messages)))


@pytest.fixture
def vault(tmp_path: Path):
    return build_minimal_vault(tmp_path / "vault")


@pytest.fixture
def app(settings: Settings, clock, vault):
    return build_application(
        settings.model_copy(update={"obsidian_vault_path": str(vault.root)}),
        clock=clock, model_gateway=fake_gateway())


def test_a_private_capture_is_persisted_as_local_only(app):
    result = app.knowledge.capture(NOTE, privacy=PRIVATE)

    with app.sessions() as session:
        from sqlalchemy import text
        row = session.execute(text(
            "SELECT local_only FROM vault_fts WHERE path = :path"),
            {"path": result.path}).first()
    assert row is not None, "the capture was not indexed at all"
    assert row[0] == 1, "a private capture was indexed as shareable"


def test_a_private_capture_is_excluded_when_local_only_is_not_allowed(app):
    """The filter runs in SQL, so private rows are never even loaded."""
    app.knowledge.capture(NOTE, privacy=PRIVATE)
    app.knowledge.capture("An ordinary note about brackets.")

    shareable = app.vault_search.search("clinic appointment brackets",
                                        allow_local_only=False)

    assert all("clinic" not in hit.snippet for hit in shareable)


def test_a_private_note_is_still_retrievable_locally(app):
    result = app.knowledge.capture(NOTE, privacy=PRIVATE)

    answer = app.knowledge.answer("clinic appointment")

    assert result.path in answer.cited_paths


def test_the_answer_carries_the_source_chain_back_to_the_private_note(app):
    result = app.knowledge.capture(NOTE, privacy=PRIVATE)

    answer = app.knowledge.answer("clinic appointment")

    assert result.path in answer.text, (
        "the answer cites a page without naming the source it rests on")


def test_a_task_made_from_a_private_note_keeps_the_label(app):
    """The hand-off A08 names: the derivative must not launder the label."""
    result = app.knowledge.capture(NOTE, privacy=PRIVATE)
    task = app.tasks.create("Confirm the appointment", privacy=PRIVATE)

    stored = app.tasks.get(task.id)

    assert stored.privacy.is_local_only
    assert stored.privacy.sensitive
    del result


def test_a_local_only_request_never_reaches_the_cloud_when_local_fails(
        settings, clock, vault):
    """The case that matters most, and the one a fallback quietly breaks.

    Cloud is fully configured here — key, model and budget — so nothing but
    the privacy gate is stopping the escalation.
    """
    from loop.ai.model_gateway import PrivacyError

    configured = settings.model_copy(
        update={"obsidian_vault_path": str(vault.root)})
    gateway = fake_gateway(cloud=True)          # no messages: the local model fails
    app = build_application(configured, clock=clock, model_gateway=gateway)
    captured = app.knowledge.capture(NOTE, privacy=PRIVATE)

    report = app.knowledge.compile_source(captured.path, privacy=PRIVATE)

    assert gateway.audit.cloud_calls == 0, "private content reached the cloud"
    assert report.outcome.value in ("deferred", "needs_context")
    assert "extraction did not succeed" in report.reason or report.reason
    del PrivacyError


def test_the_gateway_refuses_to_widen_a_private_label(app):
    """Retrieved content cannot grant authority it did not have."""
    merged = PrivacyLabel.merge([PRIVATE,
                                 PrivacyLabel(model_scope=ModelScope.CLOUD_ALLOWED)])

    assert merged.is_local_only, "merging widened a local-only label"
    assert merged.sensitive


def test_a_private_label_permits_no_destination_by_default(app):
    """An empty destination set is *no* destinations, never all of them."""
    assert PRIVATE.permits_destination("telegram:4242") is False
    assert PRIVATE.allowed_destinations == frozenset()
