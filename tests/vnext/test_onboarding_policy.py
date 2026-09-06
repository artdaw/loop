"""Onboarding and policy loading (vault §1, §3, §4).

Covers V01, V26, V27, V28.
"""

from __future__ import annotations

from datetime import date

import pytest

from loop.vault.capture import CaptureRequest, CaptureService
from loop.vault.gateway import VaultGateway
from loop.vault.onboarding import FORBIDDEN_ROOTS, VaultOnboarding
from loop.vault.policy import PolicyLoader, RoutineRegistry
from tests.vnext.vault_fixtures import build_minimal_vault

TODAY = date(2026, 9, 6)


@pytest.fixture
def vault(tmp_path):
    return build_minimal_vault(tmp_path / "GlebOS")


@pytest.fixture
def gateway(vault, sessions, clock) -> VaultGateway:
    return VaultGateway(root=vault.root, sessions=sessions, clock=clock)


@pytest.fixture
def onboarding(gateway) -> VaultOnboarding:
    return VaultOnboarding(gateway=gateway)


@pytest.fixture
def policy(gateway) -> PolicyLoader:
    return PolicyLoader(gateway=gateway)


def _snapshot(root) -> dict[str, float]:
    """Every file and its mtime, for proving nothing was written."""
    return {str(p.relative_to(root)): p.stat().st_mtime
            for p in root.rglob("*") if p.is_file()}


# --------------------------------------------------------------------------- #
# V01 — dry-run onboarding
# --------------------------------------------------------------------------- #
def test_v01_the_layout_is_detected_as_v2(onboarding):
    assert onboarding.inspect().layout_version == "glebos-v2"


def test_v01_the_map_covers_the_expected_paths(onboarding):
    report = onboarding.inspect()

    for key in ("raw", "inbox", "ledger", "concepts", "entities", "topics",
                "projects", "profile", "compile_rules"):
        assert key in report.present, f"{key} should be mapped"


def test_v01_counts_are_reported(onboarding):
    counts = onboarding.inspect().counts

    assert counts["concepts"] == 1
    assert counts["entities"] == 1
    assert counts["topics"] == 1
    assert counts["projects"] == 1


def test_v01_a_dry_run_writes_nothing(onboarding, vault):
    before = _snapshot(vault.root)

    report = onboarding.apply(dry_run=True)

    assert report.dry_run is True
    assert report.written_files == []
    assert _snapshot(vault.root) == before


def test_v01_inspection_alone_writes_nothing(onboarding, vault):
    before = _snapshot(vault.root)
    onboarding.inspect()
    assert _snapshot(vault.root) == before


def test_v01_no_para_roots_are_created(onboarding, vault):
    onboarding.apply(dry_run=True)

    for name in FORBIDDEN_ROOTS:
        assert not (vault.root / name).exists(), f"{name}/ must not be created"


def test_v01_the_fixture_has_no_forbidden_roots(onboarding):
    assert onboarding.forbidden_roots_present() == []


def test_v01_proposed_files_are_listed_but_absent(onboarding, gateway):
    report = onboarding.inspect()

    assert "_ctx/loop/manifest.yaml" in report.proposed_files
    assert not gateway.exists("_ctx/loop/manifest.yaml")


def test_an_explicit_apply_creates_the_proposed_files(onboarding, gateway):
    report = onboarding.apply(dry_run=False)

    assert "_ctx/loop/manifest.yaml" in report.written_files
    assert gateway.exists("_ctx/loop/manifest.yaml")


def test_an_apply_never_overwrites_an_existing_document(onboarding, gateway,
                                                        vault):
    target = vault.root / "_ctx/loop/behavior.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Mine\n\nHand written.\n", encoding="utf-8")

    onboarding.apply(dry_run=False)

    assert "Hand written." in gateway.read_text("_ctx/loop/behavior.md")


def test_the_manifest_maps_paths_without_assuming_a_username(onboarding, gateway):
    import yaml

    onboarding.apply(dry_run=False)
    manifest = yaml.safe_load(gateway.read_text("_ctx/loop/manifest.yaml"))

    assert manifest["layout"] == "glebos-v2"
    assert manifest["paths"]["ledger"] == "0-raw/_ledger.md"
    assert not any(str(v).startswith("/") for v in manifest["paths"].values())


