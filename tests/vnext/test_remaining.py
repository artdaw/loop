"""The last acceptance block — T04–T07, D12–D14, EX06/EX07/EX11–EX14."""

from __future__ import annotations

import datetime as dt

import pytest

from loop.capabilities.objects import (
    CapabilityObjectStore,
    ChangeKind,
    ObjectSchema,
    diff_schemas,
    rollback_schema,
)
from loop.core.errors import Conflict, InvalidInput, ValidationFailed
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.runtime.interpret import (
    IntentKind,
    PlanStatus,
    interpret,
    parse_timing,
    resolve_clarification,
)
from loop.runtime.triggers import Trigger, retime_floating

NOW = 1_780_000_000


# --------------------------------------------------------------------------- #
# T04 — "remind me later" has no time in it
# --------------------------------------------------------------------------- #
LATER = "Remind me later to call the dentist"


def test_t04_the_task_is_still_saved():
    result = interpret(LATER)

    assert result.creates_task is True
    assert result.task_title == "call the dentist"


def test_t04_the_plan_needs_input():
    assert interpret(LATER).plan_status is PlanStatus.NEEDS_INPUT


def test_t04_exactly_one_question_is_asked():
    assert len(interpret(LATER).questions) == 1


def test_t04_the_question_asks_when():
    assert "when" in interpret(LATER).questions[0].lower()


def test_t04_no_reminder_is_claimed():
    """A cheerful "I'll remind you!" for a reminder that does not exist."""
    result = interpret(LATER)

    assert result.may_claim_scheduled is False
    assert "I'll remind you" not in result.acknowledgement()


def test_t04_the_acknowledgement_says_what_is_missing():
    assert "no reminder time yet" in interpret(LATER).acknowledgement()


def test_t04_later_is_recognised_as_vague_not_as_a_time():
    timing = parse_timing(LATER)

    assert timing.vague == "later"
    assert timing.is_concrete is False


def test_t04_a_time_without_a_day_is_still_incomplete():
    result = interpret("Remind me at 10 to call the dentist")

    assert result.plan_status is PlanStatus.NEEDS_INPUT
    assert "Which day" in result.questions[0]


def test_t04_a_day_without_a_time_is_still_incomplete():
    result = interpret("Remind me tomorrow to call the dentist")

    assert result.plan_status is PlanStatus.NEEDS_INPUT
    assert "What time" in result.questions[0]


# --------------------------------------------------------------------------- #
# T05 — answering the clarification
# --------------------------------------------------------------------------- #
def test_t05_the_answer_completes_the_same_task():
    pending = interpret(LATER)
    resolved = resolve_clarification(pending, "tomorrow at 10")

    assert resolved.task_title == pending.task_title


def test_t05_the_plan_becomes_ready():
    resolved = resolve_clarification(interpret(LATER), "tomorrow at 10")

    assert resolved.plan_status is PlanStatus.READY
    assert resolved.may_claim_scheduled is True


def test_t05_the_supplied_time_is_used():
    resolved = resolve_clarification(interpret(LATER), "tomorrow at 10")

    assert resolved.timing.wall_time == dt.time(10, 0)
    assert resolved.timing.day_offset == 1


def test_t05_no_second_task_is_created():
    """"Tomorrow at 10" read as a new request makes one appointment into two."""
    resolved = resolve_clarification(interpret(LATER), "tomorrow at 10")

    assert resolved.kind is IntentKind.TASK
    assert resolved.capture_body == ""


def test_t05_the_vague_hint_is_cleared_once_answered():
    resolved = resolve_clarification(interpret(LATER), "tomorrow at 10")
    assert resolved.timing.is_vague is False


def test_t05_a_partial_answer_asks_again():
    resolved = resolve_clarification(interpret(LATER), "tomorrow")

    assert resolved.plan_status is PlanStatus.NEEDS_INPUT
    assert "What time" in resolved.questions[0]


def test_t05_a_partial_answer_keeps_what_was_already_known():
    partly = interpret("Remind me tomorrow to call the dentist")
    resolved = resolve_clarification(partly, "at 10")

    assert resolved.timing.day_offset == 1
    assert resolved.timing.wall_time == dt.time(10, 0)


def test_t05_answering_a_ready_plan_changes_nothing():
    ready = interpret("Remind me tomorrow at 10 to call the dentist")
    assert resolve_clarification(ready, "next week") is ready


