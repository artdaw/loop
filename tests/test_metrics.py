"""Metrics aggregation.

Every share is guarded: an install with no activity yet must render zeros, not
a ZeroDivisionError. The headline figure is the privacy one, because that is
the product's central claim and the number the user most needs to verify.
"""

from __future__ import annotations

from datetime import date, timedelta

from core.metrics import MetricsCollector


def _collector(memory_store) -> MetricsCollector:
    return MetricsCollector(memory_store)


# --------------------------------------------------------------------------- #
# Empty states
# --------------------------------------------------------------------------- #
def test_empty_database_yields_zeros_not_errors(memory_store):
    summary = _collector(memory_store).summary()

    assert summary.llm.total == 0
    assert summary.llm.local_share == 0.0
    assert summary.tasks.completion_rate == 0.0
    assert summary.follow_ups.total == 0
    assert summary.autonomy.total == 0


def test_empty_llm_metrics_have_zero_latency(memory_store):
    metrics = _collector(memory_store).llm_usage()
    assert metrics.avg_latency_ms == 0
    assert metrics.p95_latency_ms == 0


def test_has_data_is_false_when_empty(memory_store):
    assert _collector(memory_store).summary().has_data is False


# --------------------------------------------------------------------------- #
# LLM usage — the headline
# --------------------------------------------------------------------------- #
def test_local_share_is_the_headline_number(memory_store):
    for backend in ("local", "local", "local", "cloud"):
        memory_store.log_llm_usage(backend=backend, prompt_hash="h",
                                   latency_ms=100, local_only=False)

    metrics = _collector(memory_store).llm_usage()

    assert metrics.local_count == 3
    assert metrics.cloud_count == 1
    assert metrics.local_share == 0.75
    assert metrics.local_percent == 75


def test_fully_local_install_reports_one_hundred_percent(memory_store):
    memory_store.log_llm_usage(backend="local", prompt_hash="h",
                               latency_ms=10, local_only=True)
    assert _collector(memory_store).llm_usage().local_percent == 100


def test_average_latency(memory_store):
    for latency in (100, 200, 300):
        memory_store.log_llm_usage(backend="local", prompt_hash="h",
                                   latency_ms=latency, local_only=False)

    assert _collector(memory_store).llm_usage().avg_latency_ms == 200


def test_p95_on_a_single_sample_does_not_crash(memory_store):
    memory_store.log_llm_usage(backend="local", prompt_hash="h",
                               latency_ms=42, local_only=True)
    assert _collector(memory_store).llm_usage().p95_latency_ms == 42


def test_p95_picks_the_high_end(memory_store):
    for latency in list(range(1, 100)) + [5000]:
        memory_store.log_llm_usage(backend="local", prompt_hash="h",
                                   latency_ms=latency, local_only=False)

    assert _collector(memory_store).llm_usage().p95_latency_ms >= 95


def test_private_request_count_is_tracked(memory_store):
    memory_store.log_llm_usage(backend="local", prompt_hash="h",
                               latency_ms=10, local_only=True)
    memory_store.log_llm_usage(backend="cloud", prompt_hash="h",
                               latency_ms=10, local_only=False)

    assert _collector(memory_store).llm_usage().private_count == 1


def test_daily_series_has_one_entry_per_day_with_data(memory_store):
    memory_store.log_llm_usage(backend="local", prompt_hash="h",
                               latency_ms=10, local_only=False)

    series = _collector(memory_store).llm_usage().daily

    assert len(series) == 1
    assert series[0][1] >= 1


def test_days_window_excludes_older_rows(memory_store):
    memory_store.log_llm_usage(backend="local", prompt_hash="h",
                               latency_ms=10, local_only=False)
    memory_store.backdate_llm_usage(1, date.today() - timedelta(days=90))

    assert _collector(memory_store).llm_usage(days=30).total == 0


# --------------------------------------------------------------------------- #
# Tasks and follow-ups
# --------------------------------------------------------------------------- #
def test_task_completion_rate(memory_store):
    memory_store.add_task("a")
    task = memory_store.add_task("b")
    memory_store.complete_task(task.id)

    metrics = _collector(memory_store).task_metrics()

    assert metrics.created == 2
    assert metrics.completed == 1
    assert metrics.completion_rate == 0.5


def test_open_and_overdue_counts(memory_store):
    memory_store.add_task("late", due_date=date.today() - timedelta(days=2))
    memory_store.add_task("open")

    metrics = _collector(memory_store).task_metrics()

    assert metrics.open == 2
    assert metrics.overdue == 1


def test_follow_up_breakdown(memory_store):
    memory_store.add_follow_up(thread_id="a", status="sent")
    memory_store.add_follow_up(thread_id="b", status="ignored")
    memory_store.add_follow_up(thread_id="c", status="waiting")

    metrics = _collector(memory_store).follow_up_metrics()

    assert metrics.resolved == 1
    assert metrics.ignored == 1
    assert metrics.pending == 1
    assert metrics.total == 3


def test_follow_up_resolution_rate(memory_store):
    memory_store.add_follow_up(thread_id="a", status="sent")
    memory_store.add_follow_up(thread_id="b", status="waiting")

    assert _collector(memory_store).follow_up_metrics().resolution_rate == 0.5


# --------------------------------------------------------------------------- #
# Autonomy
# --------------------------------------------------------------------------- #
def test_autonomy_metrics_split_executed_from_blocked(memory_store):
    memory_store.log_autonomy_action(action="note_write", level="act",
                                     executed=True, approved=False, detail="")
    memory_store.log_autonomy_action(action="email_send", level="approve",
                                     executed=False, approved=False, detail="")

    metrics = _collector(memory_store).autonomy_metrics()

    assert metrics.total == 2
    assert metrics.executed == 1
    assert metrics.blocked == 1


def test_autonomy_by_action_is_ranked(memory_store):
    for _ in range(2):
        memory_store.log_autonomy_action(action="note_write", level="act",
                                         executed=True, approved=False, detail="")
    memory_store.log_autonomy_action(action="wrike_write", level="act",
                                     executed=True, approved=False, detail="")

    metrics = _collector(memory_store).autonomy_metrics()

    assert metrics.by_action[0] == ("note_write", 2)


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def test_metrics_page_renders(web_client):
    response = web_client.get("/metrics")
    assert response.status_code == 200


def test_metrics_page_shows_the_local_share(web_client, memory_store):
    for backend in ("local", "local", "local", "cloud"):
        memory_store.log_llm_usage(backend=backend, prompt_hash="h",
                                   latency_ms=10, local_only=False)

    body = web_client.get("/metrics").text

    assert "75%" in body
    assert "locally" in body.lower()


def test_metrics_page_has_an_empty_state(web_client):
    body = web_client.get("/metrics").text
    assert "no data yet" in body.lower()


def test_metrics_page_accepts_a_days_window(web_client):
    assert web_client.get("/metrics?days=7").status_code == 200
