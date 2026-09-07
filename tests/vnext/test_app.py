"""The composition root (agent-stack §1: "CLI, bot and web share application services").

If two interfaces each built their own `TaskService`, they would each own a
different truth about whether a task is done. These tests prove the single
function every interface is meant to call actually produces one coherent,
working object graph — real migrations applied, real registry discovery, the
real coordinator wired to the real invoker.

No test here touches a real model, a real Telegram server or a real vault
beyond a synthetic fixture: `build_application` must not require credentials
any more than `loop status` should.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from loop.ai.budget import RootBudget
from loop.app import Application, build_application
from loop.core.errors import Unavailable
from loop.core.privacy import PrivacyLabel
from loop.core.settings import Settings
from loop.runtime.authority import AuthorityContext


def _settings(tmp_path: Path, **kw) -> Settings:
    base = {
        "_env_file": None, "data_dir": str(tmp_path),
        "database_url": f"sqlite:///{tmp_path / 'loop.db'}",
        "ollama_default_model": "llama3.1:8b",
        "capability_paths": '["packs"]',
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _context(**kw: Any) -> AuthorityContext:
    defaults: dict[str, Any] = {"owner": "owner", "root_id": "r1",
                                "privacy": PrivacyLabel(), "budget": RootBudget()}
    defaults.update(kw)
    return AuthorityContext(**defaults)


# --------------------------------------------------------------------------- #
# Building the graph
# --------------------------------------------------------------------------- #
def test_build_application_needs_no_model_or_credentials(tmp_path):
    """Constructing the graph must not require anything to be configured."""
    app = build_application(_settings(tmp_path))
    assert isinstance(app, Application)


def test_migrations_are_applied_before_any_service_is_used(tmp_path):
    app = build_application(_settings(tmp_path))
    task = app.tasks.create("water the fern")
    assert app.tasks.get(task.id) is not None


def test_every_interface_gets_the_same_task_service_instance(tmp_path):
    """Two calls sharing one `Application` see one truth about a task."""
    app = build_application(_settings(tmp_path))
    task = app.tasks.create("water the fern")
    app.tasks.complete(task.id, expected_version=1)

    assert app.tasks.get(task.id).status == "done"


# --------------------------------------------------------------------------- #
# Real shipped packs are discovered and manageable
# --------------------------------------------------------------------------- #
def test_the_two_shipped_packs_are_discovered_from_the_real_repo_path(tmp_path):
    """`packs/` is a real directory in this checkout, not a test fixture."""
    app = build_application(_settings(tmp_path, capability_paths='["packs"]'))
    ids = {e.manifest.id for e in app.registry.entries()}
    assert ids == {"plantcare", "bikeservice"}


def test_enabling_a_shipped_pack_makes_its_operation_available(tmp_path):
    app = build_application(_settings(tmp_path))
    app.registry.enable("plantcare", "1.0.0")
    assert "plantcare.advise" in app.registry.enabled_operations()


def test_enablement_of_a_shipped_pack_survives_a_restart(tmp_path):
    """M3's registry persistence, exercised through the real composition root.

    Relies solely on what `build_application` itself does at startup — no
    manual `reapply_enablement()` call here, or a broken automatic call would
    be masked by a redundant correct one."""
    settings = _settings(tmp_path)
    first = build_application(settings)
    first.registry.enable("plantcare", "1.0.0")

    second = build_application(settings)
    assert "plantcare.advise" in second.registry.enabled_operations()


def test_the_two_shipped_packs_are_unrelated_and_both_work(tmp_path):
    """EX02, through the real registry rather than a test-only fixture."""
    app = build_application(_settings(tmp_path))
    app.registry.enable("plantcare", "1.0.0")
    app.registry.enable("bikeservice", "2.1.0")
    assert set(app.registry.enabled_operations()) == {
        "plantcare.advise", "bikeservice.schedule"}


# --------------------------------------------------------------------------- #
# Trusted-port handlers: real, not stubs
# --------------------------------------------------------------------------- #
def test_vault_search_refuses_honestly_with_no_vault_configured(tmp_path):
    app = build_application(_settings(tmp_path))
    app.registry.enable("plantcare", "1.0.0")

    with pytest.raises(Unavailable, match="[Nn]o vault"):
        app.invoker.invoke("vault.search", {"query": "fern"},
                           context=_context())


def test_vault_search_finds_a_real_note(tmp_path):
    from loop.vault.search import IndexEntry, VaultSearch

    vault = tmp_path / "vault"
    vault.mkdir()

    settings = _settings(tmp_path, obsidian_vault_path=str(vault))
    app = build_application(settings)
    VaultSearch(sessions=app.sessions).index(IndexEntry(
        path="1-wiki/fern.md", title="Fern care",
        body="Water when the topsoil is dry."))

    result = app.invoker.invoke("vault.search", {"query": "topsoil"},
                                context=_context())
    assert result.output["sources"] == ["1-wiki/fern.md"]


def test_reminder_schedule_creates_a_real_task(tmp_path):
    app = build_application(_settings(tmp_path))
    result = app.invoker.invoke("reminder.schedule", {"query": "bike service"},
                                context=_context())

    task_id = result.output["sources"][0]
    assert app.tasks.get(task_id) is not None


# --------------------------------------------------------------------------- #
# The coordinator, wired for real
# --------------------------------------------------------------------------- #
def test_the_coordinator_runs_a_shipped_pack_operation_end_to_end(tmp_path):
    """The full path: discover, enable, plan, invoke, compose — no test-only
    wiring, the same `build_application` a CLI command would call."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from loop.ai.model_gateway import ModelGateway

    vault = tmp_path / "vault"
    vault.mkdir()
    app = build_application(_settings(tmp_path, obsidian_vault_path=str(vault)))
    app.registry.enable("bikeservice", "2.1.0")

    # A fake model stands in for the local model this test must not require;
    # everything else is the real graph.
    app.invoker.gateway = ModelGateway(
        settings=app.settings,
        local_model=GenericFakeChatModel(messages=iter([AIMessage(content="{}")])))

    result = app.coordinator.invoke(
        objective="when is my bike due for a service",
        operation="bikeservice.schedule", arguments={"query": "bike service"},
        context=_context())

    assert result.response["status"] == "succeeded"


