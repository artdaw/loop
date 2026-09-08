"""The composition root: one object graph, shared by every interface.

Agent-stack §1: "CLI, bot and web share application services." That is a
constraint on architecture, not a suggestion — if the CLI and the HTTP API each
built their own `TaskService`, they would each own a different truth about
whether a task is done. This module is the one place all of the wiring happens,
so every interface constructs its services from the same function and cannot
drift into a second implementation of "how a task gets completed."

Building this required deciding what a fresh install actually needs at
startup, in order:

1. **The engine and its pragmas**, then **migrations applied** — a service
   that reads from `tasks` before the table exists is not a subtle bug, it is
   an unhandled exception on the first command anyone runs.
2. **The model gateway**, built from settings alone — no model is required to
   exist for deterministic commands to work (I6: idle service performs no
   inference; read-only commands must work without a model).
3. **The domain services** (tasks, triggers, jobs, outbox) and the **learning
   and capability stores**, all sharing one `sessions` factory so a
   transaction started by one is visible to another reading the same tables.
4. **The capability registry**, discovering from configured paths and
   reapplying whatever the owner had enabled — in that order, because
   enablement can only reattach to entries that already exist (see
   `CapabilityRegistry.reapply_enablement`).
5. **The coordinator**, wired to the invoker and the run store, so a plan that
   pauses for approval persists through the same checkpointer this process
   will still be running if it resumes it.

Nothing here talks to a real model, a real Telegram server, or a real vault
unless the settings actually configure one — importing this module and calling
`build_application` must not require credentials any more than `loop status`
should.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from loop.agents.coordinator import Coordinator
from loop.agents.roles import RoleRegistry
from loop.ai.model_gateway import ModelGateway
from loop.capabilities.objects import CapabilityObjectStore
from loop.capabilities.registry import CapabilityRegistry
from loop.capabilities.runners import ArtifactStore, CapabilityInvoker
from loop.capabilities.travel.trip import TripStore
from loop.capabilities.weather.ports import (
    load_locations,
    load_warning_feeds,
    weather_handlers,
)
from loop.capabilities.weather.service import WeatherService
from loop.core.clock import Clock, SystemClock
from loop.core.settings import Settings
from loop.db.migrations import migrate
from loop.db.session import create_db_engine, session_factory
from loop.ops.snapshot import SnapshotBarrier
from loop.runtime.condition_dispatch import (
    ConditionDispatcher,
    ConditionEdges,
)
from loop.runtime.coordinator_worker import CoordinatorJobWorker
from loop.runtime.intake import EventIntake
from loop.runtime.jobs import JobQueue
from loop.runtime.notify_policy import NotificationManager, NotificationPolicy
from loop.runtime.operations import OperationLedger
from loop.runtime.outbox import NotificationOutbox, Transport
from loop.runtime.reminder_dispatch import TaskReminderDispatcher
from loop.runtime.routine_dispatch import (
    RoutineDispatcher,
    RoutineJobWorker,
    RoutineScheduler,
)
from loop.runtime.routines import RoutineService
from loop.runtime.runs import RunStore
from loop.runtime.service import LoopService
from loop.runtime.triggers import TriggerService
from loop.runtime.trip_monitor import (
    CompositeTriggerDispatcher,
    TripCheckDispatcher,
    TripMonitorScheduler,
)
from loop.services.actions import CallbackActions
from loop.services.export_map import ExportMap
from loop.services.feedback import (
    FeedbackLog,
    FeedbackService,
    PreferenceJournal,
)
from loop.services.knowledge import KnowledgeService
from loop.services.learning import PreferenceStore
from loop.services.messages import MessageService, PendingClarifications
from loop.services.observations import ObservationStore
from loop.services.reminders import ReminderService
from loop.services.tasks import TaskService
from loop.vault.gateway import VaultGateway
from loop.vault.search import VaultSearch

logger = logging.getLogger(__name__)


@dataclass
class Application:
    """Every shared service, constructed once per process (agent-stack §1)."""

    settings: Settings
    clock: Clock
    sessions: sessionmaker[Session]
    model_gateway: ModelGateway
    tasks: TaskService
    reminders: ReminderService
    triggers: TriggerService
    jobs: JobQueue
    outbox: NotificationOutbox
    intake: EventIntake
    routines: RoutineService
    preferences: PreferenceStore
    trips: TripStore
    capability_objects: CapabilityObjectStore
    export_map: ExportMap
    notifications: NotificationManager
    weather: WeatherService
    vault_search: VaultSearch
    #: None when no vault is configured — the honest state, not an empty vault.
    knowledge: KnowledgeService | None
    routine_scheduler: RoutineScheduler
    routine_worker: RoutineJobWorker
    trip_monitor: TripMonitorScheduler
    feedback: FeedbackService
    messages: MessageService
    actions: CallbackActions
    observations: ObservationStore
    barrier: SnapshotBarrier
    conditions: ConditionDispatcher
    registry: CapabilityRegistry
    artifacts: ArtifactStore
    invoker: CapabilityInvoker
    roles: RoleRegistry
    operations: OperationLedger
    runs: RunStore
    coordinator: Coordinator
    service: LoopService
    coordinator_worker: CoordinatorJobWorker
    #: The capability roots actually configured, for `loop doctor`/status.
    capability_roots: list[Path] = field(default_factory=list)

    def known_capabilities(self) -> set[str]:
        """Every operation name a routine or pack may legitimately name.

        Both halves matter: enabled pack operations *and* the trusted ports
        the application supplies itself. Validating a routine against only one
        of them rejects a correct document — which, for a routine, means the
        owner's request is silently refused rather than run.
        """
        return set(self.registry.enabled_operations()) | set(self.invoker.handlers)


def build_application(settings: Settings | None = None, *,
                      clock: Clock | None = None,
                      apply_migrations: bool = True,
                      weather: WeatherService | None = None,
                      transports: dict[str, Transport] | None = None,
                      model_gateway: ModelGateway | None = None
                      ) -> Application:
    """Construct the full object graph from settings alone.

    This is the one function every interface calls. A CLI command, an HTTP
    route and a Telegram handler that each called this and used the returned
    `Application` are, by construction, using the same services — there is no
    second place a duplicate `TaskService` could be built.

    `weather`, `transports` and `model_gateway` are the injected
    collaborators: they are the only components here that would otherwise
    reach a network, so a test supplies fakes for them and everything
    downstream — ports, routines, jobs, policy, outbox, compile — stays the
    real code.
    """
    settings = settings or Settings()
    clock = clock or SystemClock()

    engine = create_db_engine(settings.database_url)
    if apply_migrations:
        migrate(engine, clock=clock)
    sessions = session_factory(engine)

    model_gateway = model_gateway or ModelGateway(settings=settings, clock=clock)

    tasks = TaskService(sessions=sessions, clock=clock,
                        default_timezone=settings.timezone)
    triggers = TriggerService(sessions=sessions, clock=clock)
    jobs = JobQueue(sessions=sessions, clock=clock)
    # No transport is registered by default: an unconfigured channel must
    # fail as unavailable rather than silently look delivered. An interface
    # that owns a real channel supplies it here.
    def _task_is_current(notification) -> bool:
        if not notification.subject_ref.startswith("task:"):
            return True
        task = tasks.get(notification.subject_ref.removeprefix("task:"))
        return task is not None and task.is_open

    outbox = NotificationOutbox(sessions=sessions, clock=clock,
                                transports=transports or {},
                                preflight=_task_is_current)
    reminders = ReminderService(sessions=sessions, tasks=tasks,
                                triggers=triggers, outbox=outbox)
    intake = EventIntake(sessions=sessions, settings=settings, clock=clock)
    operations = OperationLedger(sessions=sessions, clock=clock)
    runs = RunStore(sessions=sessions, clock=clock)

    # Every writer checks this before admitting new work, so a coordinated
    # snapshot can prove nothing changed underneath it (O09/LG11). Built early
    # because the sweep and the workers all take it.
    barrier = SnapshotBarrier(sessions=sessions, clock=clock)
    routines = RoutineService(sessions=sessions, clock=clock)
    preferences = PreferenceStore(sessions=sessions)
    trips = TripStore(sessions=sessions, clock=clock)
    capability_objects = CapabilityObjectStore(sessions=sessions)
    export_map = ExportMap(sessions=sessions)
    notifications = NotificationManager(
        policy=NotificationPolicy(timezone=settings.timezone),
        clock=clock, sessions=sessions)

    roots = settings.capability_roots
    registry = CapabilityRegistry(roots=roots, sessions=sessions)
    registry.register_all(registry.discover())
    registry.reapply_enablement()

    vault_root = _vault_root(settings)
    vault_gateway = (VaultGateway(root=vault_root, sessions=sessions, clock=clock)
                     if vault_root is not None else None)
    weather_service = weather or WeatherService(
        locations=load_locations(vault_root, settings.loop_policy_path),
        # Unconfigured means the warning state stays `unknown`, which is the
        # honest answer — never an all-clear from a feed nobody chose.
        warning_feeds=load_warning_feeds(vault_root, settings.loop_policy_path))

    # One index and one gateway per process, shared by the knowledge service
    # and the `vault.search` port — two of them would disagree about what has
    # been indexed the moment either one wrote.
    vault_search = VaultSearch(sessions=sessions)
    knowledge = None
    if vault_gateway is not None:
        knowledge = KnowledgeService(
            gateway=vault_gateway, search=vault_search,
            model_gateway=model_gateway, clock=clock)

    artifacts = ArtifactStore(sessions=sessions, clock=clock)
    invoker = CapabilityInvoker(
        registry=registry, gateway=model_gateway, artifact_store=artifacts,
        runs=runs, handlers=_trusted_handlers(tasks, clock, weather_service,
                                              knowledge))
    roles = RoleRegistry(vault_root=vault_root)

    coordinator = Coordinator(invoker=invoker, roles=roles, runs=runs,
                              approvals=operations)

    # Firing a trigger queues a durable job and nothing else — deterministic
    # by contract (D16/D17), because this runs on every sweep whether or not
    # anything is due.
    routine_scheduler = RoutineScheduler(routines=routines, triggers=triggers,
                                         clock=clock)
    routine_dispatcher = RoutineDispatcher(jobs=jobs, routines=routines)
    routine_worker = RoutineJobWorker(
        jobs=jobs, routines=routines, invoker=invoker, outbox=outbox,
        notifications=notifications, clock=clock,
        default_destination=settings.telegram_chat_id, barrier=barrier)

    # Condition routines fire on the edge, and the edge is stored: a restart
    # that forgot it would turn "still true" into "newly true" and announce
    # the same condition again on every deploy (P05).
    observations = ObservationStore(sessions=sessions, clock=clock)
    conditions = ConditionDispatcher(
        routines=routines, observations=observations,
        edges=ConditionEdges(sessions=sessions, clock=clock), jobs=jobs,
        clock=clock)

    # Buttons on a delivered reminder carry opaque ids; everything they need
    # is looked up here rather than encoded in the callback (interfaces §2).
    actions = CallbackActions(sessions=sessions, clock=clock)

    # Free text reaches the same services an explicit command does, so
    # "remind me tomorrow at 9" and `task add` cannot disagree about what a
    # task is (T04-T07).
    messages = MessageService(
        tasks=tasks, reminders=reminders,
        pending=PendingClarifications(sessions=sessions, clock=clock),
        knowledge=knowledge, clock=clock, timezone=settings.timezone)

    # Learning writes its records where the owner can read and delete them.
    # With no vault configured the loop still works in SQLite; what is lost is
    # the owner's ability to inspect and correct it by hand (vault §9), which
    # is a configuration gap, not a reason to stop learning.
    feedback = FeedbackService(
        log=FeedbackLog(sessions=sessions, clock=clock),
        preferences=preferences, scheduler=routine_scheduler,
        journal=PreferenceJournal(gateway=vault_gateway), clock=clock)

    # Two kinds of scheduled subject now exist. Each dispatcher ignores
    # subjects that are not its own, so routing stays in the dispatchers
    # rather than becoming a subject-type table in the composition root.
    trip_monitor = TripMonitorScheduler(triggers=triggers, sessions=sessions,
                                        clock=clock)
    dispatchers = CompositeTriggerDispatcher([
        TaskReminderDispatcher(tasks=tasks, outbox=outbox,
                               destination_id=settings.telegram_chat_id,
                               actions=actions,
                               owner=str(settings.telegram_user_id)),
        routine_dispatcher,
        TripCheckDispatcher(jobs=jobs, scheduler=trip_monitor)])

    service = LoopService(sessions=sessions, clock=clock, triggers=triggers,
                          jobs=jobs, outbox=outbox, on_trigger=dispatchers,
                          barrier=barrier)
    coordinator_worker = CoordinatorJobWorker(jobs=jobs, coordinator=coordinator)

    # A routine activated in a process that died before writing its trigger is
    # otherwise approved-but-never-running, with no error anywhere to show it.
    reconciled = routine_scheduler.reconcile()
    if reconciled.changed:
        logger.info("Routine schedules reconciled at startup: %s scheduled, "
                    "%s disabled", len(reconciled.scheduled),
                    len(reconciled.disabled))

    return Application(
        settings=settings, clock=clock, sessions=sessions,
        model_gateway=model_gateway, tasks=tasks, reminders=reminders,
        triggers=triggers,
        jobs=jobs, outbox=outbox, intake=intake, routines=routines,
        preferences=preferences, trips=trips,
        capability_objects=capability_objects, export_map=export_map,
        notifications=notifications, weather=weather_service,
        vault_search=vault_search, knowledge=knowledge,
        routine_scheduler=routine_scheduler, routine_worker=routine_worker,
        trip_monitor=trip_monitor, feedback=feedback, messages=messages,
        actions=actions, observations=observations, barrier=barrier,
        conditions=conditions,
        registry=registry, artifacts=artifacts,
        invoker=invoker, roles=roles, operations=operations, runs=runs,
        coordinator=coordinator, service=service,
        coordinator_worker=coordinator_worker, capability_roots=roots)


def _vault_root(settings: Settings) -> Path | None:
    """The configured vault, or None — role documents are consulted, not required.

    `Settings.vault_root` only looks at `obsidian_vault_path`; the private
    path is a distinct, separately-scoped setting, so this checks both rather
    than assuming the public one is always what a deployment configured.
    """
    return settings.vault_root or (
        Path(settings.obsidian_private_vault_path).expanduser().resolve()
        if settings.obsidian_private_vault_path.strip() else None)


def _trusted_handlers(tasks: TaskService, clock: Clock,
                      weather: WeatherService,
                      knowledge: KnowledgeService | None) -> dict:
    """The trusted ports every shipped pack and routine may declare as a tool.

    These are exactly the operations a manifest's `tools:` list — or a
    routine's `steps:` — can name: `vault.search` and `reminder.schedule`,
    used by the two shipped example packs (`packs/plantcare`,
    `packs/bikeservice`), plus `weather.forecast`/`weather.prepare`. A pack
    cannot invent a new trusted port by declaring one; it can only ask for one
    that already exists here, which is the whole point of the port being
    *trusted* rather than supplied by the pack itself.
    """
    from loop.capabilities.runners import RegisteredHandler
    from loop.core.errors import Unavailable

    def vault_search(args: dict, context) -> dict:
        if knowledge is None:
            raise Unavailable(
                "No vault is configured, so there is nothing to search.")
        # `allow_local_only=True` includes private rows: this handler runs
        # inside the same privacy-labelled context the model call already
        # carries, and `VaultSearch` applies its own row-level privacy filter
        # regardless. Excluding local-only rows here would additionally need
        # to know the *destination* of this specific tool result, which this
        # handler is not given — getting that wrong silently is worse than
        # keeping the default and filtering at the row level where the label
        # actually lives.
        answer = knowledge.answer(str(args.get("query", "")))
        return {"answer": answer.text, "sources": answer.cited_paths,
                "knowledge_gap": answer.knowledge_gap}

    def reminder_schedule(args: dict, context) -> dict:
        task = tasks.create(title=str(args.get("query", "reminder")),
                            owner=context.owner)
        return {"answer": f"Scheduled: {task.title}", "sources": [task.id]}

    return {
        **weather_handlers(weather, clock=clock),
        "vault.search": RegisteredHandler(
            vault_search, description="Search the owner's authorised notes.",
            input_schema={"type": "object", "additionalProperties": True},
            owner_role="seeker"),
        "reminder.schedule": RegisteredHandler(
            reminder_schedule, description="Propose a local reminder.",
            input_schema={"type": "object", "additionalProperties": True},
            owner_role="commitments"),
    }
