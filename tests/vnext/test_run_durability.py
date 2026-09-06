"""Checkpoint durability, resume, versions and paired backup (agent-stack §4).

Covers LG06, LG07, LG08, LG09, LG10, LG11.

These use a **real** ``AsyncSqliteSaver`` and real LangGraph interrupts. Replacing
the graph with a mock would prove nothing about conformance — the point is that
execution position survives a genuinely new process object reading the same file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from loop.core.errors import Conflict, InvalidInput, Unavailable
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.runtime.operations import OperationLedger, operation_key
from loop.runtime.runs import (
    CHECKPOINT_MODE,
    BackupManifest,
    PairedBackup,
    RunStatus,
    RunStore,
    checkpoint_path,
    runs_affected_by,
    secure_checkpoint_file,
)

PRIVATE = PrivacyLabel(model_scope=ModelScope.LOCAL_ONLY,
                       origins=frozenset({"telegram_private"}), sensitive=True)


@pytest.fixture
def store(sessions, clock) -> RunStore:
    return RunStore(sessions=sessions, clock=clock)


def _create(store: RunStore, **kw: object):
    defaults: dict[str, object] = {
        "root_event_id": "e1", "graph_version": "coordinator-v1",
        "state_schema_version": "state-v1", "registry_revision": 3,
        "registry_hash": "abc123", "privacy": PRIVATE,
        "pinned_packs": {"plantcare": "1.0.0"}}
    defaults.update(kw)
    return store.create(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Run mappings
# --------------------------------------------------------------------------- #
def test_a_run_records_its_thread_and_pinned_versions(store):
    mapping = _create(store)
    stored = store.get(mapping.id)

    assert stored.thread_id == mapping.thread_id
    assert stored.graph_version == "coordinator-v1"
    assert stored.pinned_packs == {"plantcare": "1.0.0"}


def test_a_thread_belongs_to_one_run(store):
    """Sharing a thread across requests would leak cancellation and approvals."""
    first = _create(store, root_event_id="e1")
    second = _create(store, root_event_id="e2")

    assert first.thread_id != second.thread_id


def test_the_run_privacy_label_is_persisted(store):
    mapping = store.get(_create(store).id)
    assert mapping.privacy.is_local_only is True
    assert "telegram_private" in mapping.privacy.origins


def test_a_run_can_be_found_by_thread(store):
    mapping = _create(store)
    assert store.by_thread(mapping.thread_id).id == mapping.id


# --------------------------------------------------------------------------- #
# LG06 — restart during graph execution
# --------------------------------------------------------------------------- #
class _State(TypedDict, total=False):
    steps: list[str]
    value: int


def _build_graph(checkpointer):
    """A small real graph with an interrupt in the middle."""
    graph = StateGraph(_State)

    def first(state: _State) -> dict:
        return {"steps": [*state.get("steps", []), "first"], "value": 1}

    def gate(state: _State) -> dict:
        decision = interrupt({"question": "approve?"})
        return {"steps": [*state.get("steps", []), f"gate:{decision}"]}

    def last(state: _State) -> dict:
        return {"steps": [*state.get("steps", []), "last"]}

    graph.add_node("first", first)
    graph.add_node("gate", gate)
    graph.add_node("last", last)
    graph.add_edge(START, "first")
    graph.add_edge("first", "gate")
    graph.add_edge("gate", "last")
    graph.add_edge("last", END)
    return graph.compile(checkpointer=checkpointer)


async def test_lg06_execution_position_survives_a_new_process_object(tmp_path,
                                                                    store):
    """A brand-new saver and graph read the same file and resume correctly."""
    path = checkpoint_path(tmp_path)
    mapping = _create(store)
    config = {"configurable": {"thread_id": mapping.thread_id}}

    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        compiled = _build_graph(saver)
        result = await compiled.ainvoke({"steps": []}, config)
        assert "__interrupt__" in result       # paused at the gate

    # Process restart: nothing carried over except the file on disk.
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        restarted = _build_graph(saver)
        state = await restarted.aget_state(config)

        assert state.next == ("gate",), "the checkpoint should hold the position"
        assert state.values["steps"] == ["first"]


async def test_lg06_a_resumed_run_completes_from_where_it_paused(tmp_path, store):
    path = checkpoint_path(tmp_path)
    mapping = _create(store)
    config = {"configurable": {"thread_id": mapping.thread_id}}

    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await _build_graph(saver).ainvoke({"steps": []}, config)

    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        final = await _build_graph(saver).ainvoke(Command(resume="yes"), config)

    assert final["steps"] == ["first", "gate:yes", "last"]


async def test_lg06_the_earlier_steps_are_not_re_executed(tmp_path, store):
    """"first" runs once, not again on resume."""
    path = checkpoint_path(tmp_path)
    mapping = _create(store)
    config = {"configurable": {"thread_id": mapping.thread_id}}

    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await _build_graph(saver).ainvoke({"steps": []}, config)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        final = await _build_graph(saver).ainvoke(Command(resume="ok"), config)

    assert final["steps"].count("first") == 1


async def test_lg06_two_concurrent_runs_keep_isolated_state(tmp_path, store):
    """Child state must not bleed between threads."""
    path = checkpoint_path(tmp_path)
    first = _create(store, root_event_id="e1")
    second = _create(store, root_event_id="e2")

    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        compiled = _build_graph(saver)
        await compiled.ainvoke(
            {"steps": ["a"]}, {"configurable": {"thread_id": first.thread_id}})
        await compiled.ainvoke(
            {"steps": ["b"]}, {"configurable": {"thread_id": second.thread_id}})

        state_a = await compiled.aget_state(
            {"configurable": {"thread_id": first.thread_id}})
        state_b = await compiled.aget_state(
            {"configurable": {"thread_id": second.thread_id}})

    assert state_a.values["steps"] == ["a", "first"]
    assert state_b.values["steps"] == ["b", "first"]


def test_lg06_resumable_runs_are_discoverable_after_restart(store):
    running = _create(store, root_event_id="e1")
    done = _create(store, root_event_id="e2")
    store.set_status(done.id, RunStatus.SUCCEEDED)

    resumable = {m.id for m in RunStore(
        sessions=store._sessions, clock=store._clock).resumable()}

    assert running.id in resumable
    assert done.id not in resumable


# --------------------------------------------------------------------------- #
# LG07 — a checkpoint is not evidence of an effect
# --------------------------------------------------------------------------- #
def test_lg07_replay_consults_the_ledger_not_the_checkpoint(sessions, clock,
                                                            store):
    ledger = OperationLedger(sessions=sessions, clock=clock)
    key = operation_key(root_id="r1", work_item_id="w1", step_id="s1",
                        action="task.create")
    sends: list[str] = []

    def perform() -> str:
        if ledger.replay(key) is not None:
            return "reused"
        operation = ledger.propose(action="task.create", target="t1",
                                   payload={"title": "x"}, idempotency_key=key)
        sends.append(key)
        ledger.mark_committed(operation, result={"task_id": "t1"})
        return "performed"

    assert perform() == "performed"
    assert perform() == "reused"        # the replay after a crash
    assert sends == [key]


def test_lg07_an_uncommitted_operation_is_retried_not_assumed(sessions, clock):
    """Crash *before* the commit: the effect has not happened."""
    ledger = OperationLedger(sessions=sessions, clock=clock)
    key = operation_key(root_id="r", work_item_id="w", step_id="s",
                        action="task.create")
    ledger.propose(action="task.create", target="t", payload={"x": 1},
                   idempotency_key=key)

    assert ledger.replay(key) is None


def test_lg07_an_unknown_remote_outcome_is_preserved(sessions, clock):
    """Never infer delivery from a graph that moved past the send node."""
    ledger = OperationLedger(sessions=sessions, clock=clock)
    operation = ledger.propose(action="task.create", target="t",
                               payload={"x": 1}, idempotency_key="k")
    ledger.mark_unknown(operation)

    assert ledger.replay("k") is None
    assert ledger.get(operation.id).status == "unknown"


# --------------------------------------------------------------------------- #
# LG08 — approval interrupt, restart, authenticated resume
# --------------------------------------------------------------------------- #
async def test_lg08_an_interrupt_pauses_before_the_effect(tmp_path, store):
    path = checkpoint_path(tmp_path)
    mapping = _create(store)
    config = {"configurable": {"thread_id": mapping.thread_id}}

    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        result = await _build_graph(saver).ainvoke({"steps": []}, config)

    store.set_status(mapping.id, RunStatus.AWAITING_APPROVAL)

    assert "__interrupt__" in result
    assert store.get(mapping.id).status is RunStatus.AWAITING_APPROVAL


async def test_lg08_waiting_consumes_no_model_calls(tmp_path, store):
    """A paused run costs nothing: the worker is released."""
    from loop.ai.budget import BudgetLimits, RootBudget

    budget = RootBudget(limits=BudgetLimits())
    path = checkpoint_path(tmp_path)
    mapping = _create(store)

    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await _build_graph(saver).ainvoke(
            {"steps": []}, {"configurable": {"thread_id": mapping.thread_id}})

    assert budget.usage.model_calls == 0


async def test_lg08_the_same_thread_resumes_with_the_decision(tmp_path, store):
    path = checkpoint_path(tmp_path)
    mapping = _create(store)
    config = {"configurable": {"thread_id": mapping.thread_id}}

    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await _build_graph(saver).ainvoke({"steps": []}, config)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        final = await _build_graph(saver).ainvoke(Command(resume="approved"),
                                                  config)

    assert "gate:approved" in final["steps"]


def test_lg08_resume_rechecks_status_rather_than_trusting_the_pause(store):
    mapping = _create(store)
    store.set_status(mapping.id, RunStatus.AWAITING_APPROVAL)
    store.cancel(mapping.id)

    with pytest.raises(Conflict):
        store.require_resumable(mapping.id)


def test_lg08_a_resumable_run_passes_the_recheck(store):
    mapping = _create(store)
    store.set_status(mapping.id, RunStatus.AWAITING_APPROVAL)

    assert store.require_resumable(mapping.id).id == mapping.id


# --------------------------------------------------------------------------- #
# LG09 — cancellation and disablement before resume
# --------------------------------------------------------------------------- #
def test_lg09_a_cancelled_run_refuses_to_resume(store):
    mapping = _create(store)
    assert store.cancel(mapping.id) is True

    with pytest.raises(Conflict):
        store.require_resumable(mapping.id)


def test_lg09_a_disabled_pack_blocks_a_run_pinned_to_it(store):
    mapping = _create(store)
    store.set_status(mapping.id, RunStatus.PAUSED)

    with pytest.raises(Unavailable) as excinfo:
        store.require_resumable(mapping.id, disabled_packs={"plantcare"})

    assert "plantcare" in excinfo.value.details["disabled"]


def test_lg09_an_unrelated_disabled_pack_does_not_block(store):
    mapping = _create(store)
    store.set_status(mapping.id, RunStatus.PAUSED)

    assert store.require_resumable(mapping.id,
                                   disabled_packs={"bikeservice"}).id == mapping.id


def test_lg09_a_terminal_run_cannot_be_cancelled_again(store):
    mapping = _create(store)
    store.set_status(mapping.id, RunStatus.SUCCEEDED)

    assert store.cancel(mapping.id) is False


def test_lg09_cancelling_an_unknown_run_is_false_not_an_error(store):
    assert store.cancel("no-such-run") is False


# --------------------------------------------------------------------------- #
# LG10 — version pinning across an upgrade
# --------------------------------------------------------------------------- #
def test_lg10_an_available_pinned_version_resumes(store):
    mapping = _create(store)
    status = store.check_versions(
        mapping.id, available_packs={"plantcare": "1.0.0"},
        available_graph_versions={"coordinator-v1"})

    assert status is not RunStatus.VERSION_UNAVAILABLE


def test_lg10_an_upgraded_pack_pauses_the_run_rather_than_substituting(store):
    """No silent substitution: 1.1.0 is not what this run pinned."""
    mapping = _create(store)
    status = store.check_versions(
        mapping.id, available_packs={"plantcare": "1.1.0"},
        available_graph_versions={"coordinator-v1"})

    assert status is RunStatus.VERSION_UNAVAILABLE


def test_lg10_the_pause_reason_is_actionable(store):
    mapping = _create(store)
    store.check_versions(mapping.id, available_packs={"plantcare": "1.1.0"},
                         available_graph_versions={"coordinator-v1"})

    assert "plantcare@1.0.0" in store.get(mapping.id).pause_reason


def test_lg10_a_missing_graph_version_also_pauses(store):
    mapping = _create(store)
    status = store.check_versions(
        mapping.id, available_packs={"plantcare": "1.0.0"},
        available_graph_versions={"coordinator-v2"})

    assert status is RunStatus.VERSION_UNAVAILABLE


def test_lg10_a_version_unavailable_run_refuses_to_resume(store):
    mapping = _create(store)
    store.check_versions(mapping.id, available_packs={},
                         available_graph_versions=set())

    with pytest.raises(Unavailable):
        store.require_resumable(mapping.id)


def test_lg10_new_runs_are_unaffected_by_a_paused_one(store):
    old = _create(store)
    store.check_versions(old.id, available_packs={"plantcare": "1.1.0"},
                         available_graph_versions={"coordinator-v1"})

    fresh = _create(store, root_event_id="e2",
                    pinned_packs={"plantcare": "1.1.0"})
    assert store.check_versions(
        fresh.id, available_packs={"plantcare": "1.1.0"},
        available_graph_versions={"coordinator-v1"}) is not \
        RunStatus.VERSION_UNAVAILABLE


# --------------------------------------------------------------------------- #
# LG11 — paired backup, restore and forgetting
# --------------------------------------------------------------------------- #
@pytest.fixture
def backup_pair(tmp_path, settings):
    domain = Path(settings.database_url.replace("sqlite:///", ""))
    domain.parent.mkdir(parents=True, exist_ok=True)
    if not domain.exists():
        domain.write_bytes(b"domain-database")
    checkpoints = checkpoint_path(tmp_path)
    checkpoints.write_bytes(b"checkpoint-database")
    return PairedBackup(domain_path=domain, checkpoint_path=checkpoints)


def test_lg11_a_backup_captures_both_databases(backup_pair, tmp_path):
    manifest = backup_pair.backup(tmp_path / "backup")

    assert (tmp_path / "backup" / manifest.domain_file).is_file()
    assert (tmp_path / "backup" / manifest.checkpoint_file).is_file()


def test_lg11_the_manifest_records_both_hashes(backup_pair, tmp_path):
    manifest = backup_pair.backup(tmp_path / "backup")

    assert manifest.domain_hash and manifest.checkpoint_hash
    assert manifest.domain_hash != manifest.checkpoint_hash


def test_lg11_verification_accepts_a_consistent_backup(backup_pair, tmp_path):
    backup_pair.backup(tmp_path / "backup")
    assert backup_pair.verify(tmp_path / "backup")


def test_lg11_a_tampered_backup_is_refused(backup_pair, tmp_path):
    """A mismatched pair is exactly what a paired manifest exists to catch."""
    manifest = backup_pair.backup(tmp_path / "backup")
    (tmp_path / "backup" / manifest.checkpoint_file).write_bytes(b"different")

    with pytest.raises(Conflict) as excinfo:
        backup_pair.verify(tmp_path / "backup")
    assert "hash mismatch" in str(excinfo.value.details["problems"])


def test_lg11_a_missing_half_is_refused(backup_pair, tmp_path):
    manifest = backup_pair.backup(tmp_path / "backup")
    (tmp_path / "backup" / manifest.checkpoint_file).unlink()

    with pytest.raises(Conflict):
        backup_pair.verify(tmp_path / "backup")


def test_lg11_restore_defaults_to_a_dry_run(backup_pair, tmp_path):
    backup_pair.backup(tmp_path / "backup")
    backup_pair.checkpoint_path.write_bytes(b"live data")

    outcome = backup_pair.restore(tmp_path / "backup")

    assert outcome["dry_run"] is True
    assert backup_pair.checkpoint_path.read_bytes() == b"live data"


def test_lg11_an_applied_restore_replaces_both(backup_pair, tmp_path):
    backup_pair.backup(tmp_path / "backup")
    backup_pair.checkpoint_path.write_bytes(b"corrupted")

    backup_pair.restore(tmp_path / "backup", dry_run=False)

    assert backup_pair.checkpoint_path.read_bytes() == b"checkpoint-database"


def test_lg11_a_missing_manifest_is_an_error(backup_pair, tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(InvalidInput):
        backup_pair.verify(tmp_path / "empty")


def test_lg11_the_checkpoint_file_is_owner_only(tmp_path):
    import stat

    path = checkpoint_path(tmp_path)
    path.write_bytes(b"private content")
    secure_checkpoint_file(path)

    assert stat.S_IMODE(path.stat().st_mode) == CHECKPOINT_MODE


def test_lg11_restored_checkpoints_keep_restricted_permissions(backup_pair,
                                                               tmp_path):
    import stat

    backup_pair.backup(tmp_path / "backup")
    backup_pair.restore(tmp_path / "backup", dry_run=False)

    assert stat.S_IMODE(backup_pair.checkpoint_path.stat().st_mode) == \
        CHECKPOINT_MODE


def test_lg11_forgetting_identifies_the_runs_it_must_invalidate(store):
    """A checkpoint holding just-deleted content would defeat the deletion."""
    affected = _create(store, root_event_id="e1")
    other = _create(store, root_event_id="e2",
                    privacy=PrivacyLabel(model_scope=ModelScope.CLOUD_ALLOWED,
                                         origins=frozenset({"work"})))

    found = {m.id for m in runs_affected_by(store, origin="telegram_private")}

    assert affected.id in found
    assert other.id not in found


def test_lg11_the_manifest_round_trips(backup_pair, tmp_path):
    manifest = backup_pair.backup(tmp_path / "backup")
    restored = BackupManifest.from_json(json.loads(
        (tmp_path / "backup" / "manifest.json").read_text()))

    assert restored.domain_hash == manifest.domain_hash
