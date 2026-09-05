"""The weekly review.

``collect()`` is pure aggregation over SQLite so the numbers are testable
without a model; ``compose()`` adds a local-LLM narrative on top and must still
produce the stats when the model is unavailable.
"""

from __future__ import annotations

from datetime import date, timedelta

from specialists.review import ReviewStats, WeeklyReview


class FakeRouter:
    """Router that returns a fixed narrative."""

    def __init__(self, text: str = "A steady week.") -> None:
        self.text = text
        self.calls: list[dict] = []

    def route(self, prompt, context_metadata=None, **kwargs):
        self.calls.append(context_metadata or {})
        return self.text


class BoomRouter:
    """Router that always fails, standing in for Ollama being down."""

    def route(self, *args, **kwargs):
        raise RuntimeError("ollama is down")


def _review(settings, memory_store, router=None) -> WeeklyReview:
    return WeeklyReview(settings, memory=memory_store, router=router)


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
def test_counts_tasks_created_and_completed(settings, memory_store):
    memory_store.add_task("a")
    task = memory_store.add_task("b")
    memory_store.complete_task(task.id)

    stats = _review(settings, memory_store).collect()

    assert stats.tasks_created == 2
    assert stats.tasks_completed == 1
    assert stats.completion_rate == 0.5


def test_zero_created_gives_zero_rate_not_a_division_error(settings, memory_store):
    stats = _review(settings, memory_store).collect()

    assert stats.tasks_created == 0
    assert stats.completion_rate == 0.0
    assert stats.local_share == 0.0


def test_counts_overdue_tasks(settings, memory_store):
    memory_store.add_task("late", due_date=date.today() - timedelta(days=3))
    memory_store.add_task("fine", due_date=date.today() + timedelta(days=3))

    assert _review(settings, memory_store).collect().tasks_overdue == 1


def test_local_share_from_the_usage_log(settings, memory_store):
    for backend in ("local", "local", "cloud"):
        memory_store.log_llm_usage(backend=backend, prompt_hash="x",
                                   latency_ms=10, local_only=False)

    stats = _review(settings, memory_store).collect()

    assert stats.llm_local == 2
    assert stats.llm_cloud == 1
    assert round(stats.local_share, 2) == 0.67


def test_counts_follow_ups_by_status(settings, memory_store):
    memory_store.add_follow_up(thread_id="t1", status="sent")
    memory_store.add_follow_up(thread_id="t2", status="ignored")
    memory_store.add_follow_up(thread_id="t3", status="waiting")

    stats = _review(settings, memory_store).collect()

    assert stats.follow_ups_resolved == 1
    assert stats.follow_ups_ignored == 1
    assert stats.follow_ups_pending == 1


def test_top_projects_are_ranked(settings, memory_store):
    memory_store.add_task("a", project="atlas")
    memory_store.add_task("b", project="atlas")
    memory_store.add_task("c", project="falcon")

    stats = _review(settings, memory_store).collect()

    assert stats.top_projects[0] == ("atlas", 2)


def test_counts_autonomy_actions(settings, memory_store):
    memory_store.log_autonomy_action(action="note_write", level="act",
                                     executed=True, approved=False, detail="")

    assert _review(settings, memory_store).collect().autonomy_actions == 1


def test_window_is_seven_days_ending_sunday(settings, memory_store):
    stats = _review(settings, memory_store).collect()
    assert (stats.week_end - stats.week_start) == timedelta(days=6)


def test_weeks_ago_shifts_the_window_back(settings, memory_store):
    this_week = _review(settings, memory_store).collect()
    last_week = _review(settings, memory_store).collect(weeks_ago=1)

    assert last_week.week_start == this_week.week_start - timedelta(days=7)


def test_rows_outside_the_window_are_excluded(settings, memory_store):
    """A task created before the window must not be counted."""
    task = memory_store.add_task("old")
    old_date = date.today() - timedelta(days=60)
    memory_store.backdate_task(task.id, old_date)

    stats = _review(settings, memory_store).collect()

    assert stats.tasks_created == 0


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #
def test_compose_includes_the_headline_numbers(settings, memory_store):
    memory_store.add_task("a")

    text = _review(settings, memory_store, FakeRouter()).compose()

    assert "Weekly review" in text
    assert "1" in text


def test_compose_appends_the_narrative(settings, memory_store):
    text = _review(settings, memory_store, FakeRouter("A steady week.")).compose()
    assert "A steady week." in text


def test_narrative_is_requested_as_non_private_work(settings, memory_store):
    router = FakeRouter()
    _review(settings, memory_store, router).compose()

    assert router.calls
    assert router.calls[0].get("source") == "work"


def test_compose_survives_a_router_failure(settings, memory_store):
    """A model outage must not cost the user their review."""
    text = _review(settings, memory_store, BoomRouter()).compose()

    assert "Weekly review" in text


def test_compose_without_a_router_still_returns_stats(settings, memory_store):
    text = _review(settings, memory_store, router=None).compose(with_narrative=False)
    assert "Weekly review" in text


def test_compose_accepts_precomputed_stats(settings, memory_store):
    stats = ReviewStats(week_start=date(2026, 8, 31), week_end=date(2026, 9, 6),
                        tasks_created=7)
    text = _review(settings, memory_store, FakeRouter()).compose(stats)

    assert "7" in text
    assert "2026-08-31" in text
