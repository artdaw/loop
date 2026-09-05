"""Loop Web — read-only FastAPI + Jinja2 + HTMX dashboard.

A lightweight browser view over the same local state the CLI uses. Phase 2 ships
a read-only dashboard:

    GET /            Today's briefing: meetings, flagged emails, tasks due today
    GET /follow-ups  Email threads flagged for follow-up (from SQLite)
    GET /tasks       All open tasks (from SQLite)
    GET /search?q=   Semantic search results (ChromaDB via VectorStore)

Styling is Tailwind (CDN) and the search box uses HTMX — no JS framework.

Run with:
    uvicorn web.main:app --reload --port 8000
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from config.settings import get_settings
from core.memory import MemoryStore

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Loop", version="0.2.0")


def _memory() -> MemoryStore:
    store = MemoryStore()
    store.bootstrap()
    return store


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe."""
    settings = get_settings()
    return {"status": "ok", "environment": settings.environment}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """Today's briefing: meetings today, flagged emails, tasks due today."""
    store = _memory()
    try:
        due_today = store.get_due_today()
        overdue = store.get_overdue()
        follow_ups = store.list_open_follow_ups()
    except Exception:  # noqa: BLE001 - empty DB / first run
        due_today, overdue, follow_ups = [], [], []

    # Calendar events come from the live integrations at runtime; the read-only
    # dashboard shows what is persisted. Meetings are surfaced as reminders.
    meetings: list = []

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "today": date.today().isoformat(),
            "meetings": meetings,
            "follow_ups": follow_ups,
            "due_today": due_today,
            "overdue": overdue,
            "active": "home",
        },
    )


@app.get("/follow-ups", response_class=HTMLResponse)
def follow_ups(request: Request) -> HTMLResponse:
    """List email threads flagged for follow-up."""
    store = _memory()
    try:
        items = store.list_open_follow_ups()
    except Exception:  # noqa: BLE001
        items = []
    return templates.TemplateResponse(
        request,
        "follow_ups.html",
        {"follow_ups": items, "active": "follow-ups"},
    )


@app.get("/tasks", response_class=HTMLResponse)
def tasks(request: Request) -> HTMLResponse:
    """List all open tasks."""
    store = _memory()
    try:
        open_tasks = store.list_open_tasks()
        overdue = set(t.id for t in store.get_overdue())
    except Exception:  # noqa: BLE001
        open_tasks, overdue = [], set()
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {"tasks": open_tasks, "overdue_ids": overdue, "active": "tasks"},
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "") -> HTMLResponse:
    """Semantic search results (rendered as a fragment for HTMX)."""
    results: list = []
    error: str | None = None
    if q.strip():
        try:
            from core.vector_store import VectorStore

            results = VectorStore().semantic_search(q, n_results=8)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)

    # HTMX requests get only the results fragment; full loads get the page.
    template = "search_results.html" if request.headers.get("HX-Request") else "search.html"
    return templates.TemplateResponse(
        request,
        template,
        {"query": q, "results": results, "error": error, "active": "search"},
    )