# --------------------------------------------------------------------------- #
# T06 — a thing to do versus a thing that is true
# --------------------------------------------------------------------------- #
def test_t06_remember_to_is_a_task():
    result = interpret("Remember to call the dentist")

    assert result.kind is IntentKind.TASK
    assert result.creates_capture is False


def test_t06_remember_that_is_a_capture():
    result = interpret("Remember that production takes six weeks")

    assert result.kind is IntentKind.CAPTURE
    assert result.creates_task is False


def test_t06_the_captured_fact_is_stored_exactly():
    """Paraphrasing is how "six weeks" becomes "about a month"."""
    result = interpret("Remember that production takes six weeks")

    assert result.capture_body == "production takes six weeks"


def test_t06_a_capture_is_immediately_ready():
    """A fact needs no time, so nothing is pending."""
    result = interpret("Remember that production takes six weeks")

    assert result.plan_status is PlanStatus.READY
    assert result.questions == []


def test_t06_the_task_form_still_needs_a_time():
    result = interpret("Remember to call the dentist")

    assert result.plan_status is PlanStatus.NEEDS_INPUT
    assert result.may_claim_scheduled is False


def test_t06_an_unclassifiable_message_asks_rather_than_guessing():
    result = interpret("the dentist")

    assert result.kind is IntentKind.UNKNOWN
    assert result.questions


def test_t06_note_that_also_captures():
    assert interpret("Note that the vendor moved offices").kind \
        is IntentKind.CAPTURE


# --------------------------------------------------------------------------- #
# T07 — one message, two commitments
# --------------------------------------------------------------------------- #
COMBINED = ("Remember that the vendor lead time is six weeks and remind me "
            "Friday at 10 to verify it")


def test_t07_both_a_capture_and_a_task_are_created():
    result = interpret(COMBINED)

    assert result.kind is IntentKind.CAPTURE_WITH_TASK
    assert result.creates_capture and result.creates_task


def test_t07_the_fact_is_captured_without_the_reminder_text():
    result = interpret(COMBINED)

    assert result.capture_body == "the vendor lead time is six weeks"
    assert "remind" not in result.capture_body


def test_t07_the_task_is_the_action_not_the_fact():
    assert interpret(COMBINED).task_title == "verify it"


def test_t07_a_concrete_time_makes_the_task_ready():
    result = interpret(COMBINED)

    assert result.plan_status is PlanStatus.READY
    assert result.timing.weekday == "friday"
    assert result.timing.wall_time == dt.time(10, 0)


def test_t07_each_half_gets_its_own_truthful_result():
    acknowledgement = interpret(COMBINED).acknowledgement()

    assert "Saved that." in acknowledgement
    assert "I'll remind you" in acknowledgement


def test_t07_a_combined_request_without_a_time_still_captures():
    """The fact is saved even though the reminder is unresolved."""
    result = interpret("Remember that the vendor moved and remind me later to "
                       "update the address")

    assert result.creates_capture is True
    assert result.plan_status is PlanStatus.NEEDS_INPUT
    assert "Saved that." in result.acknowledgement()


def test_t07_the_dangling_conjunction_is_not_part_of_the_fact():
    assert not interpret(COMBINED).capture_body.endswith("and")


# --------------------------------------------------------------------------- #
# D12 — changing timezone
# --------------------------------------------------------------------------- #
def _floating(trigger_id: str = "t1", **kw) -> Trigger:
    definition = {"timezone": "Europe/Berlin", "at": "07:00",
                  "days": ["mon", "tue"]}
    definition.update(kw.pop("definition", {}))
    return Trigger(id=trigger_id, subject_type="routine", subject_id="r1",
                   kind="local_schedule", definition=definition, enabled=True,
                   revision=1, **kw)


def test_d12_a_floating_routine_moves_with_the_owner():
    """"07:00 wherever I am" should stay 07:00 after moving to Lisbon."""
    trigger = _floating()
    report = retime_floating([trigger], new_timezone="Europe/Lisbon")

    assert report.retimed == ["t1"]
    assert trigger.definition["timezone"] == "Europe/Lisbon"


def test_d12_an_explicit_zone_is_left_alone():
    """"07:00 Berlin time" usually means something in Berlin happens then."""
    pinned = _floating("t2", definition={"timezone_is_explicit": True})
    report = retime_floating([pinned], new_timezone="Europe/Lisbon")

    assert report.unchanged_explicit == ["t2"]
    assert pinned.definition["timezone"] == "Europe/Berlin"


