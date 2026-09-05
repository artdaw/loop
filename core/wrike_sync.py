"""Wrike bidirectional sync — reconcile local tasks with a Wrike workspace.

**Conflict policy: Wrike wins, Loop pushes new.** Chosen deliberately, because
Wrike is a *team* tool: a colleague's edit there must not be silently reverted
by one peer's laptop. Loop still owns work it captured itself, and pushes it up.

===========================================  ==========================================
Situation                                    Resolution
===========================================  ==========================================
Task in both, differs                        remote wins — local row updated
Task only in Wrike                           created locally, ``source="wrike"``
Task only local, no ``wrike_id``             pushed up (gated by ``WRIKE_WRITE``)
Completed locally, open in Wrike             completion pushed
Completed in Wrike, open locally             completed locally
Deleted in Wrike                             marked ``orphaned``, never deleted
===========================================  ==========================================

Two rules shape the error handling:

* **Never raise.** An unconfigured key, an unreachable API, or a malformed task
  produces a :class:`SyncReport` describing what happened. Sync is a background
  job; it must not take the process down.
* **Reading is not acting.** The autonomy gate governs *writes* to Wrike. A pull
  runs regardless of the autonomy level, because reading changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from config.settings import Settings, get_settings
from core.autonomy import ActionType
from core.exceptions import WrikeNotConfiguredError
from core.memory import utcnow

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    """What one sync run did."""

    configured: bool
    pulled_new: int = 0
    pulled_updated: int = 0
    pushed_new: int = 0
    pushed_completed: int = 0
    orphaned: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False
    push_blocked: bool = False

    @property
    def changed(self) -> int:
        """Total number of records touched."""
        return (self.pulled_new + self.pulled_updated + self.pushed_new
                + self.pushed_completed + self.orphaned)

    def summary(self) -> str:
        """One-line human-readable summary."""
        if not self.configured:
            return "Wrike sync: not configured (set WRIKE_API_KEY in .env)."

        parts = [
            f"pulled {self.pulled_new} new",
            f"{self.pulled_updated} updated",
            f"pushed {self.pushed_new} new",
            f"{self.pushed_completed} completed",
        ]
        if self.orphaned:
            parts.append(f"{self.orphaned} orphaned")
        prefix = "Wrike sync (dry run):" if self.dry_run else "Wrike sync:"
        text = f"{prefix} " + ", ".join(parts) + "."
        if self.push_blocked:
            text += " Pushes need approval (raise wrike_write autonomy to act)."
        if self.errors:
            text += f" {len(self.errors)} error(s)."
        return text


class WrikeSync:
    """Reconciles :class:`~core.memory.Task` rows with Wrike tasks."""

    def __init__(self, memory: Any, client: Any | None = None,
                 gate: Any | None = None,
                 settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.memory = memory
        self._client = client
        self._gate = gate

    # ------------------------------------------------------------------ #
    # Collaborators
    # ------------------------------------------------------------------ #
    def _get_client(self) -> Any:
        if self._client is None:
            from integrations.wrike import WrikeClient

            self._client = WrikeClient(self.settings)
        return self._client

    def _get_gate(self) -> Any:
        if self._gate is None:
            from core.autonomy import AutonomyGate

            self._gate = AutonomyGate(self.settings, self.memory)
        return self._gate

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    async def sync(self, *, dry_run: bool = False) -> SyncReport:
        """Pull from Wrike, then push local work up. Never raises."""
        client = self._get_client()
        report = SyncReport(configured=bool(getattr(client, "configured", False)),
                            dry_run=dry_run)
        if not report.configured:
            logger.info("Wrike sync skipped: no API key configured")
            return report

        try:
            remote = await client.list_tasks()
        except WrikeNotConfiguredError:
            report.configured = False
            return report
        except Exception as exc:  # noqa: BLE001 - a background job must not crash
            logger.exception("Wrike sync failed to list tasks")
            report.errors.append(f"list_tasks: {exc}")
            return report

        self._pull(remote, report, dry_run=dry_run)
        await self._push(client, remote, report, dry_run=dry_run)
        return report

    # ------------------------------------------------------------------ #
    # Pull — remote wins
    # ------------------------------------------------------------------ #
    def _pull(self, remote: list, report: SyncReport, *, dry_run: bool) -> None:
        """Apply remote state to local rows. Reading needs no approval."""
        local_by_wrike_id = {
            task.wrike_id: task
            for task in self._all_tasks()
            if task.wrike_id
        }

        seen: set[str] = set()
        for remote_task in remote:
            wrike_id = getattr(remote_task, "task_id", "")
            if not wrike_id:
                continue
            seen.add(wrike_id)
            existing = local_by_wrike_id.get(wrike_id)
            try:
                if existing is None:
                    self._create_local(remote_task, report, dry_run=dry_run)
                else:
                    self._update_local(existing, remote_task, report, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - one bad task must not stop the run
                logger.exception("Failed to reconcile Wrike task %s", wrike_id)
                report.errors.append(f"pull {wrike_id}: {exc}")

        # Anything linked but no longer present upstream was deleted in Wrike.
        for wrike_id, task in local_by_wrike_id.items():
            if wrike_id in seen or task.status == "orphaned":
                continue
            report.orphaned += 1
            if not dry_run:
                # Kept, not deleted: never destroy user data on a remote 404.
                self.memory.set_task_status(task.id, "orphaned")

    def _create_local(self, remote_task: Any, report: SyncReport, *,
                      dry_run: bool) -> None:
        report.pulled_new += 1
        if dry_run:
            return
        task = self.memory.add_task(
            remote_task.title,
            due_date=remote_task.due,
            source="wrike",
            wrike_id=remote_task.task_id,
        )
        if getattr(remote_task, "completed", False):
            self.memory.complete_task(task.id)
        self.memory.mark_task_synced(task.id, remote_updated_at=remote_task.updated_at)

    def _update_local(self, existing: Any, remote_task: Any, report: SyncReport, *,
                      dry_run: bool) -> None:
        remote_completed = bool(getattr(remote_task, "completed", False))
        title_changed = existing.description != remote_task.title
        due_changed = existing.due_date != remote_task.due
        status_changed = remote_completed and existing.status != "done"

        if not (title_changed or due_changed or status_changed):
            return

        report.pulled_updated += 1
        if dry_run:
            return

        if title_changed or due_changed:
            self.memory.update_task_fields(
                existing.id,
                description=remote_task.title,
                due_date=remote_task.due,
            )
        if status_changed:
            self.memory.complete_task(existing.id)
        self.memory.mark_task_synced(existing.id,
                                     remote_updated_at=remote_task.updated_at)

    # ------------------------------------------------------------------ #
    # Push — Loop sends its own work up
    # ------------------------------------------------------------------ #
    async def _push(self, client: Any, remote: list, report: SyncReport, *,
                    dry_run: bool) -> None:
        """Create locally-captured tasks in Wrike and push local completions."""
        decision = self._get_gate().decide(ActionType.WRIKE_WRITE)
        if not decision.allowed or decision.requires_approval:
            report.push_blocked = True
            logger.info("Wrike push skipped: %s", decision.reason)
            return

        remote_by_id = {getattr(t, "task_id", ""): t for t in remote}

        for task in self._all_tasks():
            try:
                if task.status == "orphaned":
                    continue
                if not task.wrike_id and task.source != "wrike" and task.status == "open":
                    await self._push_new(client, task, report, dry_run=dry_run)
                elif task.wrike_id and task.status == "done":
                    remote_task = remote_by_id.get(task.wrike_id)
                    if remote_task is not None and not getattr(remote_task, "completed",
                                                               False):
                        await self._push_completion(client, task, report,
                                                    dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - keep going through the list
                logger.exception("Failed to push task %s to Wrike", task.id)
                report.errors.append(f"push {task.id}: {exc}")

    async def _push_new(self, client: Any, task: Any, report: SyncReport, *,
                        dry_run: bool) -> None:
        if dry_run:
            report.pushed_new += 1
            return
        created = await client.create_task(title=task.description, due=task.due_date)
        self.memory.link_wrike(task.id, created.task_id)
        self.memory.mark_task_synced(task.id,
                                     remote_updated_at=getattr(created, "updated_at", None))
        report.pushed_new += 1
        self._get_gate().record(
            ActionType.WRIKE_WRITE,
            level=self._get_gate().level_for(ActionType.WRIKE_WRITE),
            executed=True,
            detail=f"created wrike task for local task {task.id}",
        )

    async def _push_completion(self, client: Any, task: Any, report: SyncReport, *,
                               dry_run: bool) -> None:
        if dry_run:
            report.pushed_completed += 1
            return
        await client.complete_task(task.wrike_id)
        self.memory.mark_task_synced(task.id)
        report.pushed_completed += 1
        self._get_gate().record(
            ActionType.WRIKE_WRITE,
            level=self._get_gate().level_for(ActionType.WRIKE_WRITE),
            executed=True,
            detail=f"completed wrike task {task.wrike_id}",
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _all_tasks(self) -> list:
        """Every task regardless of status — sync cares about done ones too."""
        return self.memory.list_tasks_for_sync()


def last_synced_now() -> Any:
    """Timestamp helper shared with the memory layer."""
    return utcnow()
