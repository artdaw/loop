"""Loop Web — optional FastAPI + HTMX dashboard.

A lightweight, read-only dashboard for those who prefer a browser over the
terminal. Phase 2 ships a read-only view (today's briefing, open follow-ups,
open tasks); Phase 3 makes it interactive (approve follow-ups, create tasks,
search knowledge).

Run with:
    uvicorn web.main:app --reload --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from config.settings import get_settings

app = FastAPI(title="Loop", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe."""
    settings = get_settings()
    return {"status": "ok", "environment": settings.environment}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    """Render the dashboard home page."""
    # TODO(phase2): render an HTMX template with today's briefing, open
    #               follow-ups, and open tasks pulled from the MemoryStore.
    return (
        "<!doctype html><html><head><title>Loop</title></head>"
        "<body><h1>Loop</h1>"
        "<p>Phase 1 stub — the dashboard arrives in Phase 2.</p>"
        "</body></html>"
    )
