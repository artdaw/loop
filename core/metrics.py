"""Metrics — what Loop has been doing, and how much of it stayed local.

Pure aggregation over data Loop already records. This module adds no new
collection: it reads ``llm_usage_log`` (which stores a prompt *hash*, never
content), ``tasks``, ``follow_ups``, and ``autonomy_audit``.

The headline figure is deliberately the privacy one — *"N% of requests served
locally"* — because local-first is the product's central claim, and a claim the
user cannot verify is worth little. Everything else is secondary.

Every ratio goes through :func:`_share`, so an install with no activity yet
renders zeros rather than raising.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

logger = logging.getLogger(__name__)


def _share(numerator: int, denominator: int) -> float:
    """Guarded division: no data means 0.0, never a ZeroDivisionError."""
    return (numerator / denominator) if denominator else 0.0


def _percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile. Handles empty and single-element samples."""
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered))) - 1))
    return ordered[index]


@dataclass
class LLMUsageMetrics:
    """Local-vs-cloud usage over the window."""

    local_count: int = 0
    cloud_count: int = 0
    private_count: int = 0
    avg_latency_ms: int = 0
    p95_latency_ms: int = 0
    daily: list[tuple[str, int]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.local_count + self.cloud_count

    @property
    def local_share(self) -> float:
        return _share(self.local_count, self.total)

    @property
    def local_percent(self) -> int:
        return round(self.local_share * 100)

    @property
    def cloud_percent(self) -> int:
        return 100 - self.local_percent if self.total else 0


@dataclass
class TaskMetrics:
    """Task throughput over the window."""

    created: int = 0
    completed: int = 0
    open: int = 0
    overdue: int = 0

    @property
    def completion_rate(self) -> float:
        return _share(self.completed, self.created)

    @property
    def completion_percent(self) -> int:
        return round(self.completion_rate * 100)


@dataclass
class FollowUpMetrics:
    """How stalled email threads were dealt with."""

    resolved: int = 0
    ignored: int = 0
    pending: int = 0

    @property
    def total(self) -> int:
        return self.resolved + self.ignored + self.pending

    @property
    def resolution_rate(self) -> float:
        return _share(self.resolved, self.total)

    @property
    def resolution_percent(self) -> int:
        return round(self.resolution_rate * 100)


@dataclass
class AutonomyMetrics:
    """How often Loop acted, and how often it had to ask."""

    executed: int = 0
    blocked: int = 0
    by_action: list[tuple[str, int]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.executed + self.blocked

    @property
    def executed_percent(self) -> int:
        return round(_share(self.executed, self.total) * 100)


@dataclass
class DashboardMetrics:
    """Everything the metrics page needs."""

    days: int
    llm: LLMUsageMetrics
    tasks: TaskMetrics
    follow_ups: FollowUpMetrics
    autonomy: AutonomyMetrics

    @property
    def has_data(self) -> bool:
        """False for a fresh install, so the page can show an empty state."""
        return bool(self.llm.total or self.tasks.created or self.tasks.open
                    or self.follow_ups.total or self.autonomy.total)


class MetricsCollector:
    """Reads and aggregates Loop's own activity."""

    def __init__(self, memory: Any) -> None:
        self.memory = memory

    @staticmethod
    def _window(days: int) -> tuple[datetime, datetime]:
        end = datetime.combine(date.today(), time.max)
        start = datetime.combine(date.today() - timedelta(days=max(days - 1, 0)),
                                 time.min)
        return start, end

    # ------------------------------------------------------------------ #
    # Sections
    # ------------------------------------------------------------------ #
    def llm_usage(self, days: int = 30) -> LLMUsageMetrics:
        """Local-vs-cloud split, latency, and a per-day series."""
        start, end = self._window(days)
        rows = self.memory.llm_usage_rows(start, end)

        local = sum(1 for row in rows if row["backend"] == "local")
        cloud = sum(1 for row in rows if row["backend"] == "cloud")
        private = sum(1 for row in rows if row["local_only"])
        latencies = [int(row["latency_ms"] or 0) for row in rows]

        per_day: dict[str, int] = {}
        for row in rows:
            key = row["timestamp"].date().isoformat()
            per_day[key] = per_day.get(key, 0) + 1

        return LLMUsageMetrics(
            local_count=local,
            cloud_count=cloud,
            private_count=private,
            avg_latency_ms=round(sum(latencies) / len(latencies)) if latencies else 0,
            p95_latency_ms=_percentile(latencies, 0.95),
            daily=sorted(per_day.items()),
        )

    def task_metrics(self, days: int = 30) -> TaskMetrics:
        """Task creation, completion, and current backlog."""
        start, end = self._window(days)
        counts = self.memory.activity_counts(start, end)
        return TaskMetrics(
            created=counts["tasks_created"],
            completed=counts["tasks_completed"],
            open=len(self.memory.list_open_tasks()),
            overdue=counts["tasks_overdue"],
        )

    def follow_up_metrics(self, days: int = 30) -> FollowUpMetrics:
        """How flagged threads were resolved."""
        start, end = self._window(days)
        counts = self.memory.activity_counts(start, end)
        return FollowUpMetrics(
            resolved=counts["follow_ups_resolved"],
            ignored=counts["follow_ups_ignored"],
            pending=counts["follow_ups_pending"],
        )

    def autonomy_metrics(self, days: int = 30) -> AutonomyMetrics:
        """How often Loop acted unattended versus having to ask."""
        start, end = self._window(days)
        rows = self.memory.autonomy_rows(start, end)

        executed = sum(1 for row in rows if row["executed"])
        by_action: dict[str, int] = {}
        for row in rows:
            by_action[row["action"]] = by_action.get(row["action"], 0) + 1

        return AutonomyMetrics(
            executed=executed,
            blocked=len(rows) - executed,
            by_action=sorted(by_action.items(), key=lambda pair: (-pair[1], pair[0])),
        )

    def summary(self, days: int = 30) -> DashboardMetrics:
        """Everything at once, for the dashboard and the CLI."""
        return DashboardMetrics(
            days=days,
            llm=self.llm_usage(days),
            tasks=self.task_metrics(days),
            follow_ups=self.follow_up_metrics(days),
            autonomy=self.autonomy_metrics(days),
        )
