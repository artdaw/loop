"""Loop Web — interactive FastAPI + Jinja2 + HTMX dashboard (v2).

A lightweight browser view over the same local state the CLI uses. Phase 3
upgrades the read-only dashboard into an interactive one:

    GET  /                         Today's briefing (meetings, emails, tasks)
    GET  /follow-ups               Email threads flagged for follow-up
    POST /follow-ups/{id}/approve  Mark a follow-up approved (sent)
    POST /follow-ups/{id}/snooze   Snooze a follow-up for N hours
    POST /follow-ups/{id}/ignore   Dismiss a follow-up
    GET  /tasks                    All open tasks
    POST /tasks                    Create a task
    POST /tasks/{id}/complete      Mark a task complete
    GET  /search?q=                Semantic search results (ChromaDB)
    GET  /autonomy                 Autonomy levels per action (Phase 4)
    POST /autonomy/{action}        Change one action's autonomy level
    GET  /metrics                  Local-vs-cloud, task, and follow-up metrics

Styling is Tailwind (CDN); search uses HTMX and the action buttons post back
to the server — no JS framework.

Run with:
    uvicorn web.main:app --reload --port 8000
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config.settings import get_settings
from core.memory import MemoryStore

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Loop", version="0.3.0")

# Test seam: when set, every request uses this store/settings instead of
# building one from the ambient environment. Production never sets these.
_store_override: MemoryStore | None = None
_settings_override = None


def _memory() -> MemoryStore:
    if _store_override is not None:
        return _store_override
    store = MemoryStore()
    store.bootstrap()
    return store


def _settings():
    return _settings_override or get_settings()


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


# --------------------------------------------------------------------------- #
# Follow-up actions (Phase 3)
# --------------------------------------------------------------------------- #
@app.post("/follow-ups/{follow_up_id}/approve")
def approve_follow_up(follow_up_id: int) -> RedirectResponse:
    """Approve a follow-up (mark it sent).

    Actual dispatch runs through the Gmail/Outlook connectors when wired; the
    dashboard records the approval so the item leaves the queue either way.
    """
    store = _memory()
    try:
        store.set_follow_up_status(follow_up_id, "sent")
    except Exception:  # noqa: BLE001 - never 500 the dashboard
        pass
    return RedirectResponse(url="/follow-ups", status_code=303)


@app.post("/follow-ups/{follow_up_id}/snooze")
def snooze_follow_up_route(follow_up_id: int,
                           hours: int = Form(24)) -> RedirectResponse:
    """Snooze a follow-up for a number of hours (hidden until then)."""
    store = _memory()
    try:
        store.snooze_follow_up(follow_up_id, hours)
    except Exception:  # noqa: BLE001
        pass
    return RedirectResponse(url="/follow-ups", status_code=303)


@app.post("/follow-ups/{follow_up_id}/ignore")
def ignore_follow_up(follow_up_id: int) -> RedirectResponse:
    """Dismiss a follow-up (mark it ignored)."""
    store = _memory()
    try:
        store.set_follow_up_status(follow_up_id, "ignored")
    except Exception:  # noqa: BLE001
        pass
    return RedirectResponse(url="/follow-ups", status_code=303)


# --------------------------------------------------------------------------- #
# Task actions (Phase 3)
# --------------------------------------------------------------------------- #
@app.post("/tasks")
def create_task(description: str = Form(...),
                priority: str = Form("normal")) -> RedirectResponse:
    """Create a new task from the dashboard form."""
    store = _memory()
    text = description.strip()
    if text:
        try:
            store.add_task(text, priority=priority, source="web")
        except Exception:  # noqa: BLE001
            pass
    return RedirectResponse(url="/tasks", status_code=303)


@app.post("/tasks/{task_id}/complete")
def complete_task_route(task_id: int) -> RedirectResponse:
    """Mark a task complete."""
    store = _memory()
    try:
        store.complete_task(task_id)
    except Exception:  # noqa: BLE001
        pass
    return RedirectResponse(url="/tasks", status_code=303)


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


# --------------------------------------------------------------------------- #
# Autonomy (Phase 4)
# --------------------------------------------------------------------------- #
@app.get("/autonomy", response_class=HTMLResponse)
def autonomy_page(request: Request) -> HTMLResponse:
    """Show the autonomy level for every action type."""
    from core.autonomy import AutonomyGate, AutonomyLevel

    gate = AutonomyGate(_settings(), _memory())
    rows = []
    for action, decision in gate.levels_table().items():
        rows.append({
            "action": action.value,
            "level": decision.level.label,
            "ceiling": gate.ceiling_for(action).label,
            "capped": decision.capped,
            "reason": decision.reason,
            "requires_approval": decision.requires_approval,
            "allowed": decision.allowed,
        })

    try:
        audit = gate.recent_audit(limit=15)
    except Exception:  # noqa: BLE001 - never 500 the dashboard
        audit = []

    return templates.TemplateResponse(
        request,
        "autonomy.html",
        {
            "rows": rows,
            "levels": [level.label for level in AutonomyLevel],
            "audit": audit,
            "active": "autonomy",
        },
    )


@app.post("/autonomy/{action}")
def set_autonomy(action: str, level: str = Form(...)) -> RedirectResponse:
    """Change one action's autonomy level, then redirect back to the listing."""
    from core.autonomy import ActionType, AutonomyGate

    try:
        gate = AutonomyGate(_settings(), _memory())
        gate.set_level(ActionType(action), level)
    except (ValueError, KeyError):
        # Unknown action or unparseable level: leave the setting untouched.
        pass
    except Exception:  # noqa: BLE001 - never 500 the dashboard
        pass
    return RedirectResponse(url="/autonomy", status_code=303)
