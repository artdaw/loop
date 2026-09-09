"""Load measurement for the documented targets (main §9, O12).

This reports what was measured on the machine that ran it, alongside the target
it is being compared against. It does not certify a target: a laptop under no
other load is not a claim about a laptop during a backup, and a number without
its hardware is not a measurement anyone can act on.
"""

from __future__ import annotations

import logging
import platform
import statistics
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class LatencySample:
    name: str
    milliseconds: list[float] = field(default_factory=list)

    def add(self, value: float) -> None:
        self.milliseconds.append(value)

    @property
    def count(self) -> int:
        return len(self.milliseconds)

    @property
    def p50(self) -> float:
        return statistics.median(self.milliseconds) if self.milliseconds else 0.0

    @property
    def p95(self) -> float:
        if not self.milliseconds:
            return 0.0
        ordered = sorted(self.milliseconds)
        index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return ordered[index]

    @property
    def maximum(self) -> float:
        return max(self.milliseconds) if self.milliseconds else 0.0


@dataclass
class LoadReport:
    """A measurement plus the context needed to interpret it."""

    samples: dict[str, LatencySample] = field(default_factory=dict)
    queue_depth: int = 0
    source_count: int = 0
    hardware: str = ""
    targets: dict[str, float] = field(default_factory=dict)

    def sample(self, name: str) -> LatencySample:
        return self.samples.setdefault(name, LatencySample(name))

    def meets(self, name: str) -> bool | None:
        """Whether a measurement met its target, or None when none is defined."""
        target = self.targets.get(name)
        if target is None:
            return None
        return self.sample(name).p95 <= target

    def render(self) -> list[str]:
        lines = [f"hardware: {self.hardware}",
                 f"sources: {self.source_count}",
                 f"queue depth: {self.queue_depth}"]
        for name in sorted(self.samples):
            sample = self.samples[name]
            target = self.targets.get(name)
            line = (f"{name}: n={sample.count} p50={sample.p50:.1f}ms "
                    f"p95={sample.p95:.1f}ms max={sample.maximum:.1f}ms")
            if target is not None:
                verdict = "within" if sample.p95 <= target else "above"
                line += f" (target p95 {target:.0f}ms — {verdict})"
            lines.append(line)
        return lines


def describe_hardware() -> str:
    return (f"{platform.system()} {platform.machine()}, "
            f"Python {platform.python_version()}")


def measure(report: LoadReport, name: str,
            # The return value is discarded; only the timing matters.
            operations: Iterable[Callable[[], object]],
            *, clock: Callable[[], float] = time.perf_counter) -> LatencySample:
    """Time each operation individually, so the tail is visible.

    A total divided by a count hides the slow one, and the slow one is what the
    user notices.
    """
    sample = report.sample(name)
    for operation in operations:
        started = clock()
        operation()
        sample.add((clock() - started) * 1000.0)
    return sample