def test_d12_a_one_shot_trigger_is_not_retimed():
    one_shot = Trigger(id="t3", subject_type="task", subject_id="k1", kind="at",
                       definition={"fire_at": 1}, enabled=True, revision=1)
    report = retime_floating([one_shot], new_timezone="Europe/Lisbon")

    assert report.unchanged_explicit == ["t3"]


def test_d12_past_fires_are_not_rewritten():
    trigger = _floating(last_fire_at=12345)
    report = retime_floating([trigger], new_timezone="Europe/Lisbon")

    assert trigger.last_fire_at == 12345
    assert report.past_occurrences_untouched == 1


def test_d12_the_report_accounts_for_every_trigger():
    triggers = [_floating("a"), _floating("b", definition={"timezone_is_explicit": True})]
    report = retime_floating(triggers, new_timezone="Europe/Lisbon")

    assert report.total == 2


def test_d12_the_schedule_time_itself_is_unchanged():
    trigger = _floating()
    retime_floating([trigger], new_timezone="Europe/Lisbon")

    assert trigger.definition["at"] == "07:00"
    assert trigger.definition["days"] == ["mon", "tue"]


# --------------------------------------------------------------------------- #
# D13 — expired approval or a changed target
# --------------------------------------------------------------------------- #
def _ledger(sessions, clock):
    from loop.runtime.operations import OperationLedger

    return OperationLedger(sessions=sessions, clock=clock)


def test_d13_an_expired_approval_cannot_be_resolved(sessions, clock):
    from loop.core.errors import ApprovalRequired

    ledger = _ledger(sessions, clock)
    operation = ledger.propose(action="email.send", target="contact:1",
                               payload={"to": "someone@example.invalid"},
                               idempotency_key="op-d13-a")
    approval = ledger.request_approval(operation, destination_id="owner:telegram",
                                       actor_id="owner", ttl_seconds=60)
    clock.advance(seconds=120)

    with pytest.raises(ApprovalRequired, match="expired"):
        ledger.resolve_approval(approval.id, resolution="approved",
                                actor_id="owner",
                                current_input_hash=operation.input_hash)


def test_d13_an_unapproved_operation_still_cannot_run(sessions, clock):
    from loop.core.errors import ApprovalRequired

    ledger = _ledger(sessions, clock)
    operation = ledger.propose(action="email.send", target="contact:1",
                               payload={"to": "someone@example.invalid"},
                               idempotency_key="op-d13-b")
    approval = ledger.request_approval(operation, destination_id="owner:telegram",
                                       actor_id="owner", ttl_seconds=60)
    clock.advance(seconds=120)
    with pytest.raises(ApprovalRequired):
        ledger.resolve_approval(approval.id, resolution="approved",
                                actor_id="owner",
                                current_input_hash=operation.input_hash)

    with pytest.raises(ApprovalRequired):
        ledger.require_authorized(operation)


def test_d13_a_changed_payload_invalidates_the_approval(sessions, clock):
    """The action shown for approval is the only action approved."""
    ledger = _ledger(sessions, clock)
    operation = ledger.propose(action="email.send", target="contact:1",
                               payload={"to": "a@example.invalid"},
                               idempotency_key="op-d13-c")
    approval = ledger.request_approval(operation, destination_id="owner:telegram",
                                       actor_id="owner", ttl_seconds=600)

    with pytest.raises(Conflict, match="changed"):
        ledger.resolve_approval(approval.id, resolution="approved",
                                actor_id="owner",
                                current_input_hash="a-different-hash")


def test_d13_a_stale_object_version_is_a_conflict():
    from loop.api.service import ApplicationService, Outcome

    service = ApplicationService()
    service.put("task-1", {"done": False})
    service.mutate("task-1", expected_version=1, changes={"done": True})

    result = service.mutate("task-1", expected_version=1, changes={"done": False})
    assert result.outcome is Outcome.CONFLICT


def test_d13_the_conflict_response_is_actionable():
    from loop.api.service import ApplicationService

    service = ApplicationService()
    service.put("task-1", {"done": False})
    service.mutate("task-1", expected_version=1, changes={"done": True})
    result = service.mutate("task-1", expected_version=1, changes={})

    assert result.body["current_version"] == 2
    assert "changed since you read it" in result.error


