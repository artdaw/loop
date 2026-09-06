"""Capability registry: discovery, validation, enablement (capabilities §2–§4).

Covers EX01–EX05 and EX08, EX10.
"""

from __future__ import annotations

import pytest

from loop.capabilities.registry import (
    Availability,
    CapabilityRegistry,
    hash_package,
    load_manifest,
)
from loop.core.errors import Conflict, InvalidInput, ValidationFailed
from tests.vnext.pack_fixtures import (
    build_bike_service_pack,
    build_invalid_pack,
    build_plant_care_pack,
)


def _rewrite_dependencies(package, *, required: list[str],
                          tools: list[str]) -> None:
    """Replace a pack's dependency and tool lists, keeping it otherwise valid."""
    import re as _re

    text = (package / "capability.yaml").read_text(encoding="utf-8")
    text = _re.sub(r"dependencies:\n  required:\n(?:    - .*\n)+  optional: \[\]",
                   "dependencies:\n  required:\n"
                   + "".join(f"    - {d}\n" for d in required)
                   + "  optional: []", text)
    text = _re.sub(r"    tools:\n(?:      - .*\n)+",
                   "    tools:\n" + "".join(f"      - {t}\n" for t in tools),
                   text)
    (package / "capability.yaml").write_text(text, encoding="utf-8")


@pytest.fixture
def roots(tmp_path):
    root = tmp_path / "capabilities"
    build_plant_care_pack(root)
    build_bike_service_pack(root)
    return [root]


@pytest.fixture
def registry(roots) -> CapabilityRegistry:
    reg = CapabilityRegistry(roots=roots)
    reg.register_all(reg.discover())
    return reg


# --------------------------------------------------------------------------- #
# EX02 — two unrelated packs
# --------------------------------------------------------------------------- #
def test_ex02_both_packs_are_discovered(registry):
    ids = {e.manifest.id for e in registry.entries()}
    assert ids == {"plantcare", "bikeservice"}


def test_ex02_their_operations_register_generically(registry):
    registry.enable("plantcare", "1.0.0")
    registry.enable("bikeservice", "2.1.0")

    assert set(registry.enabled_operations()) == {
        "plantcare.advise", "bikeservice.schedule"}


def test_ex02_the_registry_needs_no_domain_branch(registry):
    """Both resolve through the same generic lookup."""
    registry.enable("plantcare", "1.0.0")
    registry.enable("bikeservice", "2.1.0")

    for name in ("plantcare.advise", "bikeservice.schedule"):
        resolved = registry.resolve_operation(name)
        assert resolved is not None


def test_ex02_a_snapshot_describes_both_without_special_casing(registry):
    registry.enable("plantcare", "1.0.0")
    registry.enable("bikeservice", "2.1.0")
    snapshot = registry.snapshot()

    assert set(snapshot["operations"]) == {"plantcare.advise",
                                           "bikeservice.schedule"}
    assert snapshot["operations"]["bikeservice.schedule"]["mode"] == "workflow"


def test_ex05_an_instruction_only_pack_needs_no_code(roots):
    """EX05: no Python, no DB table, no bespoke scheduler."""
    package = roots[0] / "plantcare" / "1.0.0"
    manifest, problems = load_manifest(package)

    assert problems == []
    assert manifest.operations["plantcare.advise"].mode == "agent"
    assert not list(package.glob("**/*.py"))


# --------------------------------------------------------------------------- #
# EX01 — scaffolding rules
# --------------------------------------------------------------------------- #
def test_ex01_a_valid_pack_carries_every_required_part(roots):
    package = roots[0] / "plantcare" / "1.0.0"

    for relative in ("capability.yaml", "instructions.md",
                     "schemas/input.schema.json", "schemas/output.schema.json",
                     "examples/cases.yaml"):
        assert (package / relative).is_file(), relative


def test_ex01_a_pack_cannot_enable_itself(tmp_path):
    package = build_invalid_pack(tmp_path, problem="self_enable")
    manifest, problems = load_manifest(package)

    assert manifest is None
    assert any("enabled" in p for p in problems)


# --------------------------------------------------------------------------- #
# EX03 — validation before anything runs
# --------------------------------------------------------------------------- #
def test_ex03_a_path_escape_is_rejected(tmp_path):
    package = build_invalid_pack(tmp_path, problem="escape")
    manifest, problems = load_manifest(package)

    assert manifest is None
    assert any("escapes the package" in p for p in problems)


def test_ex03_a_remote_schema_ref_is_rejected(tmp_path):
    """Validation must never fetch a remote schema."""
    package = build_invalid_pack(tmp_path, problem="remote_ref")
    manifest, problems = load_manifest(package)

    assert manifest is None
    assert any("remote $ref" in p for p in problems)


def test_ex03_a_reserved_namespace_is_rejected(tmp_path):
    package = build_invalid_pack(tmp_path, problem="reserved")
    manifest, problems = load_manifest(package)

    assert manifest is None
    assert any("reserved builtin namespace" in p for p in problems)


