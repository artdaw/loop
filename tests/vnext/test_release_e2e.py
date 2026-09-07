"""M7 release-path checks: real restart, reminder delivery, and full restore."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from loop.app import build_application
from loop.core.clock import UTC, FrozenClock
from loop.core.settings import Settings
from loop.ops.backup import create_backup, restore_backup
from loop.runtime.jobs import JobQueue
from loop.runtime.runs import checkpoint_path

FIRES_AT = dt.datetime(2026, 9, 8, 7, 0, tzinfo=UTC)


def _environment(root: Path) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{root / 'loop.db'}",
        "DATA_DIR": str(root),
        "TIMEZONE": "Europe/Berlin",
        "CAPABILITY_PATHS": json.dumps([]),
        "TELEGRAM_CHAT_ID": "4242",
        "OBSIDIAN_VAULT_PATH": "",
        "PYTHONPATH": str(Path.cwd()),
    }


RUN_REMINDER = textwrap.dedent("""
    import datetime as dt, json, sys
    from typer.testing import CliRunner
    import loop.interfaces.cli as cli
    from loop.app import build_application
    from loop.core.clock import FrozenClock, UTC
    from loop.core.settings import Settings
    from loop.runtime.outbox import FakeTransport

    clock = FrozenClock(dt.datetime.fromisoformat(sys.argv[1]).astimezone(UTC))
    transport = FakeTransport()
    settings = Settings(_env_file=None)
    cli._app = lambda: build_application(
        settings, clock=clock, transports={"telegram": transport})
    result = CliRunner().invoke(cli.app, ["run", "once"])
    print(json.dumps({"exit": result.exit_code, "stdout": result.stdout,
                      "sent": transport.sent}))