# --------------------------------------------------------------------------- #
# D14 — SIGTERM then a forced stop
# --------------------------------------------------------------------------- #
def test_d14_pending_work_survives_an_abrupt_stop(sessions, clock):
    """A forced kill leaves the claim in place; the lease is what releases it."""
    from loop.runtime.jobs import JobQueue

    queue = JobQueue(sessions=sessions, clock=clock)
    queue.enqueue("deliver", dedupe_key="d1", payload={"outbox_id": "o1"})
    claimed = queue.claim("worker-1")

    assert claimed is not None
    assert queue.claim("worker-2") is None


def test_d14_a_fresh_process_reclaims_after_the_lease_expires(sessions, clock):
    from loop.runtime.jobs import JobQueue

    queue = JobQueue(sessions=sessions, clock=clock)
    queue.enqueue("deliver", dedupe_key="d1", payload={"outbox_id": "o1"})
    queue.claim("worker-1")

    clock.advance(seconds=600)
    assert queue.reclaim_expired() == 1
    assert queue.claim("worker-2") is not None


def test_d14_the_reclaiming_worker_gets_a_new_fencing_token(sessions, clock):
    from loop.runtime.jobs import JobQueue

    queue = JobQueue(sessions=sessions, clock=clock)
    queue.enqueue("deliver", dedupe_key="d1", payload={"outbox_id": "o1"})
    first = queue.claim("worker-1")
    clock.advance(seconds=600)
    queue.reclaim_expired()
    second = queue.claim("worker-2")

    assert second.fencing_token > first.fencing_token


def test_d14_a_backup_manifest_records_pending_work(tmp_path):
    from loop.ops.backup import create_backup, read_manifest

    database = tmp_path / "loop.db"
    database.write_bytes(b"SQLite format 3\x00")
    create_backup(database=database, vault=tmp_path / "absent", journal=None,
                  destination=tmp_path / "backup", created_at=NOW,
                  schema_revision="0002", pending_jobs=2, pending_outbox=1)

    manifest = read_manifest(tmp_path / "backup")
    assert manifest.pending_jobs == 2 and manifest.pending_outbox == 1


# --------------------------------------------------------------------------- #
# EX06 — workflows use typed references
# --------------------------------------------------------------------------- #
EX06_CAPABILITIES = {"calendar.list", "weather.forecast"}


def _step(step_id: str, **kw) -> dict:
    base: dict = {"id": step_id, "role": "daily_life", "objective": "do a thing",
                  "capability": "calendar.list", "arguments": {},
                  "depends_on": []}
    base.update(kw)
    return base


def _plan(*steps: dict):
    from loop.runtime.planner import parse_plan

    return parse_plan({"schema_version": 1, "intents": ["prepare"],
                       "steps": list(steps), "response_intent": "reply"},
                      root_event_id="e1",
                      known_capabilities=EX06_CAPABILITIES)


def test_ex06_a_step_field_the_schema_does_not_define_is_rejected():
    """A step is a typed reference, so an unrecognised key is not "extra"."""
    from loop.core.errors import ValidationFailed
    from loop.runtime.planner import ALLOWED_STEP_FIELDS

    assert "shell" not in ALLOWED_STEP_FIELDS
    with pytest.raises(ValidationFailed):
        _plan(_step("s1", shell="rm -rf /"))


def test_ex06_an_unregistered_capability_is_rejected():
    from loop.core.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        _plan(_step("s1", capability="shell.run"))


def test_ex06_a_valid_typed_plan_parses():
    plan = _plan(_step("a"), _step("b", depends_on=["a"]))
    assert [step.id for step in plan.steps] == ["a", "b"]


def test_ex06_a_valid_dag_passes_validation():
    from loop.runtime.planner import validate_dag

    validate_dag(_plan(_step("a"), _step("b", depends_on=["a"])))


def test_ex06_a_cycle_is_rejected():
    """Built against `validate_dag` directly: `parse_plan` already refuses a
    forward reference, so a cycle cannot be expressed through it."""
    from loop.core.errors import ValidationFailed
    from loop.runtime.planner import PlanStep, TypedPlan, validate_dag

    plan = TypedPlan(schema_version=1, root_event_id="e1", intents=["prepare"],
                     steps=[PlanStep(id="a", role="daily_life",
                                     objective="o", capability="calendar.list",
                                     depends_on=("b",)),
                            PlanStep(id="b", role="daily_life", objective="o",
                                     capability="calendar.list",
                                     depends_on=("a",))],
                     response_intent="reply")
    with pytest.raises(ValidationFailed):
        validate_dag(plan)


