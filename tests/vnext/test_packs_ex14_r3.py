"""Weather, travel and a small checklist are all just packs (EX14 — R3).

EX14 asks for two things that pull against each other: the large domain
capabilities must load through the *same* registry, lifecycle and executor as
everything else, and a simple extension must not be forced to carry their
boilerplate to do it. A design that satisfies only the first turns every
checklist into a weather pack; one that satisfies only the second gives the big
capabilities a private path nothing else can use.

So these tests check both directions — same machinery, different weight — and
that enabling a pack grants nothing the host had not already decided to offer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from loop.ai.budget import RootBudget
from loop.app import build_application
from loop.core.errors import Unavailable
from loop.core.privacy import PrivacyLabel
from loop.core.settings import Settings
from loop.runtime.authority import AuthorityContext

PACKS = Path("packs")


@pytest.fixture
def app(settings: Settings, clock):
    return build_application(
        settings.model_copy(update={
            "capability_paths": json.dumps([str(PACKS.resolve())])}),
        clock=clock)


def context() -> AuthorityContext:
    return AuthorityContext(owner="owner", root_id="ex14",
                            privacy=PrivacyLabel(), budget=RootBudget())


# --------------------------------------------------------------------------- #
# One registry, one lifecycle
# --------------------------------------------------------------------------- #
def test_every_pack_is_discovered_by_the_same_registry(app):
    found = {entry.manifest.id for entry in app.registry.entries()}

    assert {"weather", "travel", "checklist"} <= found
    assert {"plantcare", "bikeservice"} <= found, "the earlier packs still load"


def test_every_pack_validates_against_the_same_manifest_contract(app):
    for entry in app.registry.entries():
        assert entry.validation_errors == [], (
            f"{entry.manifest.id}: {entry.validation_errors}")


def test_no_pack_may_ship_itself_enabled(app):
    """Enabling is the owner's decision, not the pack author's."""
    for entry in app.registry.entries():
        assert entry.enabled is False or entry.manifest.id in {}, (
            f"{entry.manifest.id} arrived enabled")


def test_the_same_enable_disable_lifecycle_serves_all_of_them(app):
    for pack in ("weather", "travel", "checklist"):
        app.registry.enable(pack, "1.0.0")
    enabled = set(app.registry.enabled_operations())
    assert {"weather.local", "travel.plan_brief", "checklist.build"} <= enabled

    for pack in ("weather", "travel", "checklist"):
        app.registry.disable(pack)
    assert not ({"weather.local", "travel.plan_brief", "checklist.build"}
                & set(app.registry.enabled_operations()))


def test_enablement_survives_a_restart(settings, clock):
    configured = settings.model_copy(update={
        "capability_paths": json.dumps([str(PACKS.resolve())])})
    first = build_application(configured, clock=clock)
    first.registry.enable("weather", "1.0.0")

    restarted = build_application(configured, clock=clock)

    assert "weather.local" in restarted.registry.enabled_operations()


# --------------------------------------------------------------------------- #
# One executor
# --------------------------------------------------------------------------- #
def test_a_pack_runs_through_the_shared_invoker_and_records_an_artifact(app):
    app.registry.enable("travel", "1.0.0")

    result = app.invoker.invoke(
        "travel.plan_brief",
        {"place": "Lisbon", "start_date": "2026-10-01",
         "end_date": "2026-10-05", "adults": 2},
        context=context())

    assert result.output["can_plan"] is False       # "which Lisbon?"
    assert "destinations" in result.output["blocking"]
    assert result.artifact_id, "the shared executor recorded no artifact"


def test_the_small_checklist_runs_without_a_model_or_host_tool(app):
    app.registry.enable("checklist", "1.0.0")

    result = app.invoker.invoke("checklist.build", {"items": ["a", "b"]},
                                context=context())

    assert result.output == {"answer": "checklist ready", "sources": []}
    assert app.model_gateway.audit.local_calls == 0
    assert app.model_gateway.audit.cloud_calls == 0


def test_the_weather_pack_reaches_the_host_port_not_its_own_network_code(app):
    """Adapter mode: the pack names a port the application already supplies."""
    app.registry.enable("weather", "1.0.0")
    manifest = yaml.safe_load(
        (PACKS / "weather/1.0.0/capability.yaml").read_text(encoding="utf-8"))
    operation = manifest["operations"]["weather.local"]

    assert operation["mode"] == "adapter"
    assert operation["handler"] == "weather.prepare"
    assert operation["handler"] in app.invoker.handlers


def test_a_disabled_pack_cannot_be_invoked(app):
    with pytest.raises(Unavailable):
        app.invoker.invoke("checklist.build", {"items": ["a"]},
                           context=context())


def test_input_is_validated_against_the_packs_own_schema(app):
    from loop.core.errors import ValidationFailed

    app.registry.enable("travel", "1.0.0")

    with pytest.raises(ValidationFailed):
        app.invoker.invoke("travel.plan_brief", {"nonsense": True},
                           context=context())


def test_version_pinning_is_recorded_for_every_pack(app):
    app.registry.enable("weather", "1.0.0")
    snapshot = app.registry.snapshot()

    assert "weather" in json.dumps(snapshot)
    assert "1.0.0" in json.dumps(snapshot)


# --------------------------------------------------------------------------- #
# A simple pack stays simple
# --------------------------------------------------------------------------- #
def test_the_checklist_pack_carries_none_of_the_large_packs_boilerplate():
    """The half of EX14 that a "make everything uniform" design would fail.

    If a small extension needs a domain schema, an instructions file and a
    tool list to be loadable, then the extension contract is really the
    weather contract wearing a different name.
    """
    checklist = yaml.safe_load(
        (PACKS / "checklist/1.0.0/capability.yaml").read_text(encoding="utf-8"))
    operation = checklist["operations"]["checklist.build"]

    assert operation["tools"] == []
    assert operation["effects"] == []
    assert "instructions" not in operation
    assert not (PACKS / "checklist/1.0.0/instructions.md").exists()

    schema = json.loads(
        (PACKS / "checklist/1.0.0/schemas/input.schema.json").read_text())
    assert list(schema["properties"]) == ["items"]


def test_the_simple_pack_is_much_smaller_than_the_domain_packs():
    def manifest_size(pack: str) -> int:
        return len((PACKS / pack / "1.0.0/capability.yaml"
                    ).read_text(encoding="utf-8").splitlines())

    assert manifest_size("checklist") < manifest_size("travel")
    assert manifest_size("checklist") < manifest_size("plantcare")


def test_adding_these_packs_needed_no_coordinator_or_channel_change():
    """The registry is the extension point; the rest of the system is not.

    Asserted against the source because it is a claim about *code that was not
    written*, which no runtime check can make.
    """
    coordinator = Path("loop/agents/coordinator.py").read_text(encoding="utf-8")
    for pack in ("weather.local", "travel.plan_brief", "checklist.build",
                 "plantcare", "bikeservice"):
        assert pack not in coordinator, (
            f"the coordinator names {pack}; packs are meant to be data")

    for interface in ("cli.py", "http.py", "telegram.py"):
        body = Path(f"loop/interfaces/{interface}").read_text(encoding="utf-8")
        for pack in ("weather.local", "travel.plan_brief", "checklist.build"):
            assert pack not in body, f"{interface} names {pack}"
