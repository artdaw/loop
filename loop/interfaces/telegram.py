"""The vNext Telegram bot (interfaces §2), sharing the same `Application`.

Every update is authenticated and durably recorded through the same
`EventIntake` the runtime cycle already uses (loop/runtime/intake.py) before
any command runs — an unpaired instance therefore refuses every sender the
same way regardless of which interface it arrived through, and a redelivered
update is absorbed as a duplicate rather than executed twice.

**Why not `telegram.ext.Application`/`Updater`.** python-telegram-bot's own
polling loop advances its update offset as part of *fetching* the next batch,
before any handler has run — so by the time a handler durably stores the
update, the library has already told Telegram it may discard it. That is
backwards from interfaces §2: "persist inbound update before advancing the
polling acknowledgement/checkpoint." This module calls `Bot.get_updates`
directly instead and only advances its own offset, in memory, after
`EventIntake.accept` has committed the update. On a restart the offset resets
to whatever Telegram itself still holds unacknowledged; `EventIntake`'s own
idempotency key (``telegram:<update_id>``) absorbs the resulting replay, so no
separate durable offset store is needed.

**Scope of this pass.** Real, tested, and wired through `build_application()`
exactly like the CLI and HTTP interfaces: `/start`, `/help`, `/status`,
`/task`, `/tasks`, `/done`. The remaining interfaces §2 command table
(`/remind`, `/snooze`, `/remember`, `/find`, `/ask`, `/travel`, `/weather`,
`/capabilities`, `/do`, `/briefing`, `/routine*`, `/review`, `/why`,
`/cancel`, voice notes, inline buttons, and the short-lived local pairing
flow as an alternative to static `TELEGRAM_CHAT_ID`/`TELEGRAM_USER_ID`) is not
yet implemented — each needs a real backing capability or approval flow this
pass does not add, and a route that returned a canned answer would be worse
than one that does not exist.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from telegram import Bot, Update
from telegram.error import TelegramError

from loop.ai.budget import RootBudget
from loop.app import Application, build_application
from loop.core.clock import to_micros
from loop.core.errors import AuthRequired, LoopError, Unavailable
from loop.core.privacy import PrivacyLabel
from loop.runtime.authority import AuthorityContext
from loop.runtime.intake import InboundMessage
from loop.runtime.routine_dispatch import ROUTINE_SUBJECT, next_local_fire
from loop.services.feedback import summarise

#: interfaces §2: "default 1 hour if omitted".
DEFAULT_SNOOZE_MINUTES = 60

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096
POLL_TIMEOUT_SECONDS = 25

CommandHandler = Callable[["LoopTelegramBot", str, list[str]], Awaitable[str]]


class TelegramClient(Protocol):
    """The `Bot` surface this module uses — narrow enough for tests to fake
    without a real network client or a `telegram.Bot` subclass."""

    async def initialize(self) -> None: ...
    async def shutdown(self) -> None: ...
    async def get_updates(self, *, offset: int | None, timeout: int,
                          allowed_updates: Sequence[str]) -> Sequence[Update]: ...
    async def send_message(self, *, chat_id: int, text: str) -> object: ...


class LoopTelegramBot:
    """One async lifecycle, driven by the caller's event loop (interfaces §2).

    ``initialize()``/``run_forever()``/``shutdown()`` are separate concerns,
    matching the pinned library's own ``Bot.initialize``/``Bot.shutdown`` —
    a caller integrating this into the durable service controls exactly when
    each phase runs rather than this class calling ``asyncio.run`` itself.
    """

    def __init__(self, application: Application) -> None:
        token = application.settings.telegram_bot_token.strip()
        if not token or token.startswith("your-"):
            raise Unavailable(
                "TELEGRAM_BOT_TOKEN is missing; create a bot with @BotFather "
                "and set it before starting the Telegram interface.")
        self.application = application
        self.bot: TelegramClient = Bot(token=token)
        self._next_offset: int | None = None
        self._stopping = asyncio.Event()

    async def initialize(self) -> None:
        await self.bot.initialize()

    async def shutdown(self) -> None:
        await self.bot.shutdown()

    def request_stop(self) -> None:
        self._stopping.set()

    async def run_forever(self) -> None:
        """Poll until `request_stop()` is called; process updates as they
        arrive, offset advanced only after each one is durably stored."""
        while not self._stopping.is_set():
            try:
                updates = await self.bot.get_updates(
                    offset=self._next_offset, timeout=POLL_TIMEOUT_SECONDS,
                    allowed_updates=["message"])
            except TelegramError:
                logger.exception("Telegram polling request failed; retrying")
                await asyncio.sleep(1)
                continue

            for update in updates:
                await self._handle_update(update)
                self._next_offset = update.update_id + 1

    # ------------------------------------------------------------------ #
    # One update
    # ------------------------------------------------------------------ #
    async def _handle_update(self, update: Update) -> None:
        message = update.message
        if message is None or message.text is None:
            return
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return

        inbound = InboundMessage(
            origin="telegram", origin_id=str(update.update_id),
            idempotency_key=f"telegram:{update.update_id}",
            kind="message.received",
            payload={"text": message.text},
            actor_chat_id=str(chat.id), actor_user_id=str(user.id))

        try:
            result = self.application.intake.accept(inbound)
        except AuthRequired:
            logger.warning("Rejected Telegram update from chat=%s user=%s",
                           chat.id, user.id)
            return

        if not result.created:
            # A redelivery of an update already processed and answered.
            return

        reply = await self._dispatch(message.text)
        if reply:
            await self._reply(chat.id, reply)

    async def _dispatch(self, text: str) -> str:
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return ""
        command = parts[0].lstrip("/").lower()
        rest = parts[1] if len(parts) > 1 else ""
        handler = _COMMANDS.get(command)
        if handler is None:
            return (f"Unrecognised command /{command}. Try /help for what "
                    "this instance currently supports.")
        try:
            return await handler(self, rest, rest.split())
        except LoopError as exc:
            return f"error: {exc.message}"

    async def _reply(self, chat_id: int, text: str) -> None:
        for start in range(0, len(text), MAX_MESSAGE_LENGTH):
            chunk = text[start:start + MAX_MESSAGE_LENGTH]
            try:
                await self.bot.send_message(chat_id=chat_id, text=chunk)
            except TelegramError:
                logger.exception("Failed to send a Telegram reply")


# --------------------------------------------------------------------------- #
# Commands (interfaces §2 subset — see module docstring for what's deferred)
# --------------------------------------------------------------------------- #
async def _start(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    """What this instance can actually do — and what it cannot, by name.

    interfaces §2 lists a larger table than this. Listing the whole table and
    failing on half of it would be worse than a short list: the owner would
    learn by trial which commands are real. So the unimplemented ones are
    named here as not yet available, rather than omitted and discovered.
    """
    del text, args
    configured = bot.application.knowledge is not None
    lines = [
        "Loop is connected.",
        "",
        "Commitments: /task <text>, /tasks, /done <id>",
        "Knowledge: /remember <text>, /find <query>, /ask <question>"
        + ("" if configured else "  (no vault configured yet)"),
        "Daily life: /weather <place>",
        "Routines: /routines, /pause <id>, /resume <id>, /snooze <id> [minutes]",
        "Abilities: /capabilities, /do <operation> <json>",
        "Review: /status, /review, /why <id>",
        "",
        "Not available yet on this instance: /remind, /travel, /trips, /trip, "
        "/briefing, /routine <description>, /cancel, voice notes and buttons.",
    ]
    return "\n".join(lines)


async def _status(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    del text, args
    application = bot.application
    pending = application.service.pending_summary()
    return (f"triggers enabled: {pending['triggers']}\n"
           f"jobs queued/retrying: {pending['jobs']}\n"
           f"outbox pending: {pending['outbox']}")


async def _task(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    del args
    if not text.strip():
        return "Usage: /task <text>"
    task = bot.application.tasks.create(text.strip())
    return f"{task.id}  {task.title}  [{task.status}]"


async def _tasks(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    del text, args
    tasks = bot.application.tasks.list(status="ready")
    if not tasks:
        return "No open tasks."
    return "\n".join(f"{t.id}  {t.title}" for t in tasks)


async def _done(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    del text
    if not args:
        return "Usage: /done <id>"
    task_id = args[0]
    tasks = {t.id: t for t in bot.application.tasks.list()}
    task = tasks.get(task_id)
    if task is None:
        return f"No such task {task_id!r}."
    completed = bot.application.tasks.complete(task_id, expected_version=task.version)
    return f"{completed.id}  done"



# --------------------------------------------------------------------------- #
# Knowledge
# --------------------------------------------------------------------------- #
def _knowledge(bot: LoopTelegramBot) -> Any:
    knowledge = bot.application.knowledge
    if knowledge is None:
        raise Unavailable("No vault is configured on this instance.")
    return knowledge


async def _remember(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    """Exact raw capture. The reply states what actually happened (vault §5)."""
    del args
    if not text.strip():
        return "Usage: /remember <text>"
    result = _knowledge(bot).capture(text, origin="telegram")
    return result.message


async def _find(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    del args
    if not text.strip():
        return "Usage: /find <query>"
    answer = _knowledge(bot).answer(text)
    return answer.text


async def _weather(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    """Compare sources and prepare advice, or ask which place is meant."""
    del args
    application = bot.application
    context = AuthorityContext(owner="owner", root_id="telegram",
                               privacy=PrivacyLabel(), budget=RootBudget())
    arguments = {"location_ref": text.strip()} if text.strip() else {}
    result = application.invoker.invoke("weather.prepare", arguments,
                                        context=context)
    return str(result.output.get("answer") or "No advice was produced.")


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #
async def _capabilities(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    del text, args
    operations = sorted(bot.application.known_capabilities())
    if not operations:
        return "No capabilities are enabled."
    return "Enabled:\n" + "\n".join(f"  {name}" for name in operations)


async def _do(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    """One route for every pack — adding a capability adds no command."""
    if not args:
        return "Usage: /do <operation> <json arguments>"
    operation = args[0]
    raw = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else "{}"
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"Arguments must be JSON: {exc}"
    if not isinstance(arguments, dict):
        return "Arguments must be a JSON object."

    # Through the coordinator, exactly as `loop-next do` and the HTTP invoke
    # route go. Calling `invoker.invoke` directly from here would skip the
    # coordinator's role-scope, plan authority and operation-ledger checks —
    # so the same call would carry different authority depending on which
    # surface it arrived on, which is the drift interfaces §5 forbids.
    context = AuthorityContext(owner="owner", root_id="telegram",
                               privacy=PrivacyLabel(), budget=RootBudget())
    result = bot.application.coordinator.invoke(
        objective=operation, operation=operation, arguments=arguments,
        context=context)
    return json.dumps(result.response, indent=2, sort_keys=True, default=str)


# --------------------------------------------------------------------------- #
# Routines
# --------------------------------------------------------------------------- #
async def _routines(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    del text, args
    application = bot.application
    routines = application.routines.list_routines()
    if not routines:
        return "No routines saved."
    lines = []
    for routine in routines:
        triggers = application.triggers.for_subject(ROUTINE_SUBJECT, routine.slug)
        when = next_local_fire(triggers[0]) if triggers else None
        lines.append(f"{routine.slug}  [{routine.status.value}]  "
                     + (f"next {when.isoformat()}" if when else "unscheduled"))
        for question in routine.blocking_questions:
            lines.append(f"    needs an answer: {question}")
    return "\n".join(lines)


async def _pause(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    del text
    if not args:
        return "Usage: /pause <routine-id>"
    routine = bot.application.routine_scheduler.pause(args[0])
    return f"{routine.slug} {routine.status.value}"


async def _resume(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    del text
    if not args:
        return "Usage: /resume <routine-id>"
    routine = bot.application.routine_scheduler.resume(args[0])
    return f"{routine.slug} {routine.status.value}"


async def _snooze(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    """Reschedule this occurrence, and record it as evidence about timing.

    Both halves matter and they are different things: the message moves now,
    and the snooze becomes one observation towards a proposal to move the
    routine permanently — which the owner still has to confirm (vault §9).
    """
    del text
    if not args:
        return "Usage: /snooze <id> [minutes]"
    subject = args[0]
    minutes = DEFAULT_SNOOZE_MINUTES
    if len(args) > 1:
        try:
            minutes = int(args[1])
        except ValueError:
            return f"{args[1]!r} is not a number of minutes."
    if minutes <= 0:
        return "A snooze moves a notification later."

    application = bot.application
    subject_ref = (subject if ":" in subject
                   else f"{ROUTINE_SUBJECT}:{subject}")
    until = to_micros(application.clock.now() + dt.timedelta(minutes=minutes))
    moved = application.outbox.defer_for_subject(subject_ref, until=until)
    application.feedback.record_snooze(
        subject_ref, event_id=f"telegram:{subject_ref}:{until}",
        shift_minutes=minutes)
    if not moved:
        return (f"Nothing is queued for {subject}, so there was nothing to "
                f"move. Recorded the {minutes}-minute snooze as feedback.")
    return f"Moved {moved} message(s) for {subject} {minutes} minutes later."


# --------------------------------------------------------------------------- #
# Review and provenance
# --------------------------------------------------------------------------- #
async def _review(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    del text, args
    application = bot.application
    pending = application.service.pending_summary()
    report = summarise(application.feedback.review_all())

    lines = [f"Open tasks: {len(application.tasks.list(status='ready'))}",
             f"Pending work: {pending['jobs']} job(s), "
             f"{pending['outbox']} message(s)"]
    if pending["unknown"]:
        lines.append(f"Uncertain deliveries: {pending['unknown']} — not resent.")
    if report.proposals:
        lines.append("")
        lines.append("Proposed adaptations (none applied):")
        for outcome in report.proposals:
            lines.append(f"  {outcome.subject_ref}: {outcome.question}")
    else:
        lines.append("No adaptations to propose.")
    return "\n".join(lines)


async def _why(bot: LoopTelegramBot, text: str, args: list[str]) -> str:
    """Where a task, routine or message came from — recorded, never inferred."""
    del text
    if not args:
        return "Usage: /why <id>"
    identifier = args[0]
    application = bot.application

    routine = application.routines.get(identifier)
    if routine is not None:
        triggers = application.triggers.for_subject(ROUTINE_SUBJECT, routine.slug)
        when = next_local_fire(triggers[0]) if triggers else None
        return (f"Routine {routine.slug} is {routine.status.value}.\n"
                f"Activated by event: {routine.activation_event_id or 'none'}\n"
                f"Trigger: {routine.trigger}\n"
                + (f"Next run: {when.isoformat()}" if when
                   else "Not currently scheduled."))

    task = next((t for t in application.tasks.list() if t.id == identifier), None)
    if task is not None:
        return (f"Task {task.id} is {task.status}.\n"
                f"Title: {task.title}\n"
                f"Due: {task.due_date or 'no date recorded'}\n"
                f"Version {task.version}.")

    notification = application.outbox.get_notification(identifier)
    if notification is not None:
        return (f"Notification {notification.id} is {notification.state}.\n"
                f"Category: {notification.category} (this is what decides "
                "whether quiet hours and the daily cap apply)\n"
                f"Subject: {notification.subject_ref}\n"
                f"Occurrence: {notification.occurrence_key}")

    return (f"Nothing recorded under {identifier!r}. IDs come from /tasks, "
            "/routines and delivered messages.")

_COMMANDS: dict[str, CommandHandler] = {
    "start": _start,
    "help": _start,
    "status": _status,
    "task": _task,
    "tasks": _tasks,
    "done": _done,
    "remember": _remember,
    "find": _find,
    # `/find` and `/ask` differ in the contract by how the answer is composed;
    # both are grounded in the same cited retrieval, and answering identically
    # is honest until a distinct grounded-answer path exists to point `/ask` at.
    "ask": _find,
    "weather": _weather,
    "capabilities": _capabilities,
    "do": _do,
    "routines": _routines,
    "pause": _pause,
    "resume": _resume,
    "snooze": _snooze,
    "review": _review,
    "why": _why,
}


def main() -> None:
    application = build_application()
    bot = LoopTelegramBot(application)

    async def _run() -> None:
        await bot.initialize()
        try:
            await bot.run_forever()
        finally:
            await bot.shutdown()

    asyncio.run(_run())
