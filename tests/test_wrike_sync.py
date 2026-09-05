"""Wrike bidirectional sync.

One test per row of the conflict table in the Phase 4 design (§5.2). The policy
is *Wrike wins, Loop pushes new*: Wrike is a team tool, so a colleague's edit
there must never be silently reverted by one peer's laptop.
"""

from __future__ import annotations

from datetime import date

import pytest

from config.settings import Settings
from core.autonomy import ActionType, AutonomyGate, AutonomyLevel
from core.wrike_sync import WrikeSync
from integrations.wrike import WrikeClient, WrikeTask


class FakeWrikeClient:
    """In-memory stand-in for :class:`WrikeClient`."""

    def __init__(self, tasks: list[WrikeTask] | None = None) -> None:
        self.tasks = tasks or []
        self.created: list[dict] = []
        self.completed: list[str] = []
        self.configured = True

    async def list_tasks(self, **kwargs):
        return list(self.tasks)

    async def create_task(self, *, title, due=None, folder_id=None):
        self.created.append({"title": title, "due": due})
        task = WrikeTask(task_id=f"W{len(self.created)}", title=title, due=due)
        self.tasks.append(task)
        return task

    async def complete_task(self, task_id):
        self.completed.append(task_id)
        for task in self.tasks:
            if task.task_id == task_id:
                task.status = "Completed"
                return task
        return WrikeTask(task_id=task_id, title="", status="Completed")

    async def update_task(self, task_id, **kwargs):
        return WrikeTask(task_id=task_id, title=kwargs.get("title", ""))


class FailingCreateClient(FakeWrikeClient):
    """Fails the first create, succeeds afterwards."""

    def __init__(self) -> None:
        super().__init__()
        self._calls = 0

    async def create_task(self, *, title, due=None, folder_id=None):
        self._calls += 1
        if self._calls == 1:
            raise RuntimeError("wrike exploded")
        return await super().create_task(title=title, due=due)


@pytest.fixture
def act_gate(settings, memory_store) -> AutonomyGate:
    """A gate that permits autonomous Wrike writes."""
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(ActionType.WRIKE_WRITE, AutonomyLevel.ACT)
    return gate


@pytest.fixture
def approve_gate(settings, memory_store) -> AutonomyGate:
    """A gate at the default level, which requires approval."""
    return AutonomyGate(settings, memory_store)


def _sync(memory_store, client, gate, settings) -> WrikeSync:
    return WrikeSync(memory=memory_store, client=client, gate=gate, settings=settings)


# --------------------------------------------------------------------------- #
# Not configured
# --------------------------------------------------------------------------- #
async def test_unconfigured_returns_a_report_and_never_raises(memory_store, settings):
    client = WrikeClient(Settings(wrike_api_key=""))
    report = await _sync(memory_store, client, None, settings).sync()

    assert report.configured is False
    assert report.errors == []
    assert report.pulled_new == 0


# --------------------------------------------------------------------------- #
# Pull: remote wins
# --------------------------------------------------------------------------- #
async def test_remote_only_task_is_created_locally(memory_store, act_gate, settings):
    client = FakeWrikeClient([WrikeTask(task_id="W1", title="from wrike",
                                        due=date(2026, 9, 30))])

    report = await _sync(memory_store, client, act_gate, settings).sync()

    assert report.pulled_new == 1
    task = memory_store.list_open_tasks()[0]
    assert task.description == "from wrike"
    assert task.source == "wrike"
    assert task.wrike_id == "W1"
    assert task.due_date == date(2026, 9, 30)


async def test_remote_wins_for_a_linked_task(memory_store, act_gate, settings):
    task = memory_store.add_task("old title", wrike_id="W1")
    client = FakeWrikeClient([WrikeTask(task_id="W1", title="new title")])

    report = await _sync(memory_store, client, act_gate, settings).sync()

    assert report.pulled_updated == 1
    assert memory_store.get_task(task.id).description == "new title"


async def test_unchanged_linked_task_is_not_counted_as_updated(memory_store, act_gate,
                                                               settings):
    memory_store.add_task("same title", wrike_id="W1")
    client = FakeWrikeClient([WrikeTask(task_id="W1", title="same title")])

    report = await _sync(memory_store, client, act_gate, settings).sync()

    assert report.pulled_updated == 0


async def test_completed_in_wrike_completes_locally(memory_store, act_gate, settings):
    task = memory_store.add_task("finish me", wrike_id="W1")
    client = FakeWrikeClient([WrikeTask(task_id="W1", title="finish me",
                                        status="Completed")])

    await _sync(memory_store, client, act_gate, settings).sync()

    assert memory_store.get_task(task.id).status == "done"