def test_ex03_two_bindings_for_one_mode_are_rejected(tmp_path):
    package = build_invalid_pack(tmp_path, problem="bad_mode")
    manifest, problems = load_manifest(package)

    assert manifest is None
    assert any("not valid for mode" in p for p in problems)


def test_ex03_an_undeclared_tool_is_rejected(tmp_path):
    package = build_invalid_pack(tmp_path, problem="undeclared_tool")
    manifest, problems = load_manifest(package)

    assert manifest is None
    assert any("not a declared dependency" in p for p in problems)


def test_ex03_a_colliding_operation_name_is_refused(registry, tmp_path):
    """A pack cannot take over another pack's operation name."""
    other_root = tmp_path / "other"
    build_plant_care_pack(other_root, version="9.9.9")
    squatter = CapabilityRegistry(roots=[other_root])
    entries = squatter.discover()

    # Rename the pack id but keep the operation name, simulating a squatter.
    entries[0].manifest.id = "impostor"

    with pytest.raises(Conflict) as excinfo:
        registry.register(entries[0])
    assert "already owned" in str(excinfo.value)


def test_ex03_validation_imports_no_pack_code(roots):
    """Nothing under the pack is executed during validation."""
    import sys

    before = set(sys.modules)
    load_manifest(roots[0] / "plantcare" / "1.0.0")
    new_modules = set(sys.modules) - before

    assert not any("plantcare" in m for m in new_modules)


def test_ex03_an_invalid_pack_is_discovered_but_not_registered(tmp_path):
    root = tmp_path / "caps"
    build_plant_care_pack(root)
    build_invalid_pack(root, problem="escape")

    registry = CapabilityRegistry(roots=[root])
    discovered = registry.discover()
    registry.register_all(discovered)

    assert any(e.availability is Availability.INVALID for e in discovered)
    assert {e.manifest.id for e in registry.entries()} == {"plantcare"}


def test_ex03_an_invalid_pack_cannot_be_enabled(tmp_path):
    root = tmp_path / "caps"
    build_invalid_pack(root, problem="escape")
    registry = CapabilityRegistry(roots=[root])
    for entry in registry.discover():
        registry._entries[entry.manifest.pack_key] = entry

    with pytest.raises(ValidationFailed):
        registry.enable(entry.manifest.id, entry.manifest.version)


# --------------------------------------------------------------------------- #
# EX04 — dependencies
# --------------------------------------------------------------------------- #
def test_ex04_a_pack_with_satisfied_dependencies_becomes_ready(registry):
    registry.enable("plantcare", "1.0.0")
    assert registry.resolve_availability("plantcare@1.0.0") is Availability.READY


def test_ex04_a_missing_dependency_blocks_availability(tmp_path):
    root = tmp_path / "caps"
    package = build_plant_care_pack(root)
    manifest_path = package / "capability.yaml"
    manifest_path.write_text(
        manifest_path.read_text().replace("- vault.search",
                                          "- nonexistent.operation"),
        encoding="utf-8")

    registry = CapabilityRegistry(roots=[root])
    registry.register_all(registry.discover())
    registry.enable("plantcare", "1.0.0")

    assert registry.resolve_availability("plantcare@1.0.0") is \
        Availability.MISSING_DEPENDENCIES
    assert registry.missing_dependencies("plantcare@1.0.0") == \
        ["nonexistent.operation"]


def test_ex04_a_blocked_pack_does_not_resolve_its_operation(tmp_path):
    """No model claim that the capability executed."""
    root = tmp_path / "caps"
    package = build_plant_care_pack(root)
    manifest_path = package / "capability.yaml"
    manifest_path.write_text(
        manifest_path.read_text().replace("- vault.search", "- ghost.op"),
        encoding="utf-8")

    registry = CapabilityRegistry(roots=[root])
    registry.register_all(registry.discover())
    registry.enable("plantcare", "1.0.0")
    registry.resolve_availability("plantcare@1.0.0")

    assert registry.resolve_operation("plantcare.advise") is None


def test_ex04_a_disabled_pack_is_not_ready(registry):
    assert registry.resolve_availability("plantcare@1.0.0") is \
        Availability.DISABLED


def test_ex04_cross_pack_cycles_are_detected(tmp_path):
    """Written explicitly rather than by patching the fixtures.

    A string replacement here silently matched inside the `tools:` list too,
    invalidating both packs so that no cycle could be found — the test passed
    for the wrong reason. Explicit manifests keep the defect isolated.
    """
    root = tmp_path / "caps"
    a = build_plant_care_pack(root)
    b = build_bike_service_pack(root)

    _rewrite_dependencies(a, required=["bikeservice.schedule"],
                          tools=["bikeservice.schedule"])
    _rewrite_dependencies(b, required=["plantcare.advise"],
                          tools=["plantcare.advise"])

    registry = CapabilityRegistry(roots=[root])
    entries = registry.discover()
    assert all(e.availability is not Availability.INVALID for e in entries), \
        "both packs must be valid, or the cycle check proves nothing"
    registry.register_all(entries)

    assert registry.dependency_cycles()