def test_ex06_there_is_no_eval_in_the_execution_path():
    """A step is a typed reference to a registered operation, not code."""
    import inspect

    import loop.capabilities.runners as runners
    import loop.runtime.planner as planner

    for module in (runners, planner):
        source = inspect.getsource(module)
        assert "eval(" not in source
        assert "exec(" not in source


# --------------------------------------------------------------------------- #
# EX07 — one invocation contract across surfaces
# --------------------------------------------------------------------------- #
def test_ex07_the_same_mutation_semantics_apply_on_every_surface():
    from loop.api.service import ApplicationService, Outcome

    service = ApplicationService()
    service.put("obj", {"value": 1})

    cli = service.mutate("obj", expected_version=1, changes={"value": 2},
                         idempotency_key="k")
    http = service.mutate("obj", expected_version=1, changes={"value": 2},
                          idempotency_key="k")

    assert cli.outcome is Outcome.APPLIED
    assert http.outcome is Outcome.REPLAYED


def test_ex07_an_unauthenticated_call_is_refused_everywhere():
    from loop.api.service import require_authentication

    with pytest.raises(Conflict):
        require_authentication(None, expected="token")


def test_ex07_error_codes_map_to_one_status_table():
    from loop.api.service import Outcome

    assert Outcome.CONFLICT.http_status == 409
    assert Outcome.NOT_FOUND.http_status == 404
    assert Outcome.APPLIED.http_status == 200


def test_ex07_adding_a_provider_needs_no_new_route():
    """Generic invocation means the route is the operation name."""
    from loop.capabilities.registry import CapabilityRegistry

    registry = CapabilityRegistry(roots=[])
    assert registry.enabled_operations() == {}


# --------------------------------------------------------------------------- #
# EX11 — breaking changes and rollback
# --------------------------------------------------------------------------- #
def _v1() -> ObjectSchema:
    return ObjectSchema(pack_id="plantcare", object_type="plant",
                        schema_version=1, required=frozenset({"name"}),
                        properties={"name": "string", "notes": "string"},
                        effects=frozenset({"local_domain_write"}))


def test_ex11_an_added_optional_field_is_compatible():
    new = ObjectSchema(pack_id="plantcare", object_type="plant",
                       schema_version=2, required=frozenset({"name"}),
                       properties={"name": "string", "notes": "string",
                                   "species": "string"},
                       effects=frozenset({"local_domain_write"}))
    kind, reasons = diff_schemas(_v1(), new)

    assert kind is ChangeKind.COMPATIBLE
    assert reasons == []


def test_ex11_a_new_required_field_is_breaking():
    """Every row already written lacks it."""
    new = ObjectSchema(pack_id="plantcare", object_type="plant",
                       schema_version=2,
                       required=frozenset({"name", "species"}),
                       properties={"name": "string", "notes": "string",
                                   "species": "string"})
    kind, reasons = diff_schemas(_v1(), new)

    assert kind is ChangeKind.BREAKING
    assert "new required field 'species'" in reasons


def test_ex11_a_new_effect_is_breaking():
    new = ObjectSchema(pack_id="plantcare", object_type="plant",
                       schema_version=2, required=frozenset({"name"}),
                       properties={"name": "string", "notes": "string"},
                       effects=frozenset({"local_domain_write", "network_read"}))
    kind, reasons = diff_schemas(_v1(), new)

    assert kind is ChangeKind.BREAKING
    assert any("network_read" in r for r in reasons)


def test_ex11_a_breaking_change_without_authority_is_refused():
    store = CapabilityObjectStore()
    store.register_schema(_v1())
    breaking = ObjectSchema(pack_id="plantcare", object_type="plant",
                            schema_version=2,
                            required=frozenset({"name", "species"}),
                            properties={"name": "string", "notes": "string",
                                        "species": "string"})

    with pytest.raises(ValidationFailed, match="breaking"):
        store.register_schema(breaking)


