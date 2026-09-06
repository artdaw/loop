"""Environment diagnosis and truthful degradation (interfaces §3, O04, O05, O13).

The point of this module is to make "it works" and "it is configured" different
answers. A connector with no credentials is not broken and it is not ready; a
service whose heartbeat stopped an hour ago is not "running". Collapsing those
into a green tick is how someone finds out their reminders stopped by missing one.

`loop status` must stay useful with no model, no connectors and no network,
because that is the state a new install is in and the state a broken one falls
back to. Deterministic tasks and reminders do not need a model, so they keep
working and say so.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

#: The floor from pyproject's requires-python.
MINIMUM_PYTHON = (3, 12)

#: A heartbeat older than this means the service is not currently running.
HEARTBEAT_STALE_SECONDS = 120


class Readiness(str, Enum):
    READY = "ready"
    UNCONFIGURED = "unconfigured"
    AUTH_REQUIRED = "auth_required"
    DEGRADED = "degraded"
    OFFLINE = "offline"

    @property
    def is_usable(self) -> bool:
        return self is Readiness.READY

    @property
    def is_failure(self) -> bool:
        """Unconfigured is a *choice*, not a fault. Only these are faults."""
        return self in (Readiness.DEGRADED, Readiness.OFFLINE,
                        Readiness.AUTH_REQUIRED)


@dataclass
class Dependency:
    """One optional capability and its honest state."""

    name: str
    readiness: Readiness
    detail: str = ""
    required: bool = False

    @property
    def blocks_startup(self) -> bool:
        return self.required and not self.readiness.is_usable


@dataclass
class DoctorReport:
    python_ok: bool = True
    python_detail: str = ""
    dependencies: list[Dependency] = field(default_factory=list)
    core_features: list[str] = field(default_factory=list)
    degraded_features: list[str] = field(default_factory=list)

    @property
    def can_start(self) -> bool:
        """Loop starts without any optional dependency (O04)."""
        return self.python_ok and not any(d.blocks_startup
                                          for d in self.dependencies)

    def by_name(self, name: str) -> Dependency | None:
        return next((d for d in self.dependencies if d.name == name), None)

    def render(self) -> list[str]:
        lines = [f"python: {'ok' if self.python_ok else self.python_detail}"]
        for dependency in sorted(self.dependencies, key=lambda d: d.name):
            lines.append(f"{dependency.name}: {dependency.readiness.value}"
                         + (f" — {dependency.detail}" if dependency.detail else ""))
        for feature in self.core_features:
            lines.append(f"available: {feature}")
        for feature in self.degraded_features:
            lines.append(f"unavailable: {feature}")
        return lines


def check_python(version: tuple[int, ...] | None = None,
                 minimum: tuple[int, int] = MINIMUM_PYTHON) -> tuple[bool, str]:
    """Check the interpreter before anything imports (O05).

    A version diagnostic must arrive *before* an arbitrary ImportError from
    deep inside a dependency, because that traceback sends people debugging the
    wrong thing entirely.
    """
    actual = version or sys.version_info[:2]
    if tuple(actual[:2]) >= minimum:
        return True, ""
    return False, (
        f"Loop needs Python {minimum[0]}.{minimum[1]} or newer; this is "
        f"{actual[0]}.{actual[1]}. Install a newer interpreter and re-run "
        f"`uv sync --locked`.")


#: Features that work with no model, no network and no connectors (O04).
DETERMINISTIC_FEATURES = (
    "tasks", "reminders", "status", "vault capture", "vault search",
    "backup", "restore",
)

#: Features that genuinely need something optional.
MODEL_FEATURES = ("briefing", "planning", "research", "summaries")
CONNECTOR_FEATURES = ("calendar", "email", "telegram delivery")


def diagnose(*, has_local_model: bool = False, connectors: dict[str, Readiness] |
             None = None, python_version: tuple[int, ...] | None = None,
             ) -> DoctorReport:
    """Report what works, without pretending about what does not."""
    python_ok, python_detail = check_python(python_version)
    report = DoctorReport(python_ok=python_ok, python_detail=python_detail)

    report.dependencies.append(Dependency(
        "local_model",
        Readiness.READY if has_local_model else Readiness.UNCONFIGURED,
        "" if has_local_model else "no local model is configured"))

    for name, readiness in sorted((connectors or {}).items()):
        report.dependencies.append(Dependency(
            name, readiness,
            "" if readiness.is_usable else f"{name} is {readiness.value}"))

    report.core_features.extend(DETERMINISTIC_FEATURES)
    if not has_local_model:
        report.degraded_features.extend(MODEL_FEATURES)
    for name, readiness in sorted((connectors or {}).items()):
        if not readiness.is_usable:
            report.degraded_features.append(f"{name} data")

    return report


@dataclass
class ServiceHealth:
    """What `loop status` can honestly say about the background service (O13)."""

    last_heartbeat_at: int | None
    now: int
    catch_up_limit_seconds: int = 86_400

    @property
    def is_running(self) -> bool:
        if self.last_heartbeat_at is None:
            return False
        return self.now - self.last_heartbeat_at <= HEARTBEAT_STALE_SECONDS

    @property
    def stale_seconds(self) -> int | None:
        if self.last_heartbeat_at is None:
            return None
        return max(0, self.now - self.last_heartbeat_at)

    def describe(self) -> str:
        """Never claims continuous availability (interfaces §3).

        A laptop that slept through the night did not run the 07:00 routine.
        Reporting "running" because the process is alive now would be a claim
        about the past that nobody checked.
        """
        if self.last_heartbeat_at is None:
            return ("The background service has never reported in. Scheduled "
                    "work is not running.")
        if self.is_running:
            return "The background service is running."
        stale = self.stale_seconds or 0
        note = (f"The background service last reported {stale // 60} minutes "
                f"ago, so scheduled work may have been missed.")
        if stale > self.catch_up_limit_seconds:
            note += (f" Catch-up only covers the last "
                     f"{self.catch_up_limit_seconds // 3600} hours; older "
                     f"occurrences are dropped rather than fired late.")
        return note
