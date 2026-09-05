"""`loop snooze` hides a follow-up from the briefing."""

from __future__ import annotations

from core.memory import MemoryStore


def test_snoozing_hides_a_follow_up_from_the_open_list(memory_store: MemoryStore):
    follow_up = memory_store.add_follow_up(thread_id="t1", subject="Contract")
    assert len(memory_store.list_open_follow_ups()) == 1

    assert memory_store.snooze_follow_up(follow_up.id, 24) is True

    assert memory_store.list_open_follow_ups() == []
    assert len(memory_store.list_open_follow_ups(include_snoozed=True)) == 1


def test_snoozing_a_missing_follow_up_returns_false(memory_store: MemoryStore):
    assert memory_store.snooze_follow_up(9999, 24) is False


def test_snoozed_thread_leaves_the_briefing(settings, memory_store):
    from specialists.briefing import DailyBriefing

    follow_up = memory_store.add_follow_up(thread_id="t1", subject="Contract review")
    assert "Contract review" in DailyBriefing(settings, memory=memory_store).compose()

    memory_store.snooze_follow_up(follow_up.id, 24)

    assert "Contract review" not in DailyBriefing(settings, memory=memory_store).compose()