async def test_deleted_in_wrike_orphans_rather_than_deletes(memory_store, act_gate,
                                                            settings):
    task = memory_store.add_task("gone from wrike", wrike_id="W9")
    client = FakeWrikeClient([])

    report = await _sync(memory_store, client, act_gate, settings).sync()

    assert report.orphaned == 1
    kept = memory_store.get_task(task.id)
    assert kept is not None
    assert kept.status == "orphaned"


async def test_orphaned_tasks_leave_the_open_list(memory_store, act_gate, settings):
    memory_store.add_task("gone", wrike_id="W9")
    await _sync(memory_store, FakeWrikeClient([]), act_gate, settings).sync()

    assert memory_store.list_open_tasks() == []


# --------------------------------------------------------------------------- #
# Push: Loop sends new work up
# --------------------------------------------------------------------------- #
async def test_local_only_task_is_pushed(memory_store, act_gate, settings):
    memory_store.add_task("local task")
    client = FakeWrikeClient()

    report = await _sync(memory_store, client, act_gate, settings).sync()

    assert report.pushed_new == 1
    assert client.created[0]["title"] == "local task"


async def test_pushed_task_records_its_wrike_id(memory_store, act_gate, settings):
    task = memory_store.add_task("local task")
    await _sync(memory_store, FakeWrikeClient(), act_gate, settings).sync()

    stored = memory_store.get_task(task.id)
    assert stored.wrike_id == "W1"
    assert stored.last_synced_at is not None


async def test_tasks_that_came_from_wrike_are_not_pushed_back(memory_store, act_gate,
                                                              settings):
    memory_store.add_task("mirrored", source="wrike", wrike_id="W1")
    client = FakeWrikeClient([WrikeTask(task_id="W1", title="mirrored")])

    report = await _sync(memory_store, client, act_gate, settings).sync()

    assert report.pushed_new == 0
    assert client.created == []


async def test_local_completion_is_pushed(memory_store, act_gate, settings):
    task = memory_store.add_task("do it", wrike_id="W1")
    memory_store.complete_task(task.id)
    client = FakeWrikeClient([WrikeTask(task_id="W1", title="do it", status="Active")])

    report = await _sync(memory_store, client, act_gate, settings).sync()

    assert report.pushed_completed == 1
    assert client.completed == ["W1"]


# --------------------------------------------------------------------------- #
# Autonomy gating
# --------------------------------------------------------------------------- #
async def test_push_is_blocked_when_approval_is_required(memory_store, approve_gate,
                                                         settings):
    memory_store.add_task("local task")
    client = FakeWrikeClient()

    report = await _sync(memory_store, client, approve_gate, settings).sync()

    assert report.pushed_new == 0
    assert client.created == []
    assert report.push_blocked is True


async def test_pull_still_runs_when_push_is_blocked(memory_store, approve_gate, settings):
    """Reading from Wrike is not an action that needs approval."""
    client = FakeWrikeClient([WrikeTask(task_id="W1", title="from wrike")])

    report = await _sync(memory_store, client, approve_gate, settings).sync()

    assert report.pulled_new == 1


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #
async def test_dry_run_writes_nothing_anywhere(memory_store, act_gate, settings):
    memory_store.add_task("local task")
    client = FakeWrikeClient([WrikeTask(task_id="W1", title="from wrike")])

    report = await _sync(memory_store, client, act_gate, settings).sync(dry_run=True)

    assert report.dry_run is True
    assert client.created == []
    assert len(memory_store.list_open_tasks()) == 1  # nothing pulled in


async def test_per_task_error_is_collected_and_the_run_continues(memory_store, act_gate,
                                                                 settings):
    memory_store.add_task("first")
    memory_store.add_task("second")
    client = FailingCreateClient()

    report = await _sync(memory_store, client, act_gate, settings).sync()

    assert len(report.errors) == 1
    assert report.pushed_new == 1  # the second one still went through


async def test_list_failure_is_reported_not_raised(memory_store, act_gate, settings):
    class BrokenClient(FakeWrikeClient):
        async def list_tasks(self, **kwargs):
            raise RuntimeError("network down")

    report = await _sync(memory_store, BrokenClient(), act_gate, settings).sync()

    assert report.errors
    assert report.configured is True


def test_report_summary_is_human_readable(memory_store, settings):
    from core.wrike_sync import SyncReport

    text = SyncReport(configured=False).summary()
    assert "not configured" in text.lower()

    text = SyncReport(configured=True, pulled_new=2, pushed_new=1).summary()
    assert "2" in text and "1" in text