def test_the_summary_states_the_mode(onboarding):
    assert "dry run" in onboarding.apply(dry_run=True).summary()


# --------------------------------------------------------------------------- #
# V26 — legacy templates conflict with the v2 schema
# --------------------------------------------------------------------------- #
def test_v26_the_legacy_template_is_reported_as_a_conflict(onboarding):
    conflicts = onboarding.find_conflicts()

    kinds = {c.kind for c in conflicts}
    assert "legacy_template" in kinds


def test_v26_the_conflict_names_the_template_path(onboarding):
    conflict = next(c for c in onboarding.find_conflicts()
                    if c.kind == "legacy_template")
    assert "_ctx/templates/Zettel.md" in conflict.paths


def test_v26_the_governing_rule_outranks_a_template(policy):
    """Templates are the lowest tier and cannot override the rules."""
    assert policy.precedence_of("_ctx/rules/compile.md") < \
        policy.precedence_of("_ctx/templates/Zettel.md")


def test_v26_ordering_puts_rules_before_templates(policy):
    ordered = policy.ordered_sources(
        ["_ctx/templates/Zettel.md", "_ctx/rules/compile.md", "CLAUDE.md"])
    assert ordered[0] == "_ctx/rules/compile.md"


def test_v26_loop_settings_outrank_governing_rules(policy):
    assert policy.precedence_of("_ctx/loop/permissions.md") < \
        policy.precedence_of("_ctx/rules/compile.md")


def test_v26_the_template_is_not_modified_by_inspection(onboarding, vault):
    before = (vault.root / "_ctx/templates/Zettel.md").read_text(encoding="utf-8")
    onboarding.find_conflicts()
    after = (vault.root / "_ctx/templates/Zettel.md").read_text(encoding="utf-8")
    assert before == after


# --------------------------------------------------------------------------- #
# V27 — policy conflict or malformed edit
# --------------------------------------------------------------------------- #
def test_v27_a_valid_scope_compiles_to_an_active_revision(policy):
    revision = policy.compile_scope("compile", ["_ctx/rules/compile.md"])

    assert revision.is_active
    assert revision.revision_id


def test_v27_a_malformed_document_yields_an_invalid_revision(policy, vault):
    broken = vault.root / "_ctx/loop/behavior.md"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("---\nthis: [is: not: valid yaml\n---\n\nbody\n",
                      encoding="utf-8")

    revision = policy.compile_scope("behavior", ["_ctx/loop/behavior.md"])

    assert revision.status == "invalid"
    assert revision.problems


def test_v27_the_last_valid_revision_is_retained_after_a_bad_edit(policy, vault):
    good = vault.root / "_ctx/loop/behavior.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("---\nquiet_hours: '22:00-07:00'\n---\n\nok\n",
                    encoding="utf-8")
    first = policy.compile_scope("behavior", ["_ctx/loop/behavior.md"])
    assert first.is_active

    good.write_text("---\nbroken: [unclosed\n---\n", encoding="utf-8")
    second = policy.compile_scope("behavior", ["_ctx/loop/behavior.md"])

    assert second.revision_id == first.revision_id
    assert second.values["quiet_hours"] == "22:00-07:00"


def test_v27_a_first_installation_leaves_the_scope_inactive(policy, vault):
    """With no previous good revision there is nothing to fall back to."""
    broken = vault.root / "_ctx/loop/notifications.md"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("---\n[unclosed\n---\n", encoding="utf-8")

    revision = policy.compile_scope("notifications",
                                    ["_ctx/loop/notifications.md"])

    assert revision.status == "invalid"
    assert revision.is_active is False


def test_v27_capture_still_works_while_a_policy_scope_is_invalid(policy, gateway,
                                                                 vault):
    """A broken policy must not take unrelated functionality down."""
    broken = vault.root / "_ctx/loop/notifications.md"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("---\n[unclosed\n---\n", encoding="utf-8")
    policy.compile_scope("notifications", ["_ctx/loop/notifications.md"])

    capture = CaptureService(gateway=gateway)
    result = capture.capture(CaptureRequest(body="still works\n"), today=TODAY)

    assert result.status == "saved"


