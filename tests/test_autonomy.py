"""The autonomy gate.

Autonomy answers "may Loop do this without asking?" — a different question from
the privacy gate's "which model may see this?". These tests pin the resolution
order, the outbound-email ceiling, and the fail-to-asking default.
"""

from __future__ import annotations

import pytest

from config.settings import Settings
from core.autonomy import ActionType, AutonomyGate, AutonomyLevel
from core.exceptions import ApprovalRequiredError


def test_defaults_to_the_configured_level(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    assert gate.level_for(ActionType.TASK_CREATE) is AutonomyLevel.APPROVE


def test_per_action_override_beats_the_global_default(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(None, AutonomyLevel.SUGGEST)
    gate.set_level(ActionType.TASK_CREATE, AutonomyLevel.ACT)

    assert gate.level_for(ActionType.TASK_CREATE) is AutonomyLevel.ACT
    assert gate.level_for(ActionType.NOTE_WRITE) is AutonomyLevel.SUGGEST


def test_global_default_preference_beats_settings(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(None, AutonomyLevel.OBSERVE)
    assert gate.level_for(ActionType.NOTE_WRITE) is AutonomyLevel.OBSERVE


def test_email_send_is_capped_at_approve_by_default(settings, memory_store):
    """Even set to ACT, outbound email still requires approval."""
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(ActionType.EMAIL_SEND, AutonomyLevel.ACT)

    decision = gate.decide(ActionType.EMAIL_SEND)

    assert decision.level is AutonomyLevel.APPROVE
    assert decision.capped is True
    assert decision.requires_approval is True
    assert "ceiling" in decision.reason


def test_email_ceiling_can_be_raised_by_config(tmp_path, memory_store):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        max_autonomy_email_send="act",
    )
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(ActionType.EMAIL_SEND, AutonomyLevel.ACT)

    decision = gate.decide(ActionType.EMAIL_SEND)

    assert decision.level is AutonomyLevel.ACT
    assert decision.capped is False
    assert decision.requires_approval is False


def test_ceiling_does_not_apply_to_other_actions(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(ActionType.NOTE_WRITE, AutonomyLevel.ACT)
    assert gate.decide(ActionType.NOTE_WRITE).level is AutonomyLevel.ACT


def test_act_does_not_require_approval(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(ActionType.NOTE_WRITE, AutonomyLevel.ACT)

    decision = gate.decide(ActionType.NOTE_WRITE)

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_observe_is_not_allowed_at_all(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(ActionType.WRIKE_WRITE, AutonomyLevel.OBSERVE)

    decision = gate.decide(ActionType.WRIKE_WRITE)

    assert decision.allowed is False
    assert decision.requires_approval is False


def test_suggest_is_allowed_but_needs_approval(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(ActionType.WRIKE_WRITE, AutonomyLevel.SUGGEST)

    decision = gate.decide(ActionType.WRIKE_WRITE)

    assert decision.allowed is True
    assert decision.requires_approval is True


def test_guard_raises_without_approval(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    with pytest.raises(ApprovalRequiredError):
        gate.guard(ActionType.TASK_CREATE)


def test_guard_passes_with_approval(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    decision = gate.guard(ActionType.TASK_CREATE, approved=True)
    assert decision.requires_approval is True


def test_guard_raises_when_action_is_disallowed_even_if_approved(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(ActionType.EMAIL_SEND, AutonomyLevel.OBSERVE)
    with pytest.raises(ApprovalRequiredError):
        gate.guard(ActionType.EMAIL_SEND, approved=True)


def test_corrupt_preference_falls_back_instead_of_raising(settings, memory_store):
    memory_store.set_preference("autonomy.task_create", "banana")
    gate = AutonomyGate(settings, memory_store)
    assert gate.level_for(ActionType.TASK_CREATE) is AutonomyLevel.APPROVE


def test_corrupt_settings_default_falls_back_to_approve(tmp_path, memory_store):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        default_autonomy_level="nonsense",
    )
    gate = AutonomyGate(settings, memory_store)
    assert gate.level_for(ActionType.NOTE_WRITE) is AutonomyLevel.APPROVE


def test_levels_are_ordered(settings, memory_store):
    assert AutonomyLevel.OBSERVE < AutonomyLevel.SUGGEST < AutonomyLevel.APPROVE
    assert AutonomyLevel.APPROVE < AutonomyLevel.ACT


def test_record_writes_an_audit_row(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.record(
        ActionType.NOTE_WRITE,
        level=AutonomyLevel.ACT,
        executed=True,
        approved=False,
        detail="voice-note.md",
    )

    rows = gate.recent_audit(limit=10)

    assert len(rows) == 1
    assert rows[0].action == "note_write"
    assert rows[0].level == "act"
    assert rows[0].executed is True
    assert rows[0].detail == "voice-note.md"


def test_guard_audits_a_blocked_action(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    with pytest.raises(ApprovalRequiredError):
        gate.guard(ActionType.TASK_CREATE, detail="task 7")

    rows = gate.recent_audit(limit=10)
    assert len(rows) == 1
    assert rows[0].executed is False


def test_set_level_accepts_a_string(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(ActionType.NOTE_WRITE, "act")
    assert gate.level_for(ActionType.NOTE_WRITE) is AutonomyLevel.ACT


def test_levels_table_reports_every_action(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    table = gate.levels_table()
    assert len(table) == len(list(ActionType))
    assert all(isinstance(decision.level, AutonomyLevel) for decision in table.values())
