"""The weekly review (runtime §1 Reviewer, vault §3).

A review is a report about what the canonical records say. Where they say
nothing, the review says nothing — it does not reach for a cached dashboard
number to fill the gap.

The failure this guards against is specific and easy to write by accident: a
dashboard was computed on Monday, the review runs on Sunday, and the review
reports Monday's percentage as this week's progress. The number looks right,
carries no visible age, and quietly misstates a week of work. So a stale
dashboard is *evidence of staleness*, never a progress source (P18).

The 14-day and 30-day inactivity thresholds both exist on purpose: an existing
routine in the vault flags projects at 14 days within its own explicit scope,
and Loop's general discretionary staleness check uses 30. Collapsing them into
one number would silently rescope a routine the user already has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

DAY = 86_400

#: Loop's general discretionary staleness threshold (vault §3).
GENERAL_STALENESS_DAYS = 30
#: The pre-existing daily routine's own narrower scope. Left as it is.
EXISTING_ROUTINE_STALENESS_DAYS = 14


class ProgressSource(str, Enum):
    CANONICAL = "canonical"
    UNKNOWN = "unknown"


@dataclass
class CanonicalRecord:
    """A goal or project record from the vault — the only progress authority."""

    ref: str
    title: str
    updated_at: int
    progress: float | None = None
    status: str = ""
    layer: str = "1-wiki"


@dataclass
class DashboardSnapshot:
    """A previously computed view. Useful for its age, not for its numbers."""

    ref: str
    progress: float
    computed_at: int

    def is_stale(self, *, now: int, max_age_seconds: int = 7 * DAY) -> bool:
        return now - self.computed_at > max_age_seconds


@dataclass
class ProjectLine:
    ref: str
    title: str
    progress: float | None
    source: ProgressSource
    days_since_update: int
    note: str = ""

    @property
    def is_inactive(self) -> bool:
        return self.days_since_update >= GENERAL_STALENESS_DAYS


@dataclass
class WeeklyReview:
    generated_at: int
    projects: list[ProjectLine] = field(default_factory=list)
    inactive: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def line_for(self, ref: str) -> ProjectLine | None:
        return next((line for line in self.projects if line.ref == ref), None)


def build_review(records: list[CanonicalRecord], *, now: int,
                 dashboards: list[DashboardSnapshot] | None = None,
                 staleness_days: int = GENERAL_STALENESS_DAYS) -> WeeklyReview:
    """Assemble a review from canonical records only (P18)."""
    review = WeeklyReview(generated_at=now)
    by_ref = {snapshot.ref: snapshot for snapshot in (dashboards or [])}

    for record in sorted(records, key=lambda r: r.ref):
        days = (now - record.updated_at) // DAY
        snapshot = by_ref.get(record.ref)

        if record.progress is not None:
            line = ProjectLine(record.ref, record.title, record.progress,
                               ProgressSource.CANONICAL, days)
        else:
            # The dashboard may well have a number here. It is not this week's.
            line = ProjectLine(record.ref, record.title, None,
                               ProgressSource.UNKNOWN, days,
                               note="the canonical record does not state progress")
            if snapshot is not None:
                review.caveats.append(
                    f"{record.ref}: a dashboard figure exists from "
                    f"{(now - snapshot.computed_at) // DAY} days ago; it is not "
                    f"used as current progress")

        if snapshot is not None and snapshot.is_stale(now=now):
            review.caveats.append(
                f"{record.ref}: dashboard snapshot is stale "
                f"({(now - snapshot.computed_at) // DAY} days old)")

        if days >= staleness_days:
            review.inactive.append(record.ref)

        review.projects.append(line)

    return review


def render_review(review: WeeklyReview) -> list[str]:
    """Render deterministically. A model may rephrase; it cannot add numbers."""
    lines: list[str] = []
    for project in review.projects:
        if project.progress is None:
            lines.append(f"{project.title}: progress not recorded "
                         f"({project.days_since_update} days since last update)")
        else:
            lines.append(f"{project.title}: {project.progress:.0%} "
                         f"({project.days_since_update} days since last update)")
    for ref in review.inactive:
        lines.append(f"{ref} has had no activity for "
                     f"{GENERAL_STALENESS_DAYS}+ days.")
    lines.extend(review.caveats)
    return lines
