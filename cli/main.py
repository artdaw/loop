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
Phase 4 commands: autonomy, autonomy-set, review, sync, metrics, note-from-audio.
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
    """Show what is wired up, what is not, and what is waiting."""
    from core.health import HealthChecker

    typer.echo(HealthChecker().check().render())


@app.command()
def briefing() -> None:
    """Print today's briefing: meetings, flagged threads, and tasks due."""
    from specialists.briefing import DailyBriefing

    typer.echo(DailyBriefing(memory=_store(), calendar=_calendar()).compose())


@app.command()
def telegram() -> None:
    """Run the private Telegram bot using long polling."""
    from integrations.telegram_bot import TelegramBot

    typer.echo("Starting Loop's Telegram bot. Press Ctrl+C to stop.")
    try:
        TelegramBot(memory=_store(), calendar=_calendar()).run_polling()
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


def _calendar():
    """Build a CalendarSpecialist wired to whichever calendars are configured.

    Returns ``None`` when neither Google nor Outlook has credentials, which the
    briefing renders as "calendars not connected".

    The connectors must actually be passed in: a CalendarSpecialist with no
    connectors returns an empty list, which the briefing would report as "nothing
    in the diary" — indistinguishable from a genuinely free day. Wiring the real
    clients means their Phase 1 ``NotImplementedError`` surfaces instead, and the
    briefing says the connectors are not implemented yet.
    """
    from pathlib import Path

    settings = get_settings()
    google = outlook = None

    if Path(settings.gmail_credentials_path).expanduser().is_file():
        from integrations.google_calendar import GoogleCalendarClient

        google = GoogleCalendarClient(settings)

    # Only a client id is needed: Loop signs in with the delegated device-code
    # flow, which uses a public client and therefore carries no secret.
    if settings.outlook_client_id.strip():
        from integrations.outlook_calendar import OutlookCalendarClient

        outlook = OutlookCalendarClient(settings)

    if google is None and outlook is None:
        return None

    from specialists.calendar import CalendarSpecialist

    return CalendarSpecialist(settings, google_calendar=google,
                              outlook_calendar=outlook)


@app.command()
def snooze(
    item: int = typer.Argument(..., help="Follow-up id to snooze (see `loop briefing`)."),
    hours: int = typer.Option(24, "--hours", "-h", help="Snooze duration in hours."),
) -> None:
    """Hide a follow-up from the briefing for a number of hours."""
    store = _store()
    follow_up = store.get_follow_up(item)
    if follow_up is None:
        typer.secho(f"No follow-up with id {item}.", fg=typer.colors.RED)
        typer.echo("Run `loop briefing` to see the ids of open threads.")
        raise typer.Exit(code=1)

    if store.snooze_follow_up(item, hours):
        subject = follow_up.subject or follow_up.thread_id
        typer.secho(f"Snoozed \"{subject}\" for {hours}h.", fg=typer.colors.GREEN)
    else:
        typer.secho(f"Could not snooze follow-up {item}.", fg=typer.colors.RED)
        raise typer.Exit(code=1)


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


# --------------------------------------------------------------------------- #
# Phase 4 commands
# --------------------------------------------------------------------------- #
@app.command()
def autonomy() -> None:
    """Show how autonomously Loop may act, per action type."""
    from core.autonomy import AutonomyGate

    gate = AutonomyGate()
    typer.echo("Autonomy levels (observe < suggest < approve < act)\n")
    typer.echo(f"  {'ACTION':<16} {'LEVEL':<10} {'CEILING':<10} STATUS")
    for action, decision in gate.levels_table().items():
        if decision.capped:
            status = "capped by ceiling"
            colour = typer.colors.YELLOW
        elif not decision.allowed:
            status = "records only"
            colour = typer.colors.BRIGHT_BLACK
        elif decision.requires_approval:
            status = "needs approval"
            colour = typer.colors.BLUE
        else:
            status = "autonomous"
            colour = typer.colors.GREEN
        typer.secho(
            f"  {action.value:<16} {decision.level.label:<10} "
            f"{gate.ceiling_for(action).label:<10} {status}",
            fg=colour,
        )
    typer.echo("\nChange one with:  loop autonomy-set <action> <level>")


@app.command("autonomy-set")
def autonomy_set(
    action: str = typer.Argument(..., help="Action type, or 'default' for the global level."),
    level: str = typer.Argument(..., help="observe | suggest | approve | act"),
) -> None:
    """Set the autonomy level for one action (or the global default)."""
    from core.autonomy import ActionType, AutonomyGate

    gate = AutonomyGate()
    target = None
    if action.lower() != "default":
        try:
            target = ActionType(action.lower())
        except ValueError:
            valid = ", ".join(a.value for a in ActionType)
            typer.secho(f"Unknown action {action!r}. Valid: {valid}, default",
                        fg=typer.colors.RED)
            raise typer.Exit(code=1) from None
    try:
        gate.set_level(target, level)
    except ValueError:
        typer.secho(f"Unknown level {level!r}. Valid: observe, suggest, approve, act",
                    fg=typer.colors.RED)
        raise typer.Exit(code=1) from None

    name = "default" if target is None else target.value
    typer.secho(f"Set {name} autonomy to {level.lower()}.", fg=typer.colors.GREEN)

    if target is not None:
        decision = gate.decide(target)
        if decision.capped:
            typer.secho(
                f"Note: capped at {decision.level.label} by the {name} ceiling. "
                f"Raise MAX_AUTONOMY_EMAIL_SEND in .env to lift it.",
                fg=typer.colors.YELLOW,
            )