def test_ex04_no_cycle_in_the_normal_fixtures(registry):
    assert registry.dependency_cycles() == []


# --------------------------------------------------------------------------- #
# EX08 — a manifest declares, it does not grant
# --------------------------------------------------------------------------- #
def test_ex08_declared_effects_are_requirements_not_grants(registry):
    registry.enable("bikeservice", "2.1.0")
    _, operation = registry.resolve_operation("bikeservice.schedule")

    assert "owner_notification_proposal" in operation.effects
    # The manifest states a requirement; nothing here is a permission.
    assert operation.destinations == ["owner:telegram"]


def test_ex08_a_remote_effect_is_marked_as_needing_approval(tmp_path):
    root = tmp_path / "caps"
    package = build_bike_service_pack(root)
    (package / "capability.yaml").write_text(
        (package / "capability.yaml").read_text().replace(
            "      - local_domain_write", "      - remote_write"),
        encoding="utf-8")

    registry = CapabilityRegistry(roots=[root])
    registry.register_all(registry.discover())
    registry.enable("bikeservice", "2.1.0")
    _, operation = registry.resolve_operation("bikeservice.schedule")

    assert operation.needs_approval is True


def test_ex08_default_arguments_cannot_carry_authority(tmp_path):
    root = tmp_path / "caps"
    package = build_plant_care_pack(root)
    (package / "capability.yaml").write_text(
        (package / "capability.yaml").read_text()
        + "    default_arguments:\n      authority: owner\n", encoding="utf-8")

    manifest, problems = load_manifest(package)
    assert manifest is None
    assert any("default_arguments" in p for p in problems)


# --------------------------------------------------------------------------- #
# EX10 — immutable versions
# --------------------------------------------------------------------------- #
def test_ex10_the_package_hash_covers_every_file(roots):
    package = roots[0] / "plantcare" / "1.0.0"
    before = hash_package(package)
    (package / "instructions.md").write_text("# Changed\n", encoding="utf-8")

    assert hash_package(package) != before


def test_ex10_same_version_with_changed_bytes_is_rejected(registry, roots):
    package = roots[0] / "plantcare" / "1.0.0"
    (package / "instructions.md").write_text("# Edited in place\n",
                                             encoding="utf-8")

    changed = CapabilityRegistry(roots=roots)
    entry = next(e for e in changed.discover() if e.manifest.id == "plantcare")

    with pytest.raises(Conflict) as excinfo:
        registry.register(entry)
    assert "bump the version" in str(excinfo.value)


def test_ex10_re_registering_identical_bytes_is_a_no_op(registry, roots):
    again = CapabilityRegistry(roots=roots)
    entry = next(e for e in again.discover() if e.manifest.id == "plantcare")

    before = registry.revision
    registry.register(entry)
    assert registry.revision == before


def test_ex10_enabling_a_version_disables_the_others(tmp_path):
    root = tmp_path / "caps"
    build_plant_care_pack(root, version="1.0.0")
    build_plant_care_pack(root, version="1.1.0")
    registry = CapabilityRegistry(roots=[root])
    registry.register_all(registry.discover())

    registry.enable("plantcare", "1.0.0")
    registry.enable("plantcare", "1.1.0")

    assert registry.get("plantcare@1.0.0").enabled is False
    assert registry.get("plantcare@1.1.0").enabled is True


def test_ex10_the_revision_advances_on_enablement(registry):
    before = registry.revision
    registry.enable("plantcare", "1.0.0")
    assert registry.revision > before


# --------------------------------------------------------------------------- #
# EX09 — disable
# --------------------------------------------------------------------------- #
def test_ex09_disabling_stops_new_invocations(registry):
    registry.enable("plantcare", "1.0.0")
    registry.disable("plantcare")

    assert registry.resolve_operation("plantcare.advise") is None


def test_ex09_disable_reports_what_it_affected(registry):
    registry.enable("plantcare", "1.0.0")
    assert registry.disable("plantcare") == ["plantcare@1.0.0"]


def test_ex09_disabling_an_already_disabled_pack_is_a_no_op(registry):
    assert registry.disable("plantcare") == []


# --------------------------------------------------------------------------- #
# Discovery bounds
# --------------------------------------------------------------------------- #
def test_discovery_needs_no_network_model_or_credential(roots):
    registry = CapabilityRegistry(roots=roots)
    assert len(registry.discover()) == 2


def test_discovery_does_not_crawl_arbitrary_depth(tmp_path):
    root = tmp_path / "caps"
    deep = root / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "capability.yaml").write_text("schema_version: 1\n", encoding="utf-8")

    assert CapabilityRegistry(roots=[root]).discover() == []


def test_a_missing_root_is_ignored(tmp_path):
    assert CapabilityRegistry(roots=[tmp_path / "absent"]).discover() == []


def test_resolving_an_unknown_pack_is_an_error(registry):
    with pytest.raises(InvalidInput):
        registry.resolve_availability("ghost@1.0.0")
