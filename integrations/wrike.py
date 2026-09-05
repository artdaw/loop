"""Wrike integration — read and write tasks via the Wrike REST API v4.

Authenticated with a permanent API token (``WRIKE_API_KEY``). The client is
async and lazily constructed, matching how the other SDK-backed integrations in
Loop behave: importing this module costs nothing and requires no credentials.

Wrike's v4 API takes most write parameters as **query-string** values rather
than a JSON body, which is why the methods below pass ``params=`` rather than
``json=``. Dates use ``YYYY-MM-DD``.

Without a key, :attr:`WrikeClient.configured` is ``False`` and every call raises
:class:`~core.exceptions.WrikeNotConfiguredError`. Callers are expected to check
``configured`` first — :class:`core.wrike_sync.WrikeSync` turns the unconfigured
case into a report rather than an error, so an install with no Wrike key simply
has a feature switched off.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx

from config.settings import Settings, get_settings
from core.exceptions import WrikeNotConfiguredError

logger = logging.getLogger(__name__)


@dataclass
class WrikeTask:
    """A normalised Wrike task."""

    task_id: str
    title: str
    due: date | None = None
    status: str = "Active"
    updated_at: datetime | None = None
    permalink: str = ""
    metadata: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def completed(self) -> bool:
        """Wrike marks finished work as ``Completed``."""
        return self.status.lower() == "completed"


def _parse_date(value: Any) -> date | None:
    """Parse ``YYYY-MM-DD`` (possibly with a time suffix). ``None`` if unusable."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    """Parse a Wrike ISO-8601 timestamp (``...Z``). ``None`` if unusable."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Store naive UTC to match the rest of Loop's timestamps.
    return parsed.replace(tzinfo=None) if parsed.tzinfo is None else (
        parsed.astimezone(tz=None).replace(tzinfo=None)
    )


class WrikeClient:
    """Thin async wrapper around the Wrike REST API."""

    BASE_URL = "https://www.wrike.com/api/v4"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ #
    # Wiring
    # ------------------------------------------------------------------ #
    @property
    def api_key(self) -> str:
        return (self.settings.wrike_api_key or "").strip()

    @property
    def configured(self) -> bool:
        """True when an API key is present. Check this before calling."""
        return bool(self.api_key)

    def _require_configured(self) -> None:
        if not self.configured:
            raise WrikeNotConfiguredError(
                "No Wrike API key configured. Set WRIKE_API_KEY in .env to enable sync."
            )

    def _client(self) -> httpx.AsyncClient:
        """Lazily build the authenticated HTTP client."""
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.BASE_URL,
                timeout=30.0,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._http

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------ #
    # Requests
    # ------------------------------------------------------------------ #
    async def _request(self, method: str, path: str, **kwargs: Any) -> list[dict]:
        """Issue a request and return the ``data`` array from the response."""
        self._require_configured()
        response = await self._client().request(method, path, **kwargs)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, list) else []

    async def _single(self, method: str, path: str, **kwargs: Any) -> WrikeTask:
        """Issue a request expected to return exactly one task."""
        data = await self._request(method, path, **kwargs)
        if not data:
            raise RuntimeError(f"Wrike returned no task for {method} {path}")
        return self._to_task(data[0])

    @staticmethod
    def _to_task(raw: dict) -> WrikeTask:
        """Normalise one Wrike API task object."""
        dates = raw.get("dates") or {}
        return WrikeTask(
            task_id=str(raw.get("id") or ""),
            title=str(raw.get("title") or ""),
            due=_parse_date(dates.get("due")),
            status=str(raw.get("status") or "Active"),
            updated_at=_parse_datetime(raw.get("updatedDate")),
            permalink=str(raw.get("permalink") or ""),
            metadata=raw,
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def list_tasks(self, *, updated_since: date | None = None,
                         include_completed: bool = True) -> list[WrikeTask]:
        """Return tasks, optionally only those updated since a date.

        Entries without an id are skipped rather than producing a task with an
        empty key — a malformed row must not corrupt the local sync state.
        """
        params: dict[str, str] = {
            "fields": json.dumps(["dates"]),
        }
        if not include_completed:
            params["status"] = "Active"
        if updated_since is not None:
            # Wrike expects an ISO-8601 range object for date filters.
            params["updatedDate"] = json.dumps({"start": f"{updated_since.isoformat()}T00:00:00Z"})

        data = await self._request("GET", "/tasks", params=params)
        tasks = [self._to_task(raw) for raw in data if raw.get("id")]
        if len(tasks) != len(data):
            logger.warning("Skipped %d Wrike task(s) with no id", len(data) - len(tasks))
        return tasks

    async def create_task(self, *, title: str, due: date | None = None,
                          folder_id: str | None = None) -> WrikeTask:
        """Create a task, optionally inside a folder."""
        self._require_configured()
        params: dict[str, str] = {"title": title}
        if due is not None:
            params["dates"] = json.dumps({"due": due.isoformat()})

        folder = folder_id or (self.settings.wrike_folder_id or "").strip()
        path = f"/folders/{folder}/tasks" if folder else "/tasks"
        return await self._single("POST", path, params=params)

    async def update_task(self, task_id: str, *, title: str | None = None,
                          due: date | None = None) -> WrikeTask:
        """Update a task's title and/or due date."""
        params: dict[str, str] = {}
        if title is not None:
            params["title"] = title
        if due is not None:
            params["dates"] = json.dumps({"due": due.isoformat()})
        return await self._single("PUT", f"/tasks/{task_id}", params=params)

    async def complete_task(self, task_id: str) -> WrikeTask:
        """Mark a task completed in Wrike."""
        return await self._single("PUT", f"/tasks/{task_id}",
                                  params={"status": "Completed"})