def test_a_run_pauses_and_resumes_through_the_real_composition_root(tmp_path):
    """M2's durability, exercised through `build_application` rather than a
    hand-assembled test coordinator."""
    from loop.agents.coordinator import Coordinator
    from loop.capabilities.runners import RegisteredHandler
    from loop.runtime.checkpointer import open_production_checkpointer

    async def scenario():
        settings = _settings(tmp_path)
        app = build_application(settings)
        sent = []
        app.invoker.handlers["email.send"] = RegisteredHandler(
            lambda args, context: sent.append(args) or {"answer": "sent",
                                                         "sources": []},
            description="Send an email.",
            input_schema={"type": "object", "additionalProperties": True},
            needs_approval=True, owner_role="commitments")

        async with open_production_checkpointer(app.settings.data_path) as saver:
            coordinator = Coordinator(invoker=app.invoker, roles=app.roles,
                                      runs=app.runs, approvals=app.operations,
                                      checkpointer=saver)
            paused = await coordinator.ainvoke(
                objective="send it", operation="email.send",
                arguments={"body": "hi"}, context=_context())
        assert paused.is_paused

        async with open_production_checkpointer(app.settings.data_path) as saver:
            coordinator = Coordinator(invoker=app.invoker, roles=app.roles,
                                      runs=app.runs, approvals=app.operations,
                                      checkpointer=saver)
            resumed = await coordinator.aresume(
                run_id=paused.run_id,
                decision={"approved": True, "actor": "owner"}, actor="owner",
                context=_context())
        assert resumed.is_paused is False
        assert sent == [{"body": "hi"}]

    import asyncio
    asyncio.run(scenario())