""")


def _run_cli(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "loop.interfaces.cli", *arguments],
        cwd=Path.cwd(), env=_environment(root), capture_output=True,
        text=True, timeout=120)


def _restart_and_sweep(root: Path, script: Path) -> dict:
    script.write_text(RUN_REMINDER, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), FIRES_AT.isoformat()], cwd=Path.cwd(),
        env=_environment(root), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_task_restart_reminder_uses_the_shipped_entrypoint_and_fake_transport(
        tmp_path):
    """Nothing crosses the process boundary except durable files."""
    scheduled = _run_cli(
        tmp_path, "remind", "Call the repair shop", "--at",
        "2026-09-08T09:00", "--timezone", "Europe/Berlin")
    assert scheduled.returncode == 0, scheduled.stderr
    task_id = scheduled.stdout.split()[0]

    delivered = _restart_and_sweep(tmp_path, tmp_path / "restart.py")

    assert delivered["exit"] == 0
    assert "1 notification(s) sent" in delivered["stdout"]
    assert [item["payload"]["task_id"] for item in delivered["sent"]] == [task_id]


def test_completion_through_the_shipped_entrypoint_prevents_stale_delivery(tmp_path):
    scheduled = _run_cli(
        tmp_path, "remind", "Do not send after completion", "--at",
        "2026-09-08T09:00", "--timezone", "Europe/Berlin")
    task_id = scheduled.stdout.split()[0]
    completed = _run_cli(tmp_path, "task", "complete", task_id, "1")
    assert completed.returncode == 0, completed.stderr

    swept = _restart_and_sweep(tmp_path, tmp_path / "restart-complete.py")

    assert swept["sent"] == []
    assert "0 notification(s) sent" in swept["stdout"]


def test_daemon_releases_its_durable_leader_lease_on_sigterm(tmp_path):
    process = subprocess.Popen(
        [sys.executable, "-m", "loop.interfaces.cli", "run", "daemon",
         "--interval-seconds", "0.05"], cwd=Path.cwd(),
        env=_environment(tmp_path), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    database = tmp_path / "loop.db"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if database.exists():
            try:
                with sqlite3.connect(database) as connection:
                    row = connection.execute(
                        "SELECT COUNT(*) FROM leader_leases").fetchone()
                if row == (1,):
                    break
            except sqlite3.OperationalError:
                pass
        time.sleep(0.05)
    else:
        process.kill()
        pytest.fail("daemon did not acquire its leader lease")

    process.terminate()
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stdout + stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM leader_leases").fetchone() == (0,)


def test_trigger_dispatch_rolls_back_as_one_unit_when_queueing_fails(tmp_path):
    settings = Settings(
        _env_file=None, environment="test",
        database_url=f"sqlite:///{tmp_path / 'loop.db'}", data_dir=str(tmp_path),
        capability_paths="[]", telegram_chat_id="4242")
    before = FrozenClock(FIRES_AT - dt.timedelta(hours=1))
    application = build_application(settings, clock=before)
    reminder = application.reminders.schedule(
        "Still due after a crash", instant=FIRES_AT, timezone="Europe/Berlin")

    def fail(*args, **kwargs):
        raise RuntimeError("simulated process failure before queue commit")

    restarted = build_application(settings, clock=FrozenClock(FIRES_AT))
    restarted.service._on_trigger = fail
    with pytest.raises(RuntimeError, match="simulated process failure"):
        restarted.service.tick()
    restarted.service.leader.release()

    recovered = build_application(settings, clock=FrozenClock(FIRES_AT))
    assert [t.id for t in recovered.triggers.due_triggers()] == [reminder.trigger.id]
    report = recovered.service.tick()
    assert report.triggers_fired == 1
    assert report.notifications_failed == 1
    with recovered.sessions() as session:
        from sqlalchemy import text
        assert session.execute(text(
            "SELECT COUNT(*) FROM notifications WHERE subject_ref = :subject"),
            {"subject": f"task:{reminder.task.id}"}).scalar() == 1


def test_domain_checkpoint_and_vault_restore_as_one_verified_backup(tmp_path):
    source = tmp_path / "source"
    vault = source / "vault"
    (vault / "1-wiki").mkdir(parents=True)
    (vault / "1-wiki" / "fact.md").write_text("source-backed fact")
    settings = Settings(
        _env_file=None, environment="test",
        database_url=f"sqlite:///{source / 'loop.db'}", data_dir=str(source),
        obsidian_vault_path=str(vault), capability_paths="[]")
    application = build_application(settings)
    task = application.tasks.create("survive the restore")
    JobQueue(sessions=application.sessions).enqueue(
        "release.probe", dedupe_key="release-probe", payload={"task": task.id})

    checkpoints = checkpoint_path(source)
    with sqlite3.connect(checkpoints) as connection:
        connection.execute("CREATE TABLE checkpoint_probe (value TEXT)")
        connection.execute("INSERT INTO checkpoint_probe VALUES ('paused-run')")
        connection.commit()

    manifest = create_backup(
        database=source / "loop.db", checkpoints=checkpoints, vault=vault,
        journal=None, destination=tmp_path / "backup", created_at=1_780_000_000,
        schema_revision="0004", pending_jobs=1)
    result = restore_backup(tmp_path / "backup", target=tmp_path / "restored")

    assert {part.name for part in manifest.parts} == {
        "database", "checkpoints", "vault"}
    restored_settings = Settings(
        _env_file=None, environment="test",
        database_url=f"sqlite:///{result.database}",
        data_dir=str(tmp_path / "restored"),
        obsidian_vault_path=str(result.vault), capability_paths="[]")
    restored = build_application(restored_settings)
    assert restored.tasks.get(task.id).title == "survive the restore"
    assert restored.service.pending_summary()["jobs"] == 1
    assert (result.vault / "1-wiki" / "fact.md").read_text() == "source-backed fact"
    with sqlite3.connect(result.checkpoints) as connection:
        assert connection.execute(
            "SELECT value FROM checkpoint_probe").fetchone() == ("paused-run",)