@app.command()
def sync(
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n",
        help="Report what would change without writing anything.",
    ),
) -> None:
    """Sync tasks with Wrike (remote wins; locally-captured tasks are pushed)."""
    import asyncio

    from core.wrike_sync import WrikeSync

    try:
        report = asyncio.run(WrikeSync(memory=_store()).sync(dry_run=dry_run))
    except Exception as exc:  # noqa: BLE001 - surface a friendly message
        typer.secho(f"Sync failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    if not report.configured:
        typer.secho(report.summary(), fg=typer.colors.YELLOW)
        raise typer.Exit(code=0)

    typer.echo(report.summary())
    for error in report.errors:
        typer.secho(f"  ! {error}", fg=typer.colors.RED)


@app.command()
def review(
    weeks_ago: int = typer.Option(
        0, "--weeks-ago", "-w",
        help="0 = this week, 1 = last week, and so on.",
    ),
    no_narrative: bool = typer.Option(
        False, "--no-narrative",
        help="Skip the LLM reflection and print only the statistics.",
    ),
) -> None:
    """Print the weekly review: what you finished, what slipped, what it cost."""
    from core.llm_router import LLMRouter
    from specialists.review import WeeklyReview

    store = _store()
    router = None if no_narrative else LLMRouter(memory=store)
    weekly = WeeklyReview(memory=store, router=router)

    try:
        stats = weekly.collect(weeks_ago=weeks_ago)
        typer.echo(weekly.compose(stats, with_narrative=not no_narrative))
    except Exception as exc:  # noqa: BLE001 - surface a friendly message
        typer.secho(f"Review failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


@app.command("note-from-audio")
def note_from_audio(
    audio: str = typer.Argument(..., help="Path to an audio file (ogg, mp3, wav, m4a)."),
    write: bool = typer.Option(True, "--write/--dry-run",
                               help="Write the note to the vault, or just print it."),
) -> None:
    """Transcribe a voice memo locally and file it as an Obsidian note.

    Audio is processed entirely on this machine: transcription runs a local
    Whisper model and the note is formatted by the local LLM only. Nothing is
    sent to a cloud provider.
    """
    from pathlib import Path

    from specialists.knowledge import KnowledgeSpecialist

    path = Path(audio).expanduser()
    if not path.exists():
        typer.secho(f"No such file: {path}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    try:
        draft = KnowledgeSpecialist().note_from_audio(path, source="cli", write=write)
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # noqa: BLE001 - surface a friendly message
        typer.secho(f"Could not process the recording: {exc}", fg=typer.colors.RED)
        typer.echo("Is Ollama running? Voice notes are local-only, so there is no "
                   "cloud fallback by design.")
        raise typer.Exit(code=1) from exc

    if write:
        typer.secho(f"Filed note: {draft.title}", fg=typer.colors.GREEN)
    else:
        typer.echo(draft.to_markdown())


@app.command()
def metrics(
    days: int = typer.Option(30, "--days", "-d", help="Window size in days."),
) -> None:
    """Show what Loop has been doing, and how much of it stayed local."""
    from core.metrics import MetricsCollector

    summary = MetricsCollector(_store()).summary(days=max(1, days))

    if not summary.has_data:
        typer.echo("No data yet — metrics appear once Loop has answered a "
                   "question, tracked a task, or flagged an email.")
        raise typer.Exit(code=0)

    typer.echo(f"Loop metrics — last {summary.days} days\n")

    llm = summary.llm
    typer.secho(f"  {llm.local_percent}% of {llm.total} AI requests served locally",
                fg=typer.colors.GREEN, bold=True)
    typer.echo(f"    local {llm.local_count} | cloud {llm.cloud_count} "
               f"| private {llm.private_count}")
    typer.echo(f"    latency: avg {llm.avg_latency_ms}ms, p95 {llm.p95_latency_ms}ms\n")

    tasks = summary.tasks
    typer.echo(f"  Tasks: {tasks.completed}/{tasks.created} completed "
               f"({tasks.completion_percent}%), {tasks.open} open")
    if tasks.overdue:
        typer.secho(f"    {tasks.overdue} overdue", fg=typer.colors.RED)

    follow_ups = summary.follow_ups
    typer.echo(f"  Follow-ups: {follow_ups.resolved} resolved, "
               f"{follow_ups.ignored} ignored, {follow_ups.pending} pending")

    if summary.autonomy.total:
        typer.echo(f"  Autonomy: {summary.autonomy.executed}/{summary.autonomy.total} "
                   f"actions ran unattended")


def _store():
    """Build a bootstrapped MemoryStore for the CLI."""
    from core.memory import MemoryStore

    store = MemoryStore()
    store.bootstrap()
    return store


def main() -> None:
    """Console-script entry point (see pyproject [project.scripts])."""
    app()


if __name__ == "__main__":
    main()