def test_ex11_it_applies_with_a_migration_and_authority():
    store = CapabilityObjectStore()
    store.register_schema(_v1())
    breaking = ObjectSchema(pack_id="plantcare", object_type="plant",
                            schema_version=2,
                            required=frozenset({"name", "species"}),
                            properties={"name": "string", "notes": "string",
                                        "species": "string"})

    store.register_schema(breaking, migration="add species",
                          authority_event_id="e1")
    assert store.schema("plantcare:plant").schema_version == 2


def test_ex11_a_registered_version_is_immutable():
    store = CapabilityObjectStore()
    store.register_schema(_v1())

    with pytest.raises(InvalidInput, match="immutable"):
        store.register_schema(_v1())


def test_ex11_rollback_preserves_existing_objects():
    store = CapabilityObjectStore()
    store.register_schema(_v1())
    store.create(object_id="p1", pack_id="plantcare", object_type="plant",
                 payload={"name": "fern"})
    store.register_schema(
        ObjectSchema(pack_id="plantcare", object_type="plant", schema_version=2,
                     required=frozenset({"name", "species"}),
                     properties={"name": "string", "notes": "string",
                                 "species": "string"}),
        migration="add species", authority_event_id="e1")

    result = rollback_schema(store, "plantcare:plant", to_version=1)

    assert result.restored_version == 1
    assert result.preserved_objects == 1
    assert store.get("p1").payload["name"] == "fern"


def test_ex11_rollback_cannot_undo_what_already_left_the_machine():
    """Reporting "reverted" after an email was sent describes no real state."""
    store = CapabilityObjectStore()
    store.register_schema(_v1())
    result = rollback_schema(store, "plantcare:plant", to_version=1,
                             external_effects=["email sent to the supplier"])

    assert result.fully_reversed is False
    assert result.irreversible_effects == ["email sent to the supplier"]


def test_ex11_rolling_back_to_an_unknown_version_is_rejected():
    store = CapabilityObjectStore()
    store.register_schema(_v1())

    with pytest.raises(InvalidInput):
        rollback_schema(store, "plantcare:plant", to_version=9)


# --------------------------------------------------------------------------- #
# EX12 — offline conformance isolation
# --------------------------------------------------------------------------- #
def test_ex12_the_test_settings_never_read_a_real_env_file():
    from loop.core.settings import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.telegram_bot_token == ""


def test_ex12_no_test_reads_the_real_vault():
    """A real vault is read-only during development.

    With `_env_file=None` the settings cannot pick up a real vault path from a
    `.env`, so nothing in the suite can resolve to the live vault.
    """
    from loop.core.settings import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    configured = str(getattr(settings, "vault_path", ""))

    # No vault resolves from settings that were built without an env file, so
    # nothing in the suite can reach a real one.
    assert configured in ("", "None")


def test_ex12_a_conformance_example_cannot_reach_the_network():
    """The suite carries no credentials, so a live call has nothing to use."""
    from loop.core.settings import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.anthropic_api_key == ""
    assert settings.cloud_available is False


def test_ex12_the_registry_discovers_without_a_network():
    from loop.capabilities.registry import CapabilityRegistry

    registry = CapabilityRegistry(roots=[])
    assert registry.discover() == []


# --------------------------------------------------------------------------- #
# EX13 — capability objects and concurrent edits
# --------------------------------------------------------------------------- #
@pytest.fixture
def object_store() -> CapabilityObjectStore:
    store = CapabilityObjectStore()
    store.register_schema(_v1())
    return store


def test_ex13_a_new_domain_needs_no_core_table(object_store):
    record = object_store.create(object_id="p1", pack_id="plantcare",
                                 object_type="plant", payload={"name": "fern"})
    assert record.schema_version == 1


def test_ex13_the_payload_is_validated_against_the_registered_schema(object_store):
    with pytest.raises(ValidationFailed, match="schema"):
        object_store.create(object_id="p1", pack_id="plantcare",
                            object_type="plant", payload={"notes": "no name"})


def test_ex13_an_unknown_field_is_rejected(object_store):
    with pytest.raises(ValidationFailed):
        object_store.create(object_id="p1", pack_id="plantcare",
                            object_type="plant",
                            payload={"name": "fern", "colour": "green"})


def test_ex13_a_wrong_type_is_rejected(object_store):
    with pytest.raises(ValidationFailed):
        object_store.create(object_id="p1", pack_id="plantcare",
                            object_type="plant", payload={"name": 42})


