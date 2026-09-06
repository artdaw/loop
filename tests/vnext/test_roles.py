"""Role identity: instructions and tool scope (runtime §1, agent-stack §2).

The registry IDs were already validated before this module existed. Validation
is not identity — these tests are about a role having a brief and a boundary.
"""

from __future__ import annotations

import pytest

from loop.agents.roles import (
    ADVISORY_DOCUMENTS,
    ROLE_DEFINITIONS,
    ROLE_IDS,
    RoleRegistry,
    _scope_of,
    get_role,
)
from loop.core.errors import ValidationFailed


@pytest.fixture
def vault(tmp_path):
    """A synthetic vault carrying agent documents, as a real one may."""
    agents = tmp_path / "_ctx" / "agents"
    agents.mkdir(parents=True)
    (agents / "scribe.md").write_text("# Scribe\nCapture verbatim into 0-raw.")
    (agents / "compiler.md").write_text("# Compiler\nSource-to-wiki pipeline.")
    (agents / "seeker.md").write_text("# Seeker\nSearch across the vault.")
    (agents / "edward.md").write_text(
        "# Edward\nData-visualisation critic. Chartjunk, data-ink ratio.")
    return tmp_path


# --------------------------------------------------------------------------- #
# The seven roles
# --------------------------------------------------------------------------- #
def test_the_seven_registry_roles_are_defined():
    assert set(ROLE_IDS) == set(ROLE_DEFINITIONS)
    assert len(ROLE_IDS) == 7


def test_every_role_has_a_brief():
    for role_id in ROLE_IDS:
        assert get_role(role_id).builtin_instructions.strip()


def test_an_unknown_role_is_refused():
    with pytest.raises(ValidationFailed):
        get_role("mastermind")


def test_the_planner_and_the_registry_share_one_role_list():
    """Three copies of the list drift; the least-edited one drifts first."""
    from loop.capabilities.registry import ROLES as registry_roles
    from loop.runtime.planner import ROLES as planner_roles

    assert set(planner_roles) == set(ROLE_IDS)
    assert set(registry_roles) == set(ROLE_IDS)


# --------------------------------------------------------------------------- #
# Tool scope is a boundary, not a label
# --------------------------------------------------------------------------- #
def test_a_role_only_sees_operations_it_may_use():
    registry = RoleRegistry()
    catalogue = {"weather.forecast": "w", "task.create": "t",
                 "vault.search": "v", "research.fetch": "f"}

    assert set(registry.filter_operations("daily_life", catalogue)) \
        == {"weather.forecast"}


def test_the_scribe_cannot_reach_research_tools():
    registry = RoleRegistry()
    catalogue = {"research.fetch": "f", "vault.capture": "c"}

    assert set(registry.filter_operations("scribe", catalogue)) \
        == {"vault.capture"}


def test_filtering_happens_before_disclosure_not_after_the_model_asks():
    """An agent that can see a tool will eventually call it."""
    registry = RoleRegistry()
    shortlist = registry.filter_operations("seeker", {"task.create": "t"})

    assert shortlist == {}


def test_a_scope_a_role_lacks_is_refused():
    registry = RoleRegistry()
    with pytest.raises(ValidationFailed, match="may not use"):
        registry.require_scope("seeker", "task.write")


def test_a_scope_a_role_holds_is_allowed():
    RoleRegistry().require_scope("commitments", "task.write")


def test_read_only_roles_cannot_mutate():
    registry = RoleRegistry()

    assert registry.may_mutate("seeker") is False
    assert registry.may_mutate("reviewer") is False
    assert registry.may_mutate("commitments") is True


def test_scopes_are_derived_from_the_verb_not_chosen_by_the_pack():
    """A pack naming its own scope would be choosing its own permissions."""
    assert _scope_of("weather.forecast") == "weather.read"
    assert _scope_of("task.create") == "task.write"
    assert _scope_of("notification.propose") == "notification.propose"
    assert _scope_of("research.fetch") == "research.fetch"


