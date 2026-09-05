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
    """Semantic search across the Obsidian knowledge base (Phase 2)."""
    typer.echo(f"(Phase 2 stub) Searching knowledge base for: {query!r} (limit {limit})")
    # TODO(phase2): call KnowledgeSpecialist.search(query, limit=limit) and print hits.


@app.command()
def ask(
    question: str = typer.Argument(..., help="Free-form question across all sources."),
) -> None:
    """Ask a free-form question across all connected sources (Phase 3)."""
    typer.echo(f"(Phase 3 stub) You asked: {question!r}")
    # TODO(phase3): route through the Orchestrator for a cross-source answer.


def main() -> None:
    """Console-script entry point (see pyproject [project.scripts])."""
    app()


if __name__ == "__main__":
    main()