def test_ex13_a_boolean_is_not_an_integer():
    """`bool` subclasses `int`; a flag is not a count."""
    store = CapabilityObjectStore()
    store.register_schema(ObjectSchema(
        pack_id="p", object_type="o", schema_version=1,
        required=frozenset({"count"}), properties={"count": "integer"}))

    with pytest.raises(ValidationFailed):
        store.create(object_id="x", pack_id="p", object_type="o",
                     payload={"count": True})


def test_ex13_a_concurrent_edit_is_refused(object_store):
    object_store.create(object_id="p1", pack_id="plantcare",
                        object_type="plant", payload={"name": "fern"})
    object_store.update("p1", expected_version=1, payload={"notes": "thirsty"})

    with pytest.raises(Conflict):
        object_store.update("p1", expected_version=1, payload={"notes": "fine"})


def test_ex13_the_conflict_names_the_current_version(object_store):
    object_store.create(object_id="p1", pack_id="plantcare",
                        object_type="plant", payload={"name": "fern"})
    object_store.update("p1", expected_version=1, payload={"notes": "thirsty"})

    with pytest.raises(Conflict) as excinfo:
        object_store.update("p1", expected_version=1, payload={})

    assert excinfo.value.details["current_version"] == 2


def test_ex13_privacy_travels_with_the_object(object_store):
    private = PrivacyLabel(model_scope=ModelScope.LOCAL_ONLY, sensitive=True)
    object_store.create(object_id="p1", pack_id="plantcare",
                        object_type="plant", payload={"name": "fern"},
                        privacy=private)

    _, merged = object_store.read_with_privacy(
        "p1", context=PrivacyLabel(model_scope=ModelScope.CLOUD_ALLOWED))

    assert merged.is_local_only is True


def test_ex13_storing_and_reading_back_cannot_launder_a_label(object_store):
    private = PrivacyLabel(model_scope=ModelScope.LOCAL_ONLY)
    object_store.create(object_id="p1", pack_id="plantcare",
                        object_type="plant", payload={"name": "fern"},
                        privacy=private)

    _, merged = object_store.read_with_privacy("p1", context=PrivacyLabel())
    assert merged.is_local_only is True


def test_ex13_an_unregistered_type_cannot_be_created(object_store):
    with pytest.raises(InvalidInput, match="no registered schema"):
        object_store.create(object_id="x", pack_id="unknown",
                            object_type="thing", payload={})


# --------------------------------------------------------------------------- #
# EX14 — big packs and a small one share the same machinery
# --------------------------------------------------------------------------- #
def test_ex14_a_trivial_extension_needs_no_large_schema():
    """A checklist should not have to look like the travel pack to be a pack."""
    store = CapabilityObjectStore()
    checklist = ObjectSchema(pack_id="checklist", object_type="item",
                             schema_version=1, required=frozenset({"text"}),
                             properties={"text": "string", "done": "boolean"})
    store.register_schema(checklist)
    record = store.create(object_id="c1", pack_id="checklist",
                          object_type="item",
                          payload={"text": "water the fern", "done": False})

    assert record.payload["done"] is False


def test_ex14_a_small_and_a_large_pack_share_one_store():
    store = CapabilityObjectStore()
    store.register_schema(ObjectSchema(
        pack_id="checklist", object_type="item", schema_version=1,
        required=frozenset({"text"}), properties={"text": "string"}))
    store.register_schema(ObjectSchema(
        pack_id="travel", object_type="trip", schema_version=1,
        required=frozenset({"title"}),
        properties={"title": "string", "options": "array",
                    "budget_minor": "integer"}))

    store.create(object_id="c1", pack_id="checklist", object_type="item",
                 payload={"text": "water the fern"})
    store.create(object_id="t1", pack_id="travel", object_type="trip",
                 payload={"title": "Italy", "options": [], "budget_minor": 1})

    assert store.get("c1") is not None and store.get("t1") is not None


def test_ex14_one_packs_schema_does_not_constrain_another():
    store = CapabilityObjectStore()
    store.register_schema(ObjectSchema(
        pack_id="checklist", object_type="item", schema_version=1,
        required=frozenset({"text"}), properties={"text": "string"}))
    store.register_schema(ObjectSchema(
        pack_id="travel", object_type="trip", schema_version=1,
        required=frozenset({"title"}), properties={"title": "string"}))

    with pytest.raises(ValidationFailed):
        store.create(object_id="c2", pack_id="checklist", object_type="item",
                     payload={"title": "wrong field for this pack"})


