"""The vNext CLI (interfaces §3), installed as `loop-next` (see pyproject).

Every command below does the same three things: build the application from
settings, call exactly one method on a shared service, and print the result.
There is no second implementation of "what a task is" hiding in a command
function — that would be the CLI quietly diverging from what the HTTP API and
the Telegram bot do with the same request.

This intentionally does not attempt parity with the legacy `cli/main.py`
(autonomy levels, Wrike sync, voice notes, metrics) yet. `loop` still points
at that CLI; this ships as `loop-next` so it is genuinely installable and
callable without displacing the existing surface before its replacement
has matching behaviour (main spec §10, agent-stack migration note).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import signal
from contextlib import suppress
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from loop.agents.coordinator import Coordinator
from loop.ai.budget import RootBudget
from loop.app import Application, build_application
from loop.core.errors import LoopError
from loop.core.privacy import PrivacyLabel
from loop.db.migrations import applied_revisions
from loop.ops.backup import BackupError, create_backup, restore_backup, verify_backup
from loop.runtime.authority import AuthorityContext
from loop.runtime.checkpointer import open_production_checkpointer
from loop.runtime.coordinator_worker import CoordinatorJobWorker
from loop.runtime.routine_dispatch import ROUTINE_SUBJECT, next_local_fire
from loop.runtime.routines import parse_routine
from loop.runtime.runs import checkpoint_path
from loop.services.feedback import summarise
from loop.services.knowledge import KnowledgeService

app = typer.Typer(name="loop-next", add_completion=False,
                  help="Loop vNext — the durable coordinated stack.")

task_app = typer.Typer(help="Durable commitments.")
capability_app = typer.Typer(help="Capability pack discovery and enablement.")
run_app = typer.Typer(help="The durable service sweep.")
routine_app = typer.Typer(help="Recurring routines the owner has approved.")
vault_app = typer.Typer(help="The knowledge vault: capture, compile, search.")
learn_app = typer.Typer(help="What Loop has learned, and what it proposes.")
app.add_typer(task_app, name="task")
app.add_typer(capability_app, name="capability")
app.add_typer(run_app, name="run")
app.add_typer(routine_app, name="routine")
app.add_typer(vault_app, name="vault")
app.add_typer(learn_app, name="learning")


def _app() -> Application:
    return build_application()


def _fail(exc: LoopError) -> None:
    typer.echo(f"error: {exc.message}", err=True)
    raise typer.Exit(code=exc.exit_code)


def _database_path(application: Application) -> Path:
    prefix = "sqlite:///"
    url = application.settings.database_url
    if not url.startswith(prefix) or url == "sqlite:///:memory:":
        raise BackupError("Backup requires a file-backed SQLite DATABASE_URL.")
    return Path(url.removeprefix(prefix)).expanduser().resolve()


# --------------------------------------------------------------------------- #
# Status — must work with nothing configured (I6)
# --------------------------------------------------------------------------- #
@app.command()
def status() -> None:
    """What is due, what is enabled, and what the service is waiting on."""
    application = _app()
    pending = application.service.pending_summary()
    enabled = sorted(application.registry.enabled_operations())
    limitations = application.model_gateway.describe_limitations()

    typer.echo(f"triggers enabled: {pending['triggers']}")
    typer.echo(f"jobs queued/retrying: {pending['jobs']}")
    typer.echo(f"outbox pending: {pending['outbox']}")
    if pending["unknown"]:
        typer.echo(f"outbox with unknown delivery: {pending['unknown']}")
    typer.echo(f"enabled capabilities: {', '.join(enabled) or 'none'}")
    for limitation in limitations:
        typer.echo(f"note: {limitation}")


@app.command("remind")
def remind(title: str, at: str = typer.Option(
        ..., "--at", help="ISO local or zoned time, for example 2026-09-08T09:00."),
        timezone: str | None = typer.Option(None, "--timezone")) -> None:
    """Persist a task and its exact reminder before confirming it."""
    application = _app()
    zone_name = timezone or application.settings.timezone
    try:
        zone = ZoneInfo(zone_name)
        parsed = dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
        instant = parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError) as exc:
        typer.echo(f"error: invalid reminder time {at!r}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    local = instant.astimezone(zone)
    try:
        result = application.reminders.schedule(
            title, instant=instant, timezone=zone_name,
            original_local=local.isoformat(timespec="minutes"))
    except (LoopError, ValueError) as exc:
        if isinstance(exc, LoopError):
            _fail(exc)
            return
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"{result.task.id}  scheduled for "
               f"{local.isoformat(timespec='minutes')}")


@app.command("backup")
def backup(output: Annotated[Path, typer.Option("--output")]) -> None:
    """Snapshot domain state, graph checkpoints, and the selected vault."""
    application = _app()
    try:
        pending = application.service.pending_summary()
        revisions = applied_revisions(application.sessions.kw["bind"])
        manifest = create_backup(
            database=_database_path(application),
            checkpoints=checkpoint_path(application.settings.data_path),
            vault=(application.settings.vault_root
                   or application.settings.data_path / ".no-vault-configured"),
            journal=None, destination=output,
            created_at=int(application.clock.now().timestamp()),
            schema_revision=sorted(revisions)[-1] if revisions else "",
            pending_jobs=int(pending["jobs"]),
            pending_outbox=int(pending["outbox"]))
    except (BackupError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"backup written to {output} ({len(manifest.parts)} parts)")


@app.command("restore")
def restore(source: Annotated[Path, typer.Option("--from")],
            target: Annotated[Path, typer.Option("--target")],
            apply: Annotated[bool, typer.Option("--apply")] = False) -> None:
    """Verify a backup; apply only into an explicit, empty target."""
    try:
        problems = verify_backup(source)
        if problems:
            raise BackupError("; ".join(problems))
        if not apply:
            typer.echo(f"verified; would restore into {target}")
            return
        result = restore_backup(source, target=target)
    except (BackupError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"restored database to {result.database}")
    if result.checkpoints is not None:
        typer.echo(f"restored checkpoints to {result.checkpoints}")
    if result.vault is not None:
        typer.echo(f"restored vault to {result.vault}")


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
@task_app.command("add")
def task_add(title: str, due_date: str | None = None) -> None:
    """Create a task. A task with no date is normal, not an error."""
    application = _app()
    try:
        task = application.tasks.create(title, due_date=due_date)
    except LoopError as exc:
        _fail(exc)
        return
    typer.echo(f"{task.id}  {task.title}  [{task.status}]")


@task_app.command("list")
def task_list(status: str | None = None) -> None:
    application = _app()
    for task in application.tasks.list(status=status):
        due = f" due {task.due_date}" if task.due_date else ""
        typer.echo(f"{task.id}  {task.title}  [{task.status}]{due}")


@task_app.command("complete")
def task_complete(task_id: str, expected_version: int) -> None:
    """Complete a task. `expected_version` is required — never a blind write."""
    application = _app()
    try:
        task = application.reminders.complete(task_id,
                                              expected_version=expected_version)
    except LoopError as exc:
        _fail(exc)
        return
    typer.echo(f"{task.id}  done")


# --------------------------------------------------------------------------- #
# Capability management (M4: "ship validated packs and capability management")
# --------------------------------------------------------------------------- #
@capability_app.command("list")
def capability_list() -> None:
    application = _app()
    for entry in application.registry.entries():
        state = "enabled" if entry.enabled else entry.availability.value
        typer.echo(f"{entry.manifest.pack_key}  [{state}]  "
                   f"{entry.manifest.title}")


@capability_app.command("enable")
def capability_enable(pack_id: str, version: str) -> None:
    application = _app()
    try:
        application.registry.enable(pack_id, version)
    except LoopError as exc:
        _fail(exc)
        return
    typer.echo(f"{pack_id}@{version} enabled")


@capability_app.command("disable")
def capability_disable(pack_id: str) -> None:
    application = _app()
    affected = application.registry.disable(pack_id)
    typer.echo(f"disabled: {', '.join(affected) or 'nothing was enabled'}")


# --------------------------------------------------------------------------- #
# The durable sweep
# --------------------------------------------------------------------------- #
@run_app.command("once")
def run_once(sweep_only: bool = typer.Option(
        False, "--sweep-only",
        help="Fire triggers and dispatch the outbox, but run no queued work.")
        ) -> None:
    """Process due work to quiescence and return (interfaces §3).

    Three phases, because a tick that only fires triggers and dispatches an
    outbox is not the complete service: the sweep queues durable work, the
    worker runs it, and a second sweep delivers whatever that produced. Run
    with `--sweep-only` for the strictly deterministic half — that phase fires
    triggers and dispatches the outbox and reaches no model at all (I6), while
    executing a routine's steps runs real capabilities and may.
    """
    application = _app()
    reports = application.service.run_once()
    fired = sum(r.triggers_fired for r in reports)

    # Conditions are evaluated in the deterministic half: a predicate over
    # stored observations reaches no model, so it belongs with the sweep
    # rather than with the work the sweep queues.
    conditions = [] if sweep_only else application.conditions.sweep()
    outcomes = [] if sweep_only else application.routine_worker.run_due()
    if outcomes or any(outcome.fired for outcome in conditions):
        reports.extend(application.service.run_once())

    sent = sum(r.notifications_sent for r in reports)
    failed = sum(r.notifications_failed for r in reports)
    typer.echo(f"{len(reports)} sweep(s); {fired} trigger(s) fired; "
              f"{len(outcomes)} routine(s) run; {sent} notification(s) sent"
              + (f"; {failed} undeliverable" if failed else ""))
    for condition in conditions:
        if condition.fired:
            typer.echo(f"  {condition.slug}: condition became true")
        elif condition.missing:
            typer.echo(f"  {condition.slug}: unknown — no fresh observation "
                       f"for {', '.join(condition.missing)}")
    for outcome in outcomes:
        typer.echo(f"  {outcome.slug}: {outcome.decision or outcome.state}"
                   + (f" — {outcome.reason}" if outcome.reason else "")
                   + (f" [{outcome.error}]" if outcome.error else ""))


@run_app.command("daemon")
def run_daemon(interval_seconds: float = typer.Option(
        1.0, "--interval-seconds", min=0.05)) -> None:
    """Run the durable scheduler and workers until SIGTERM or Ctrl-C."""
    asyncio.run(_run_daemon(interval_seconds))


async def _run_daemon(interval_seconds: float) -> None:
    application = _app()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):  # pragma: no cover - Windows loop
            loop.add_signal_handler(signum, stopping.set)

    async with open_production_checkpointer(application.settings.data_path) as saver:
        coordinator = Coordinator(
            invoker=application.invoker, roles=application.roles,
            runs=application.runs, approvals=application.operations,
            checkpointer=saver)
        coordinator_worker = CoordinatorJobWorker(
            jobs=application.jobs, coordinator=coordinator)
        try:
            while not stopping.is_set():
                application.service.run_once()
                application.conditions.sweep()
                application.routine_worker.run_due()
                while await coordinator_worker.run_one() is not None:
                    pass
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=interval_seconds)
                except TimeoutError:
                    continue
        finally:
            application.service.leader.release()


# --------------------------------------------------------------------------- #
# Routines
# --------------------------------------------------------------------------- #
@routine_app.command("list")
def routine_list() -> None:
    """Every saved routine, its status, and when it next runs."""
    application = _app()
    routines = application.routines.list_routines()
    if not routines:
        typer.echo("no routines saved")
        return
    for routine in routines:
        triggers = application.triggers.for_subject(ROUTINE_SUBJECT, routine.slug)
        when = next_local_fire(triggers[0]) if triggers else None
        line = f"{routine.slug}\t{routine.status.value}\t{routine.title}"
        typer.echo(line + (f"\tnext {when.isoformat()}" if when else "\tunscheduled"))
        for question in routine.blocking_questions:
            typer.echo(f"  needs an answer: {question}")


@routine_app.command("add")
def routine_add(document: Path) -> None:
    """Save a routine document. Saving is not activating (P02, P28).

    A parsed routine with unmet requirements is still saved and listed, with
    its questions — losing the owner's request because one detail was missing
    is worse than holding it. It simply cannot be activated until answered.
    """
    application = _app()
    try:
        text = document.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"error: cannot read {document}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    routine, problems = parse_routine(
        text, known_capabilities=application.known_capabilities())
    if problems or routine is None:
        for problem in problems:
            typer.echo(f"error: {problem}", err=True)
        raise typer.Exit(code=2)

    application.routines.save(routine)
    typer.echo(f"{routine.slug} saved ({routine.status.value})")
    for missing in routine.missing:
        typer.echo(f"  needs {missing.kind} {missing.name}"
                   + (f": {missing.question}" if missing.question else ""))


@routine_app.command("activate")
def routine_activate(slug: str, event_id: str = typer.Option(
        ..., "--event-id", help="The owner request that authorises this.")
        ) -> None:
    """Activate a routine and schedule it.

    `--event-id` is required rather than defaulted: activation is authority
    (P01), and a routine that activated itself with a generated identifier
    would have no record of who asked for it.
    """
    application = _app()
    try:
        routine = application.routine_scheduler.activate(
            slug, activation_event_id=event_id)
    except LoopError as exc:
        _fail(exc)
        return
    triggers = application.triggers.for_subject(ROUTINE_SUBJECT, routine.slug)
    when = next_local_fire(triggers[0]) if triggers else None
    typer.echo(f"{routine.slug} active"
               + (f"; next run {when.isoformat()}" if when
                  else "; no clock schedule (event- or condition-triggered)"))


@routine_app.command("pause")
def routine_pause(slug: str) -> None:
    """Pause a routine and stop its schedule firing."""
    application = _app()
    try:
        routine = application.routine_scheduler.pause(slug)
    except LoopError as exc:
        _fail(exc)
        return
    typer.echo(f"{routine.slug} {routine.status.value}")


# --------------------------------------------------------------------------- #
# The vault (M5)
# --------------------------------------------------------------------------- #
def _knowledge(application: Application) -> KnowledgeService:
    if application.knowledge is None:
        typer.echo("error: no vault is configured. Set OBSIDIAN_VAULT_PATH.",
                   err=True)
        raise typer.Exit(code=4)
    return application.knowledge


@vault_app.command("capture")
def vault_capture(body: str,
                  title: str | None = typer.Option(None, "--title"),
                  kind: str = typer.Option("note", "--kind"),
                  origin: str = typer.Option("owner", "--origin")) -> None:
    """Preserve exactly what was said, and register it.

    The wording of the reply is the result, not a fixed string: "saved to your
    vault" requires both the file and the ledger row (vault §5).
    """
    application = _app()
    knowledge = _knowledge(application)
    try:
        result = knowledge.capture(body, title=title, source_kind=kind,
                                   origin=origin)
    except LoopError as exc:
        _fail(exc)
        return
    typer.echo(result.message)


@vault_app.command("compile")
def vault_compile(source: str | None = typer.Argument(None),
                  limit: int = typer.Option(10, "--limit")) -> None:
    """Compile one registered source, or the pending backlog."""
    application = _app()
    knowledge = _knowledge(application)
    try:
        reports = ([knowledge.compile_source(source)] if source
                   else knowledge.compile_pending(limit=limit))
    except LoopError as exc:
        _fail(exc)
        return

    if not reports:
        typer.echo("nothing pending")
        return
    for report in reports:
        typer.echo(f"{report.source_path}: {report.outcome.value}"
                   + (f" → {', '.join(report.pages)}" if report.pages else "")
                   + (f" — {report.reason}" if report.reason else ""))
        for rejected in report.claims_rejected:
            typer.echo(f"  rejected: {rejected}")


@vault_app.command("ask")
def vault_ask(question: str) -> None:
    """Answer from compiled knowledge, citing what it rests on."""
    application = _app()
    knowledge = _knowledge(application)
    answer = knowledge.answer(question)
    typer.echo(answer.text)
    if answer.knowledge_gap:
        raise typer.Exit(code=0)


@vault_app.command("reindex")
def vault_reindex() -> None:
    """Index the vault as it is on disk, including notes Loop never wrote."""
    application = _app()
    knowledge = _knowledge(application)
    typer.echo(f"{knowledge.reindex()} document(s) indexed")


# --------------------------------------------------------------------------- #
# Learning (M6)
# --------------------------------------------------------------------------- #
@learn_app.command("review")
def learning_review() -> None:
    """Run the timing learner and print what it would ask.

    Printing the question is the whole output: nothing here applies anything.
    `learning confirm` is a separate, explicit command because vault §9 says a
    proposal is never auto-applied, and a review that silently changed a
    schedule would make that sentence untrue.
    """
    application = _app()
    report = summarise(application.feedback.review_all())
    if not report.proposals and not report.quiet:
        typer.echo("no feedback recorded yet")
        return
    for outcome in report.proposals:
        typer.echo(f"{outcome.subject_ref}\t{outcome.preference_id}")
        typer.echo(f"  {outcome.question}")
        typer.echo("  confirm with: loop-next learning confirm "
                   f"{outcome.preference_id}")
    for outcome in report.quiet:
        typer.echo(f"{outcome.subject_ref}\tno proposal — {outcome.reason}")


@learn_app.command("confirm")
def learning_confirm(preference_id: str) -> None:
    """Accept a proposal and move the routine it was about."""
    application = _app()
    try:
        record, applied = application.feedback.confirm(preference_id)
    except LoopError as exc:
        _fail(exc)
        return
    typer.echo(f"{record.key} confirmed; {applied}")


@learn_app.command("forget")
def learning_forget(subject: str) -> None:
    """Remove a learned record, its vault copy and the evidence behind it."""
    application = _app()
    result = application.feedback.forget(subject)
    typer.echo(f"forgot {len(result['removed_records'])} record(s), "
               f"{len(result['removed_documents'])} document(s), "
               f"{result['removed_feedback']} feedback event(s)")


@learn_app.command("snooze")
def learning_snooze(subject: str, minutes: int,
                    event_id: str = typer.Option(..., "--event-id")) -> None:
    """Record that the owner pushed a notification later.

    `--event-id` is required so the same action arriving twice — a redelivered
    update, a replayed job — is one observation rather than two.
    """
    application = _app()
    try:
        recorded = application.feedback.record_snooze(
            subject, event_id=event_id, shift_minutes=minutes)
    except LoopError as exc:
        _fail(exc)
        return
    typer.echo("recorded" if recorded else "already recorded")


@app.command("say")
def say(message: str) -> None:
    """Send Loop an ordinary sentence and act on what it means (T04-T07).

    The same path the Telegram bot uses for non-command text, so a reminder
    asked for in a chat and one asked for here cannot behave differently.
    """
    application = _app()
    try:
        outcome = application.messages.handle(message)
    except LoopError as exc:
        _fail(exc)
        return
    typer.echo(outcome.reply)
    if outcome.task_id:
        typer.echo(f"task: {outcome.task_id}")
    if outcome.capture_path:
        typer.echo(f"capture: {outcome.capture_path}")


# --------------------------------------------------------------------------- #
# Generic capability invocation (agent-stack §2: no new command per pack)
# --------------------------------------------------------------------------- #
@app.command("do")
def do(operation: str,
       arguments: str = typer.Argument("{}")) -> None:
    """Invoke any enabled capability by name. Adding a pack needs no new command."""
    application = _app()
    try:
        payload = json.loads(arguments)
    except json.JSONDecodeError as exc:
        typer.echo(f"error: arguments must be JSON: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    context = AuthorityContext(owner="owner", root_id="cli", privacy=PrivacyLabel(),
                               budget=RootBudget())
    try:
        result = application.coordinator.invoke(
            objective=operation, operation=operation, arguments=payload,
            context=context)
    except LoopError as exc:
        _fail(exc)
        return
    typer.echo(json.dumps(result.response, indent=2, sort_keys=True, default=str))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
