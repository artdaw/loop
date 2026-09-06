"""WP5: real LangChain/LangGraph capability execution (LG01, LG02)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import select

from loop.agents.coordinator import Coordinator
from loop.ai.budget import BudgetLimits, RootBudget
from loop.ai.model_gateway import ModelGateway
from loop.capabilities.registry import CapabilityRegistry
from loop.capabilities.runners import (
    ArtifactStore,
    CapabilityInvoker,
    RegisteredHandler,
)
from loop.core.ids import content_hash
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.core.settings import Settings
from loop.db.models import Artifact
from loop.runtime.authority import AuthorityContext
from tests.vnext.pack_fixtures import build_bike_service_pack, build_plant_care_pack


class ToolCallingFakeChatModel(GenericFakeChatModel):
    """LangChain's fake model with deterministic tool binding for agent tests."""

    def bind_tools(self, tools: Sequence[object], *,
                   tool_choice: str | None = None, **kwargs: object):
        del tools, tool_choice, kwargs
        return self


def _tool_name(operation: str) -> str:
    return f"{operation.replace('.', '_')}_{content_hash(operation)[:10]}"


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, environment="test", timezone="Europe/Berlin",
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        data_dir=str(tmp_path / "data"), obsidian_vault_path="",
        ollama_default_model="fake-local")


def _context() -> AuthorityContext:
    return AuthorityContext(
        owner="owner", root_id="root-lg01",
        privacy=PrivacyLabel(
            model_scope=ModelScope.LOCAL_ONLY,
            origins=frozenset({"synthetic-test"}),
            allowed_destinations=frozenset({"owner:telegram"})),
        budget=RootBudget(limits=BudgetLimits(model_calls=4, tool_calls=8)))


def _registry(root) -> CapabilityRegistry:
    build_plant_care_pack(root)
    build_bike_service_pack(root)
    registry = CapabilityRegistry(roots=[root])
    registry.register_all(registry.discover())
    registry.enable("plantcare", "1.0.0")
    registry.enable("bikeservice", "2.1.0")
    return registry


def _handlers() -> dict[str, RegisteredHandler]:
    query_schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"query": {"type": "string"}}, "required": ["query"],
    }
    return {
        "vault.search": RegisteredHandler(
            lambda args, context: {
                "matches": ["Water when the top soil is dry."],
                "sources": ["note://plants/fern"],
            },
            description="Search the owner's authorized notes.",
            input_schema=query_schema),
        "reminder.schedule": RegisteredHandler(
            lambda args, context: {
                "answer": "Bike service reminder proposed.",
                "sources": ["note://bikes/service-log"],
            },
            description="Propose a local reminder.",
            input_schema={"type": "object", "additionalProperties": True}),
    }


def test_lg01_agent_pack_uses_create_agent_and_persists_validated_evidence(
        tmp_path, sessions, clock):
    registry = _registry(tmp_path / "caps")
    messages = iter([
        AIMessage(content="", tool_calls=[{
            "name": _tool_name("vault.search"),
            "args": {"query": "fern watering"},
            "id": "search-1", "type": "tool_call",
        }]),
        AIMessage(content=json.dumps({
            "answer": "Water after the top soil dries.",
            "sources": ["note://plants/fern"],
        })),
    ])
    model = ToolCallingFakeChatModel(messages=messages)
    gateway = ModelGateway(settings=_settings(tmp_path), local_model=model,
                           clock=clock)
    invoker = CapabilityInvoker(
        registry=registry, gateway=gateway,
        artifact_store=ArtifactStore(sessions=sessions, clock=clock),
        handlers=_handlers())

    result = Coordinator(invoker=invoker).invoke(
        objective="How often should I water my fern?",
        operation="plantcare.advise",
        arguments={"query": "fern watering"}, context=_context())

    execution = result.results["request"]
    assert execution.output["answer"] == "Water after the top soil dries."
    assert execution.evidence == ["note://plants/fern"]
    assert execution.artifact_id is not None
    assert gateway.audit.local_calls == 2
    with sessions() as session:
        artifact = session.scalar(select(Artifact).where(
            Artifact.id == execution.artifact_id))
    assert artifact is not None
    assert json.loads(artifact.payload_json or "{}") == execution.output
    assert json.loads(artifact.evidence_json) == ["note://plants/fern"]


def test_lg02_unrelated_agent_and_workflow_packs_use_the_same_core(
        tmp_path, sessions, clock):
    protected = [
        "loop/agents/coordinator.py", "loop/db/models.py",
        "loop/runtime/intake.py", "loop/runtime/service.py",
    ]
    before = {path: content_hash(Path(path).read_bytes()) for path in protected}
    registry = _registry(tmp_path / "caps")
    model = ToolCallingFakeChatModel(messages=iter([
        AIMessage(content=json.dumps({
            "answer": "Use the note's watering guidance.",
            "sources": ["note://plants/fern"],
        })),
    ]))
    gateway = ModelGateway(settings=_settings(tmp_path), local_model=model,
                           clock=clock)
    coordinator = Coordinator(invoker=CapabilityInvoker(
        registry=registry, gateway=gateway,
        artifact_store=ArtifactStore(sessions=sessions, clock=clock),
        handlers=_handlers()))

    plant = coordinator.invoke(
        objective="Advise on the fern", operation="plantcare.advise",
        arguments={"query": "fern"}, context=_context())
    bike = coordinator.invoke(
        objective="Schedule bike service", operation="bikeservice.schedule",
        arguments={"query": "bike"}, context=_context())

    after = {path: content_hash(Path(path).read_bytes()) for path in protected}
    assert plant.response["summary"]["answer"]
    assert bike.response["summary"]["answer"] == "Bike service reminder proposed."
    assert plant.stage_trace == list(Coordinator.STAGES)
    assert bike.stage_trace == list(Coordinator.STAGES)
    assert before == after, "adding/running packs must not edit coordinator/channel/schema"
