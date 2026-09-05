"""Health check — what is wired up, what is not, and what is waiting.

Backs ``loop status``. The guiding rule: **nothing here raises.** An
unconfigured integration, an unreachable Ollama, a missing vault, or an
unopenable database are all *states to report*. A status command that crashes
when something is broken is useless precisely when it is needed.

"Configured" means Loop has what it needs to try — a token, a credentials file
that exists, a vault directory that is really there. It does not mean the
service has been contacted; only Ollama is actually probed, because it is the
one thing every other feature depends on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: Seconds to wait when probing the local model. Short: this is a status check.
PROBE_TIMEOUT = 1.5


@dataclass
class CheckResult:
    """One yes/no health check with a human-readable detail line."""

    ok: bool
    detail: str = ""


@dataclass
class IntegrationStatus:
    """Whether one external integration has what it needs to run."""

    name: str
    configured: bool
    detail: str = ""


@dataclass
class QueueCounts:
    """How much work is currently outstanding."""

    open_tasks: int = 0
    due_today: int = 0
    overdue: int = 0
    open_follow_ups: int = 0


@dataclass
class HealthReport:
    """Everything ``loop status`` prints."""

    environment: str
    database: CheckResult
    vector_store: CheckResult
    ollama: CheckResult
    integrations: list[IntegrationStatus] = field(default_factory=list)
    queue: QueueCounts = field(default_factory=QueueCounts)
    autonomy_default: str = "approve"
    email_ceiling: str = "approve"

    @property
    def configured_count(self) -> int:
        return sum(1 for i in self.integrations if i.configured)

    def render(self) -> str:
        """Render the report as aligned terminal text."""
        lines = [f"Loop status — {self.environment}", ""]

        lines.append("  Storage")
        lines.append(self._row("database", self.database.ok, self.database.detail))
        lines.append(self._row("vectors", self.vector_store.ok,
                               self.vector_store.detail))
        lines.append("")

        lines.append("  Local model")
        lines.append(self._row("ollama", self.ollama.ok, self.ollama.detail))
        lines.append("")

        lines.append(f"  Integrations ({self.configured_count}/{len(self.integrations)}"
                     " configured)")
        for integration in self.integrations:
            lines.append(self._row(integration.name, integration.configured,
                                   integration.detail))
        lines.append("")

        lines.append("  Work queue")
        task_detail = f"{self.queue.open_tasks} open"
        if self.queue.due_today:
            task_detail += f", {self.queue.due_today} due today"
        if self.queue.overdue:
            task_detail += f", {self.queue.overdue} overdue"
        lines.append(f"    {'tasks':<14} {task_detail}")
        lines.append(f"    {'follow-ups':<14} {self.queue.open_follow_ups} waiting")
        lines.append("")

        ceiling = ""
        if self.email_ceiling != "act":
            ceiling = f"  (email_send capped at {self.email_ceiling})"
        lines.append(f"  Autonomy         {self.autonomy_default}{ceiling}")

        return "\n".join(lines)

    @staticmethod
    def _row(label: str, ok: bool, detail: str) -> str:
        mark = "✓" if ok else "·"
        return f"    {mark} {label:<12} {detail}"


class HealthChecker:
    """Builds a :class:`HealthReport`. Never raises."""

    def __init__(self, settings: Settings | None = None,
                 memory: Any | None = None,
                 probe: Callable[[str], bool] | None = None) -> None:
        self.settings = settings or get_settings()
        self._memory = memory
        # Injectable so tests never touch the network.
        self._probe = probe or self._default_probe

    # ------------------------------------------------------------------ #
    # Probes
    # ------------------------------------------------------------------ #
    @staticmethod
    def _default_probe(url: str) -> bool:
        """True when an HTTP GET to ``url`` succeeds quickly."""
        try:
            import httpx

            response = httpx.get(url, timeout=PROBE_TIMEOUT)
            return response.status_code < 500
        except Exception:  # noqa: BLE001 - unreachable is a state, not an error
            return False

    def _check_ollama(self) -> CheckResult:
        base = self.settings.ollama_base_url.rstrip("/")
        try:
            reachable = self._probe(f"{base}/api/tags")
        except Exception:  # noqa: BLE001 - a broken probe means "unreachable"
            reachable = False

        if reachable:
            return CheckResult(True, f"{base} ({self.settings.ollama_default_model})")
        return CheckResult(False, f"{base} not reachable — is Ollama running?")

    def _check_database(self) -> CheckResult:
        try:
            memory = self._get_memory()
            memory.bootstrap()
            path = self.settings.database_url.replace("sqlite:///", "")
            return CheckResult(True, path)
        except Exception as exc:  # noqa: BLE001 - report, never raise
            logger.debug("Database health check failed: %s", exc)
            return CheckResult(False, f"cannot open {self.settings.database_url}")

    def _check_vector_store(self) -> CheckResult:
        path = Path(self.settings.chroma_persist_dir).expanduser()
        if path.is_dir():
            return CheckResult(True, str(path))
        return CheckResult(False, f"{path} (not created yet — index a vault first)")

    # ------------------------------------------------------------------ #
    # Integrations
    # ------------------------------------------------------------------ #
    def _check_integrations(self) -> list[IntegrationStatus]:
        settings = self.settings
        results: list[IntegrationStatus] = []

        # Telegram needs both halves to be usable.
        token = settings.telegram_bot_token.strip()
        chat_id = settings.telegram_chat_id.strip()
        if token and chat_id:
            results.append(IntegrationStatus("telegram", True, "bot + chat id set"))
        elif token:
            results.append(IntegrationStatus("telegram", False,
                                             "token set, chat id missing"))
        else:
            results.append(IntegrationStatus("telegram", False, "no bot token"))

        results.append(self._check_file_credential(
            "gmail", settings.gmail_credentials_path,
            hint="download OAuth client credentials from Google Cloud Console",
        ))

        # Device-code sign-in needs only a client id; the secret is optional.
        if settings.outlook_client_id.strip():
            results.append(IntegrationStatus("outlook", True,
                                             f"tenant {settings.outlook_tenant_id}"))
        else:
            results.append(IntegrationStatus("outlook", False, "no client id"))

        if settings.teams_app_id.strip() and settings.teams_app_password.strip():
            results.append(IntegrationStatus("teams", True, "bot framework app set"))
        else:
            results.append(IntegrationStatus("teams", False, "no app id/password"))

        if settings.wrike_api_key.strip():
            results.append(IntegrationStatus("wrike", True, "api key set"))
        else:
            results.append(IntegrationStatus("wrike", False,
                                             "no api key — sync disabled"))

        results.append(self._check_vault())

        if settings.anthropic_api_key.strip():
            results.append(IntegrationStatus("anthropic", True,
                                             f"{settings.anthropic_model} (fallback)"))
        else:
            results.append(IntegrationStatus("anthropic", False,
                                             "no key — fully local, no cloud fallback"))

        results.append(self._check_voice())
        return results

    @staticmethod
    def _check_file_credential(name: str, raw_path: str, *,
                               hint: str) -> IntegrationStatus:
        path_text = (raw_path or "").strip()
        if not path_text:
            return IntegrationStatus(name, False, f"not configured — {hint}")
        path = Path(path_text).expanduser()
        if path.is_file():
            return IntegrationStatus(name, True, str(path))
        return IntegrationStatus(name, False, f"{path} not found")

    def _check_vault(self) -> IntegrationStatus:
        raw = (self.settings.obsidian_vault_path or "").strip()
        if not raw:
            return IntegrationStatus("obsidian", False, "no vault path set")
        path = Path(raw).expanduser()
        if not path.is_dir():
            return IntegrationStatus("obsidian", False, f"{path} does not exist")

        private = (self.settings.obsidian_private_vault_path or "").strip()
        detail = str(path)
        if private:
            detail += "  (+ private vault)"
        return IntegrationStatus("obsidian", True, detail)

    def _check_voice(self) -> IntegrationStatus:
        try:
            from integrations.transcribe import FasterWhisperTranscriber

            if FasterWhisperTranscriber(self.settings).available():
                return IntegrationStatus(
                    "voice", True, f"whisper {self.settings.whisper_model_size} (local)"
                )
        except Exception:  # noqa: BLE001 - report, never raise
            pass
        return IntegrationStatus("voice", False,
                                 'not installed — pip install -e ".[voice]"')

    # ------------------------------------------------------------------ #
    # Queue + autonomy
    # ------------------------------------------------------------------ #
    def _check_queue(self) -> QueueCounts:
        try:
            memory = self._get_memory()
            return QueueCounts(
                open_tasks=len(memory.list_open_tasks()),
                due_today=len(memory.get_due_today(today=date.today())),
                overdue=len(memory.get_overdue(today=date.today())),
                open_follow_ups=len(memory.list_open_follow_ups()),
            )
        except Exception:  # noqa: BLE001 - a broken queue read must not crash status
            logger.debug("Queue health check failed", exc_info=True)
            return QueueCounts()

    def _autonomy(self) -> tuple[str, str]:
        try:
            from core.autonomy import ActionType, AutonomyGate

            gate = AutonomyGate(self.settings, self._get_memory())
            return (
                gate.level_for(ActionType.TASK_CREATE).label,
                gate.ceiling_for(ActionType.EMAIL_SEND).label,
            )
        except Exception:  # noqa: BLE001 - fall back to the documented defaults
            logger.debug("Autonomy health check failed", exc_info=True)
            return self.settings.default_autonomy_level, self.settings.max_autonomy_email_send

    def _get_memory(self) -> Any:
        if self._memory is None:
            from core.memory import MemoryStore

            self._memory = MemoryStore(self.settings)
        return self._memory

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    def check(self) -> HealthReport:
        """Build the full report."""
        autonomy_default, email_ceiling = self._autonomy()
        return HealthReport(
            environment=self.settings.environment,
            database=self._check_database(),
            vector_store=self._check_vector_store(),
            ollama=self._check_ollama(),
            integrations=self._check_integrations(),
            queue=self._check_queue(),
            autonomy_default=autonomy_default,
            email_ceiling=email_ceiling,
        )