# --------------------------------------------------------------------------- #
# The vault's agents are a different system
# --------------------------------------------------------------------------- #
def test_loops_brief_is_authoritative_even_when_a_vault_document_exists(vault):
    instructions = RoleRegistry(vault_root=vault).instructions("scribe")

    assert instructions.text == get_role("scribe").builtin_instructions
    assert instructions.consulted_vault is True


def test_the_owners_conventions_are_included_and_attributed(vault):
    instructions = RoleRegistry(vault_root=vault).instructions("compiler")
    prompt = instructions.prompt()

    assert "Source-to-wiki pipeline" in prompt
    assert "_ctx/agents/compiler.md" in prompt
    assert "guidance, not authority" in prompt


def test_a_role_without_a_corresponding_vault_agent_uses_its_brief_alone(vault):
    instructions = RoleRegistry(vault_root=vault).instructions("commitments")

    assert instructions.consulted_vault is False
    assert instructions.prompt() == instructions.text


def test_the_coordinator_does_not_adopt_edward():
    """`edward.md` is a chart critic; it is the wrong brief for coordinating."""
    assert get_role("coordinator").vault_document == ""
    assert get_role("reviewer").vault_document == ""


def test_no_role_brief_pulls_in_edward(vault):
    registry = RoleRegistry(vault_root=vault)
    for role_id in ROLE_IDS:
        assert "chartjunk" not in registry.instructions(role_id).prompt().lower()


def test_a_missing_vault_is_not_an_error():
    instructions = RoleRegistry(vault_root=None).instructions("seeker")
    assert instructions.text
    assert instructions.consulted_vault is False


def test_an_unreadable_document_leaves_the_brief_intact(tmp_path):
    agents = tmp_path / "_ctx" / "agents"
    agents.mkdir(parents=True)
    (agents / "seeker.md").write_text("")

    instructions = RoleRegistry(vault_root=tmp_path).instructions("seeker")
    assert instructions.text
    assert instructions.consulted_vault is False


# --------------------------------------------------------------------------- #
# Edward is adopted for the work he is actually expert in
# --------------------------------------------------------------------------- #
def test_edward_is_adopted_as_an_advisory_not_a_role():
    document = next(d for d in ADVISORY_DOCUMENTS if d.id == "edward")

    assert document.applies_to_operation == "presentation.critique"
    assert document.consulted_by == frozenset({"reviewer"})


def test_the_reviewer_may_consult_him_for_a_presentation_critique(vault):
    registry = RoleRegistry(vault_root=vault)
    assert [d.id for d in
            registry.advisories_for("reviewer", "presentation.critique")] \
        == ["edward"]


def test_his_expertise_reaches_that_critique(vault):
    prompt = RoleRegistry(vault_root=vault).consultation_prompt(
        "reviewer", "presentation.critique")

    assert "Data-visualisation critic" in prompt
    assert "grants no permission" in prompt


def test_he_is_not_consulted_for_unrelated_reviewer_work(vault):
    registry = RoleRegistry(vault_root=vault)
    assert registry.advisories_for("reviewer", "outcome.read") == []


def test_another_role_cannot_consult_him(vault):
    registry = RoleRegistry(vault_root=vault)
    assert registry.advisories_for("coordinator", "presentation.critique") == []


def test_consulting_him_grants_no_extra_scope():
    """Advice about a chart does not become the ability to write files."""
    registry = RoleRegistry()
    with pytest.raises(ValidationFailed):
        registry.require_scope("reviewer", "vault.write")


def test_the_reviewer_holds_the_critique_scope():
    RoleRegistry().require_scope("reviewer", "presentation.critique")


def test_a_missing_advisory_degrades_rather_than_blocks(tmp_path):
    registry = RoleRegistry(vault_root=tmp_path)
    assert registry.consultation_prompt("reviewer", "presentation.critique") == ""