def test_v27_equal_authority_disagreement_is_a_conflict(policy):
    conflicts = policy.detect_conflicts("staleness", {
        "stale_days": [("_ctx/rules/compile.md", 14),
                       ("_ctx/rules/naming.md", 30)],
    })

    assert len(conflicts) == 1
    assert set(conflicts[0].paths) == {"_ctx/rules/compile.md",
                                       "_ctx/rules/naming.md"}


def test_v27_a_lower_tier_disagreement_is_not_a_conflict(policy):
    """A template disagreeing with a rule is simply overridden."""
    conflicts = policy.detect_conflicts("staleness", {
        "stale_days": [("_ctx/rules/compile.md", 30),
                       ("_ctx/templates/Zettel.md", 14)],
    })

    assert conflicts == []


def test_v27_agreement_is_not_a_conflict(policy):
    conflicts = policy.detect_conflicts("staleness", {
        "stale_days": [("_ctx/rules/compile.md", 30),
                       ("_ctx/rules/naming.md", 30)],
    })
    assert conflicts == []


def test_v27_a_conflict_carries_both_excerpts(policy):
    conflict = policy.detect_conflicts("staleness", {
        "stale_days": [("_ctx/rules/compile.md", 14),
                       ("_ctx/rules/naming.md", 30)],
    })[0]

    assert set(conflict.excerpts.values()) == {"14", "30"}


def test_v27_a_user_resolution_is_recorded_as_scoped(policy):
    decision = policy.resolve("staleness", "stale_days", 30)

    assert decision["kind"] == "scoped_decision"
    assert decision["scope"] == "staleness"


def test_the_revision_id_changes_when_a_source_changes(policy, vault):
    first = policy.compile_scope("compile", ["_ctx/rules/compile.md"])
    (vault.root / "_ctx/rules/compile.md").write_text(
        "# Compile rules\n\n1. Changed.\n", encoding="utf-8")
    second = policy.compile_scope("compile", ["_ctx/rules/compile.md"])

    assert second.revision_id != first.revision_id


def test_precedence_is_not_decided_by_modification_time(policy, vault):
    """Recency is not authority (vault §3)."""
    import os
    import time

    template = vault.root / "_ctx/templates/Zettel.md"
    os.utime(template, (time.time() + 10_000, time.time() + 10_000))

    ordered = policy.ordered_sources(["_ctx/templates/Zettel.md",
                                      "_ctx/rules/compile.md"])
    assert ordered[0] == "_ctx/rules/compile.md"


# --------------------------------------------------------------------------- #
# V28 — a prepared routine is not an activated one
# --------------------------------------------------------------------------- #
def test_v28_the_prepared_routine_is_reported_as_unregistered(onboarding):
    conflicts = onboarding.find_conflicts()
    assert any(c.kind == "unregistered_routine" for c in conflicts)


def test_v28_the_conflict_explains_that_presence_is_not_permission(onboarding):
    conflict = next(c for c in onboarding.find_conflicts()
                    if c.kind == "unregistered_routine")
    assert "not permission" in conflict.detail.lower()


def test_v28_a_document_alone_does_not_activate_a_routine():
    registry = RoutineRegistry()
    assert registry.is_active("daily-compile") is False


def test_v28_activation_requires_an_authenticated_event():
    registry = RoutineRegistry()
    with pytest.raises(ValueError):
        registry.activate("daily-compile", activation_event_id="")


def test_v28_an_activated_routine_records_its_authority():
    registry = RoutineRegistry()
    registry.activate("daily-compile", activation_event_id="event-42")

    assert registry.is_active("daily-compile") is True
    assert registry.activation_event("daily-compile") == "event-42"


def test_v28_pausing_removes_the_activation():
    registry = RoutineRegistry()
    registry.activate("daily-compile", activation_event_id="event-42")
    registry.pause("daily-compile")

    assert registry.is_active("daily-compile") is False


def test_v28_onboarding_does_not_activate_anything(onboarding, vault):
    """Inspecting a prepared routine must not schedule it or commit to git."""
    before = _snapshot(vault.root)
    onboarding.apply(dry_run=True)

    assert _snapshot(vault.root) == before
    assert not (vault.root / ".git").exists()
