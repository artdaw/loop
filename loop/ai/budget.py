"""Shared budget accounting (agent-stack §3, runtime §2).

One accounting service per **root request**, shared across every child
assignment, schema repair and parallel branch. That sharing is the whole point:
a budget enforced per-call is not a budget, because spawning children multiplies
it. LG05 exists to catch exactly that.

Budget is *reserved before* a call and *recorded after*, including on failure.
Charging only on success would let a run burn its deadline on failures and still
report budget remaining.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from loop.core.errors import BudgetExhausted


@dataclass(frozen=True)
class BudgetLimits:
    """Per-root-request ceilings (runtime §2 defaults)."""

    wall_clock_seconds: float = 120.0
    model_calls: int = 12
    total_tokens: int = 32_000
    tool_calls: int = 30

    @classmethod
    def interactive(cls) -> BudgetLimits:
        return cls()

    @classmethod
    def background_routine(cls) -> BudgetLimits:
        return cls(wall_clock_seconds=90.0, model_calls=4, total_tokens=16_000,
                   tool_calls=12)

    @classmethod
    def research_batch(cls) -> BudgetLimits:
        return cls(wall_clock_seconds=600.0, model_calls=24, total_tokens=96_000,
                   tool_calls=80)


@dataclass
class BudgetUsage:
    """What has been consumed so far."""

    model_calls: int = 0
    total_tokens: int = 0
    tool_calls: int = 0
    failures: int = 0


@dataclass
class RootBudget:
    """A single shared budget for one root request and all its descendants.

    Thread-safe because parallel children may reserve concurrently; without the
    lock two branches can each see room for the last call.
    """

    limits: BudgetLimits = field(default_factory=BudgetLimits)
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    root_id: str = "root"
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------ #
    # Reservation
    # ------------------------------------------------------------------ #
    def reserve_model_call(self, *, estimated_tokens: int = 0) -> None:
        """Claim one model call up front, or refuse the whole request."""
        with self._lock:
            if self.usage.model_calls >= self.limits.model_calls:
                raise BudgetExhausted(
                    "Model call budget exhausted for this request.",
                    details={"root_id": self.root_id,
                             "model_calls": self.usage.model_calls,
                             "limit": self.limits.model_calls})
            if (self.usage.total_tokens + estimated_tokens
                    > self.limits.total_tokens):
                raise BudgetExhausted(
                    "Token budget exhausted for this request.",
                    details={"root_id": self.root_id,
                             "total_tokens": self.usage.total_tokens,
                             "limit": self.limits.total_tokens})
            self.usage.model_calls += 1

    def reserve_tool_call(self) -> None:
        with self._lock:
            if self.usage.tool_calls >= self.limits.tool_calls:
                raise BudgetExhausted(
                    "Tool call budget exhausted for this request.",
                    details={"root_id": self.root_id,
                             "tool_calls": self.usage.tool_calls,
                             "limit": self.limits.tool_calls})
            self.usage.tool_calls += 1

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    def record_tokens(self, tokens: int) -> None:
        with self._lock:
            self.usage.total_tokens += max(0, tokens)

    def record_failure(self) -> None:
        """A failed call still consumed its reserved allowance."""
        with self._lock:
            self.usage.failures += 1

    # ------------------------------------------------------------------ #
    # Inspection
    # ------------------------------------------------------------------ #
    @property
    def model_calls_remaining(self) -> int:
        return max(0, self.limits.model_calls - self.usage.model_calls)

    @property
    def exhausted(self) -> bool:
        return (self.usage.model_calls >= self.limits.model_calls
                or self.usage.total_tokens >= self.limits.total_tokens)

    def snapshot(self) -> dict[str, int]:
        return {"model_calls": self.usage.model_calls,
                "total_tokens": self.usage.total_tokens,
                "tool_calls": self.usage.tool_calls,
                "failures": self.usage.failures}
