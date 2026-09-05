"""Wrike integration — read tasks and create tasks via the Wrike REST API.

Authenticates with a Wrike API key (permanent token). The user will supply the
key later, so this connector is wired for Phase 2.

Phase 2 scope:
    - Authenticated httpx client against https://www.wrike.com/api/v4.
    - list_tasks(): return open tasks with due dates.
    - create_task(): create a task (used by the Task Specialist).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

from config.settings import Settings, get_settings


@dataclass
class WrikeTask:
    """A normalised Wrike task."""

    task_id: str
    title: str
    due: date | None = None
    status: str = "Active"


class WrikeClient:
    """Thin wrapper around the Wrike REST API."""

    BASE_URL = "https://www.wrike.com/api/v4"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # TODO(phase2): build httpx.Client with Bearer settings.wrike_api_key.
        self._http = httpx.Client(base_url=self.BASE_URL, timeout=30.0)

    def list_tasks(self) -> list[WrikeTask]:
        """Return open tasks with due dates."""
        # TODO(phase2): GET /tasks?status=Active&fields=[dates]; normalise.
        raise NotImplementedError("WrikeClient.list_tasks is a Phase 2 stub.")

    def create_task(self, *, title: str, due: date | None = None,
                    folder_id: str | None = None) -> WrikeTask:
        """Create a task in Wrike."""
        # TODO(phase2): POST /folders/{id}/tasks (or /tasks) with the payload.
        raise NotImplementedError("WrikeClient.create_task is a Phase 2 stub.")
