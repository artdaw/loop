"""Weekly review — what happened this week, and what it means.

Deliberately split in two:

* :meth:`WeeklyReview.collect` is **pure aggregation** over SQLite. No LLM, no
  network — so the numbers are unit-testable against a seeded database.
* :meth:`WeeklyReview.compose` renders those numbers deterministically and then
  *appends* a short narrative from the local model.

The split is what makes the failure mode acceptable: if Ollama is down, the
narrative is dropped and the user still gets their review. A model outage must
not cost them the week's figures.

The narrative is non-private work data (aggregate counts, never content), so it
routes with ``source="work"`` and may use the cloud fallback like any other
work request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are Loop, a concise personal chief-of-staff. Given a week's activity "
    "statistics, write ONE short paragraph (max 3 sentences) reflecting on the "
    "week. Be specific about the numbers you were given. Do not invent facts, "
    "do not add bullet points, and do not repeat the raw statistics verbatim."
)


@dataclass
class ReviewStats:
    """Aggregated activity for one week. Pure data — no LLM involved."""

    week_start: date
    week_end: date
    tasks_created: int = 0
    tasks_completed: int = 0
    tasks_overdue: int = 0
    completion_rate: float = 0.0
    follow_ups_resolved: int = 0
    follow_ups_ignored: int = 0
    follow_ups_pending: int = 0
    llm_local: int = 0
    llm_cloud: int = 0
    local_share: float = 0.0
    autonomy_actions: int = 0
    top_projects: list[tuple[str, int]] = field(default_factory=list)

    @property
    def llm_total(self) -> int:
        return self.llm_local + self.llm_cloud


def _share(numerator: int, denominator: int) -> float:
    """Guarded division: an empty week is 0.0, never a ZeroDivisionError."""
    return (numerator / denominator) if denominator else 0.0


class WeeklyReview:
    """Builds the weekly digest."""

    def __init__(self, settings: Settings | None = None,
                 memory: Any | None = None,
                 router: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._memory = memory
        self._router = router

    # ------------------------------------------------------------------ #
    # Collaborators
    # ------------------------------------------------------------------ #
    def _get_memory(self) -> Any:
        if self._memory is None:
            from core.memory import MemoryStore

            self._memory = MemoryStore(self.settings)
            self._memory.bootstrap()
        return self._memory

    # ------------------------------------------------------------------ #
    # Stats
    # ------------------------------------------------------------------ #
    def collect(self, week_start: date | None = None, *,
                weeks_ago: int = 0) -> ReviewStats:
        """Aggregate one week's activity. Monday-to-Sunday by default."""
        start = week_start or self._week_start(weeks_ago=weeks_ago)
        end = start + timedelta(days=6)
        memory = self._get_memory()

        window_start = datetime.combine(start, time.min)
        window_end = datetime.combine(end, time.max)

        counts = memory.activity_counts(window_start, window_end)

        created = counts.get("tasks_created", 0)
        completed = counts.get("tasks_completed", 0)
        local = counts.get("llm_local", 0)
        cloud = counts.get("llm_cloud", 0)

        return ReviewStats(
            week_start=start,
            week_end=end,
            tasks_created=created,
            tasks_completed=completed,
            tasks_overdue=counts.get("tasks_overdue", 0),
            completion_rate=_share(completed, created),
            follow_ups_resolved=counts.get("follow_ups_resolved", 0),
            follow_ups_ignored=counts.get("follow_ups_ignored", 0),
            follow_ups_pending=counts.get("follow_ups_pending", 0),
            llm_local=local,
            llm_cloud=cloud,
            local_share=_share(local, local + cloud),
            autonomy_actions=counts.get("autonomy_actions", 0),
            top_projects=counts.get("top_projects", []),
        )

    @staticmethod
    def _week_start(*, weeks_ago: int = 0) -> date:
        """The Monday of the current week, shifted back ``weeks_ago`` weeks."""
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        return monday - timedelta(days=7 * weeks_ago)

    # ------------------------------------------------------------------ #
    # Composition
    # ------------------------------------------------------------------ #
    def compose(self, stats: ReviewStats | None = None, *,
                with_narrative: bool = True) -> str:
        """Render the review: deterministic stats, then an optional narrative."""
        stats = stats or self.collect()
        lines = self._stats_block(stats)

        if with_narrative and self._router is not None:
            narrative = self._narrative(stats)
            if narrative:
                lines.extend(["", narrative])

        return "\n".join(lines).strip()

    @staticmethod
    def _stats_block(stats: ReviewStats) -> list[str]:
        """The part that always renders, model or no model."""
        lines = [
            f"📆 *Weekly review* — {stats.week_start.isoformat()} to "
            f"{stats.week_end.isoformat()}",
            "",
            "*Tasks*",
            f"  • created: {stats.tasks_created}",
            f"  • completed: {stats.tasks_completed} "
            f"({stats.completion_rate * 100:.0f}% of created)",
        ]
        if stats.tasks_overdue:
            lines.append(f"  ⚠️ overdue right now: {stats.tasks_overdue}")

        lines.extend([
            "",
            "*Follow-ups*",
            f"  • resolved: {stats.follow_ups_resolved}",
            f"  • ignored: {stats.follow_ups_ignored}",
            f"  • still pending: {stats.follow_ups_pending}",
        ])

        if stats.llm_total:
            lines.extend([
                "",
                "*Privacy*",
                f"  • {stats.local_share * 100:.0f}% of {stats.llm_total} AI "
                f"requests served locally ({stats.llm_cloud} used the cloud)",
            ])

        if stats.autonomy_actions:
            lines.extend(["", f"*Autonomy* — {stats.autonomy_actions} gated action(s)"])

        if stats.top_projects:
            lines.extend(["", "*Most active projects*"])
            lines.extend(f"  • {slug}: {count}" for slug, count in stats.top_projects)

        return lines

    def _narrative(self, stats: ReviewStats) -> str:
        """One local-model paragraph. Returns '' when unavailable."""
        router = self._router
        if router is None:
            return ""
        prompt = (
            "Week of "
            f"{stats.week_start.isoformat()} to {stats.week_end.isoformat()}.\n"
            f"Tasks created: {stats.tasks_created}; completed: {stats.tasks_completed}; "
            f"currently overdue: {stats.tasks_overdue}.\n"
            f"Follow-ups resolved: {stats.follow_ups_resolved}; "
            f"ignored: {stats.follow_ups_ignored}; pending: {stats.follow_ups_pending}.\n"
            f"AI requests: {stats.llm_total} "
            f"({stats.local_share * 100:.0f}% local).\n"
            f"Most active projects: {stats.top_projects or 'none'}.\n\n"
            "Write the reflection paragraph."
        )
        try:
            text = router.route(prompt, {"source": "work"},
                                      system=_SYSTEM_PROMPT)
            return (text or "").strip()
        except TypeError:
            # Routers whose sync signature takes no `system` kwarg.
            try:
                return (router.route(prompt, {"source": "work"}) or "").strip()
            except Exception:  # noqa: BLE001
                logger.exception("Weekly review narrative failed")
                return ""
        except Exception:  # noqa: BLE001 - stats matter more than prose
            logger.exception("Weekly review narrative failed; returning stats only")
            return ""
