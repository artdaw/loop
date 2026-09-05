"""Loop CLI — terminal interface built with Typer.

The terminal is the power-user surface: check status, print the morning
briefing, snooze reminders, search the knowledge base, and ask free-form
questions.

Run with:
    python -m cli.main --help
    loop --help          # once installed via pyproject entry point

Phase 1 commands: status, briefing, snooze.
Phase 2 command:  find (semantic search).
Phase 3 command:  ask  (free-form query across all sources).
All commands are wired as stubs so the CLI runs end-to-end today.
"""

from __future__ import annotations

import typer

from config.settings import get_settings

app = typer.Typer(
    name="loop",
    help="Loop — your terminal-first, AI-driven personal command centre.",
    add_completion=False,
)


@app.command()
def status() -> None:
    """Show assistant health: connected integrations, scheduler, pending items."""
    settings = get_settings()
    typer.echo(f"Loop status — environment: {settings.environment}")
    typer.echo("  (Phase 1 stub) integrations: not yet wired")
    typer.echo("  (Phase 1 stub) scheduler:    not running")
    typer.echo("  (Phase 1 stub) open follow-ups / reminders: n/a")
    # TODO(phase1): report real integration connectivity, scheduler state,
    #               and counts of open follow-ups / due reminders from memory.


@app.command()
def briefing() -> None:
    """Print today's morning briefing (meetings + flagged emails)."""
    typer.echo("(Phase 1 stub) Morning briefing will list today's meetings")
    typer.echo("and flagged email threads once integrations are wired.")
    # TODO(phase1): call CalendarSpecialist.morning_briefing() and print it.


@app.command()
def snooze(
    item: str = typer.Argument(..., help="Reminder/follow-up id to snooze."),
    hours: int = typer.Option(24, "--hours", "-h", help="Snooze duration in hours."),
) -> None:
    """Snooze a reminder or follow-up for a number of hours."""
    typer.echo(f"(Phase 1 stub) Snoozing '{item}' for {hours}h.")
    # TODO(phase1): update the reminder/follow-up fire_at in the MemoryStore.


@app.command()
def find(
    query: str = typer.Argument(..., help="Search text for the knowledge base."),
    limit: int = typer.Option(5, "--limit", "-n", help="Max results to return."),
) -> None:
    """Semantic search across the Obsidian knowledge base + email summaries."""
    from core.vector_store import VectorStore

    try:
        store = VectorStore()
        results = store.semantic_search(query, n_results=limit)
    except Exception as exc:  # noqa: BLE001 - surface a friendly message
        typer.secho(f"Search failed: {exc}", fg=typer.colors.RED)
        typer.echo("Is Ollama running (for embeddings) and has the vault been indexed?")
        raise typer.Exit(code=1) from exc

    if not results:
        typer.echo(f"No results for {query!r}.")
        return

    for i, hit in enumerate(results, start=1):
        meta = hit.metadata or {}
        title = meta.get("title") or meta.get("subject") or "(untitled)"
        excerpt = _excerpt(hit.document, query)
        if hit.kind == "email":
            sender = meta.get("sender", "unknown")
            date = meta.get("date", "")
            typer.echo(f'[{i}] Email: "{title}" from {sender} ({date})')
        else:
            date = meta.get("date", "")
            date_str = f" ({date})" if date else ""
            typer.echo(f'[{i}] Note: "{title}"{date_str}')
        typer.echo(f'    "...{excerpt}..."')


def _excerpt(document: str, query: str, *, width: int = 160) -> str:
    """Return a short excerpt from ``document``, centred on the query if found."""
    text = " ".join((document or "").split())
    if not text:
        return ""
    lowered = text.lower()
    pos = lowered.find(query.lower().split()[0]) if query.split() else -1
    if pos == -1:
        return text[:width]
    start = max(0, pos - width // 2)
    return text[start:start + width]


@app.command()
def ask(
    question: str = typer.Argument(..., help="Free-form question across all sources."),
    local: bool = typer.Option(
        False, "--local", "-l",
        help="Force local-only inference (never use the cloud LLM).",
    ),
    session: str = typer.Option(
        "cli", "--session", "-s",
        help="Conversation session id (keeps context across questions).",
    ),
) -> None:
    """Ask a free-form question across all connected sources (Phase 3).

    Retrieves relevant notes/emails, blends in recent conversation history,
    and answers via the privacy-gated LLM router (local-first, with an
    Anthropic fallback for non-private work). Private sources always stay
    local; ``--local`` forces local-only regardless of source.
    """
    import asyncio

    from core.orchestrator import Orchestrator

    async def _run() -> None:
        orchestrator = Orchestrator()
        response = await orchestrator.ask(
            question,
            session_id=session,
            local_only=True if local else None,
        )
        typer.echo(response.text)
        if response.actions:
            meta = response.actions[0]
            typer.secho(
                f"\n[backend: {meta.get('backend')} | "
                f"local_only: {meta.get('local_only')} | "
                f"sources: {meta.get('sources')}]",
                fg=typer.colors.BRIGHT_BLACK,
            )

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - surface a friendly message
        typer.secho(f"Ask failed: {exc}", fg=typer.colors.RED)
        typer.echo("Is Ollama running (for local inference and embeddings)?")
        raise typer.Exit(code=1) from exc


def main() -> None:
    """Console-script entry point (see pyproject [project.scripts])."""
    app()


if __name__ == "__main__":
    main()