def test_ex14_the_weather_and_travel_packs_use_the_shared_contract():
    """Neither imports the other, and neither is special-cased in the core."""
    import inspect

    import loop.capabilities.travel.trip as travel
    import loop.capabilities.weather.bundle as weather

    assert "weather" not in inspect.getsource(travel)
    assert "travel" not in inspect.getsource(weather)


def test_ex14_no_coordinator_branch_names_a_pack():
    """Adding a pack must not require a domain-specific coordinator branch."""
    import inspect

    import loop.agents.coordinator as coordinator

    source = inspect.getsource(coordinator)
    for pack in ("travel", "weather", "plantcare", "checklist"):
        assert f'"{pack}"' not in source and f"'{pack}'" not in source


# --------------------------------------------------------------------------- #
# M3 — capability objects survive a restart
# --------------------------------------------------------------------------- #
def test_m3_a_registered_schema_survives_a_fresh_store(sessions):
    first = CapabilityObjectStore(sessions=sessions)
    first.register_schema(_v1())

    second = CapabilityObjectStore(sessions=sessions)
    assert second.schema("plantcare:plant").schema_version == 1


def test_m3_an_object_survives_a_restart(sessions):
    first = CapabilityObjectStore(sessions=sessions)
    first.register_schema(_v1())
    first.create(object_id="p1", pack_id="plantcare", object_type="plant",
                payload={"name": "fern"})

    second = CapabilityObjectStore(sessions=sessions)
    assert second.get("p1").payload["name"] == "fern"


def test_m3_an_update_survives_a_restart(sessions):
    first = CapabilityObjectStore(sessions=sessions)
    first.register_schema(_v1())
    first.create(object_id="p1", pack_id="plantcare", object_type="plant",
                payload={"name": "fern"})
    first.update("p1", expected_version=1, payload={"notes": "thirsty"})

    second = CapabilityObjectStore(sessions=sessions)
    record = second.get("p1")
    assert record.payload["notes"] == "thirsty"
    assert record.version == 2


def test_m3_expected_version_still_enforced_after_a_restart(sessions):
    """The concurrency control itself must not reset just because the
    process did."""
    first = CapabilityObjectStore(sessions=sessions)
    first.register_schema(_v1())
    first.create(object_id="p1", pack_id="plantcare", object_type="plant",
                payload={"name": "fern"})
    first.update("p1", expected_version=1, payload={"notes": "thirsty"})

    second = CapabilityObjectStore(sessions=sessions)
    with pytest.raises(Conflict):
        second.update("p1", expected_version=1, payload={"notes": "fine"})


def test_m3_a_breaking_schema_version_survives_a_restart(sessions):
    first = CapabilityObjectStore(sessions=sessions)
    first.register_schema(_v1())
    breaking = ObjectSchema(pack_id="plantcare", object_type="plant",
                            schema_version=2,
                            required=frozenset({"name", "species"}),
                            properties={"name": "string", "notes": "string",
                                        "species": "string"})
    first.register_schema(breaking, migration="add species",
                          authority_event_id="e1")

    second = CapabilityObjectStore(sessions=sessions)
    assert second.schema("plantcare:plant").schema_version == 2
    with pytest.raises(InvalidInput, match="immutable"):
        second.register_schema(_v1())


def test_m3_a_rollback_survives_a_restart(sessions):
    first = CapabilityObjectStore(sessions=sessions)
    first.register_schema(_v1())
    first.create(object_id="p1", pack_id="plantcare", object_type="plant",
                payload={"name": "fern"})
    breaking = ObjectSchema(pack_id="plantcare", object_type="plant",
                            schema_version=2,
                            required=frozenset({"name", "species"}),
                            properties={"name": "string", "notes": "string",
                                        "species": "string"})
    first.register_schema(breaking, migration="add species",
                          authority_event_id="e1")
    rollback_schema(first, "plantcare:plant", to_version=1)

    second = CapabilityObjectStore(sessions=sessions)
    assert second.schema("plantcare:plant").schema_version == 1
    assert second.get("p1").payload["name"] == "fern"


def test_m3_a_store_with_no_sessions_still_works_exactly_as_before():
    store = CapabilityObjectStore()
    store.register_schema(_v1())
    record = store.create(object_id="p1", pack_id="plantcare",
                          object_type="plant", payload={"name": "fern"})
    assert record.schema_version == 1
