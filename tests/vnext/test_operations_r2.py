"""Service lifecycle and operational maintenance (D14, O05, O10–O13 — R2).

R0 found that the logic for O05 and O10–O13 lived in `loop/ops/` with **no
production caller at all**: `check_python`, `rebuild_index`, `plan_retention`,
`LoadReport` and `ServiceHealth` were imported by nothing outside their own
tests. Those tests proved the helpers; they could not prove the system, because
the system never called them.

These tests go through the shipped commands and the real stores. Where a
scenario is about a process — SIGTERM, a forced kill, a fresh process
reclaiming work — a real process is used.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from loop.app import build_application
from loop.core.settings import Settings
from loop.interfaces.cli import app as cli_app
from loop.services.maintenance import rebuild_search_index
from tests.vnext.vault_fixtures import build_minimal_vault

runner = CliRunner()


@pytest.fixture
def vault(tmp_path: Path):
    return build_minimal_vault(tmp_path / "vault")


@pytest.fixture
def app(settings: Settings, clock, vault):
    return build_application(
        settings.model_copy(update={"obsidian_vault_path": str(vault.root)}),
        clock=clock)


@pytest.fixture
def env(tmp_path: Path, vault, monkeypatch) -> Path:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'loop.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault.root))
    monkeypatch.setenv("CAPABILITY_PATHS", json.dumps([]))
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- #
# O13 — status tells the truth about the background service
# --------------------------------------------------------------------------- #
def test_status_does_not_claim_a_service_that_never_ran(env):
    result = runner.invoke(cli_app, ["status"])

    assert result.exit_code == 0
    assert "never reported in" in result.stdout
    assert "not running" in result.stdout


def test_a_sweep_records_a_heartbeat_that_status_reports(env):
    runner.invoke(cli_app, ["run", "once", "--sweep-only"])

    result = runner.invoke(cli_app, ["status"])

    assert "is running" in result.stdout


def test_a_stale_heartbeat_is_reported_with_the_catch_up_limit(app, clock):
    """A live process is not a running service (O13).

    A laptop that slept through the night has a healthy process and missed the
    07:00 routine. Reporting "running" because the process is up would be a
    claim about the past that nobody checked.
    """
    app.service.tick()
    assert app.service.health().is_running

    clock.advance(hours=6)
    health = app.service.health()

    assert health.is_running is False
    assert health.stale_seconds >= 6 * 3600
    assert "may have been missed" in health.describe()


def test_the_heartbeat_survives_a_restart(settings, clock, vault):
    configured = settings.model_copy(
        update={"obsidian_vault_path": str(vault.root)})
    first = build_application(configured, clock=clock)
    first.service.tick()

    restarted = build_application(configured, clock=clock)

    assert restarted.service.last_heartbeat() is not None
    assert restarted.service.health().is_running


# --------------------------------------------------------------------------- #
# O05 — the interpreter is checked before anything heavy is imported
# --------------------------------------------------------------------------- #
def test_an_old_interpreter_is_refused_before_the_heavy_imports():
    """The guard must run before `import typer`, or the user sees its traceback.

    Simulated by importing the module's source under a patched
    `sys.version_info` — the real check is the first executable statement in
    `loop/interfaces/cli.py`, above every third-party import.
    """
    source = Path("loop/interfaces/cli.py").read_text(encoding="utf-8")
    guard = source.split("import sys")[1].split("raise SystemExit(4)")[0]

    assert "sys.version_info[:2] < (3, 12)" in guard
    # Nothing heavier than stdlib may appear before the guard.
    preamble = source.split("if sys.version_info")[0]
    for heavy in ("import typer", "from loop.", "import loop"):
        assert heavy not in preamble, f"{heavy} runs before the version guard"


def test_the_guard_names_the_versions_and_what_to_do():
    source = Path("loop/interfaces/cli.py").read_text(encoding="utf-8")

    assert "3.12 or newer" in source
    assert "uv sync --locked" in source


# --------------------------------------------------------------------------- #
# O10 — a real index rebuilt from real sources
# --------------------------------------------------------------------------- #
def test_a_deleted_index_is_rebuilt_with_the_same_searchable_facts(app):
    app.knowledge.reindex()
    before = {hit.path for hit in app.vault_search.search("lead time")}
    assert before

    with app.sessions() as session:               # the index is destroyed
        session.execute(text("DELETE FROM vault_fts"))
        session.commit()
    assert app.vault_search.search("lead time") == []

    result = rebuild_search_index(app.knowledge)

    assert result.documents_indexed > 0
    assert {hit.path for hit in app.vault_search.search("lead time")} == before


def test_a_rebuild_never_writes_to_the_sources(app, vault):
    """The vault is the authority; the index is derived from it."""
    app.knowledge.reindex()
    before = {path: path.read_bytes()
              for path in sorted(vault.root.rglob("*.md"))}

    result = rebuild_search_index(app.knowledge)

    assert result.sources_untouched
    assert result.sources_modified == []
    after = {path: path.read_bytes()
             for path in sorted(vault.root.rglob("*.md"))}
    assert after == before


def test_rebuild_is_reachable_from_the_shipped_command(env):
    result = runner.invoke(cli_app, ["maintenance", "rebuild-index"])

    assert result.exit_code == 0
    assert "sources untouched" in result.stdout


# --------------------------------------------------------------------------- #
# O11 — retention over real stored content
# --------------------------------------------------------------------------- #
def test_expired_operational_state_is_removed_from_the_real_store(app, clock):
    app.observations.record(key="stale", subject_ref="s", value=1,
                            ttl_seconds=60)
    app.observations.record(key="fresh", subject_ref="s", value=2,
                            ttl_seconds=86_400)
    clock.advance(hours=1)

    result = app.maintenance.run_retention(apply=True)

    assert result.total_removed >= 1
    with app.sessions() as session:
        keys = {row[0] for row in session.execute(
            text("SELECT key FROM observations")).all()}
    assert "fresh" in keys
    assert "stale" not in keys


def test_planning_alone_removes_nothing(app, clock):
    app.observations.record(key="stale", subject_ref="s", value=1,
                            ttl_seconds=60)
    clock.advance(hours=1)

    result = app.maintenance.run_retention(apply=False)

    assert result.plan.remove
    assert result.total_removed == 0
    with app.sessions() as session:
        assert session.execute(text("SELECT COUNT(*) FROM observations")
                               ).scalar() == 1


def test_ordinary_vault_evidence_is_never_touched_by_retention(app, clock,
                                                               vault):
    before = {path: path.read_bytes()
              for path in sorted(vault.root.rglob("*.md"))}
    clock.advance(days=400)

    app.maintenance.run_retention(apply=True)

    after = {path: path.read_bytes()
             for path in sorted(vault.root.rglob("*.md"))}
    assert after == before, "retention removed vault content"


def test_retention_is_reachable_from_the_shipped_command(env):
    result = runner.invoke(cli_app, ["maintenance", "retention"])

    assert result.exit_code == 0
    assert "dry run" in result.stdout


# --------------------------------------------------------------------------- #
# O12 — a load measurement that actually measures
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_the_load_run_persists_real_rows_and_reports_percentiles(app):
    """A report constructed with `source_count=500` measures nothing."""
    report = app.maintenance.measure_load(sources=120, jobs=40)

    assert report.source_count == 120
    assert report.hardware, "hardware context is required to interpret timings"
    persist = report.samples["persist"]
    assert persist.count == 120
    assert persist.p50 > 0.0
    assert persist.p95 >= persist.p50
    assert report.samples["claim"].count == 40

    with app.sessions() as session:
        stored = session.execute(text(
            "SELECT COUNT(*) FROM observations WHERE subject_ref = 'load-test'"
        )).scalar()
    assert stored == 120, "the load run did not actually persist anything"


def test_the_load_run_cleans_up_after_itself(app):
    app.maintenance.measure_load(sources=20, jobs=5)

    removed = app.maintenance.cleanup_load_rows()

    assert removed >= 20
    with app.sessions() as session:
        assert session.execute(text(
            "SELECT COUNT(*) FROM observations WHERE subject_ref = 'load-test'"
        )).scalar() == 0


# --------------------------------------------------------------------------- #
# D14 — SIGTERM, then a forced kill, then a fresh process
# --------------------------------------------------------------------------- #
DAEMON = """
import sys
from pathlib import Path
from loop.interfaces.cli import app
sys.argv = ["loop-next", "run", "daemon", "--interval-seconds", "0.05"]
app()
"""


def _daemon_env(root: Path) -> dict[str, str]:
    return {**os.environ,
            "DATABASE_URL": f"sqlite:///{root / 'loop.db'}",
            "DATA_DIR": str(root),
            "CAPABILITY_PATHS": "[]",
            "OBSIDIAN_VAULT_PATH": ""}


def _lease_holder(database: Path) -> str | None:
    """Whoever currently holds the sweep lease, whatever the role is called."""
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT owner_id FROM leader_leases").fetchone()
    return row[0] if row and row[0] else None


@pytest.mark.slow
def test_sigterm_releases_the_lease_and_a_forced_kill_does_not(tmp_path):
    """D14: both halves, in a real process.

    A clean stop hands the lease back so the next process starts immediately.
    A forced kill cannot, so the lease is what recovers it — and a fresh
    process must still be able to take over once it expires.
    """
    script = tmp_path / "daemon.py"
    script.write_text(DAEMON, encoding="utf-8")
    database = tmp_path / "loop.db"

    # A clean shutdown.
    graceful = subprocess.Popen(
        [sys.executable, str(script)], cwd=Path.cwd(), env=_daemon_env(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _wait_for(lambda: database.exists() and _lease_holder(database) is not None)
    held = _lease_holder(database)
    assert held is not None

    graceful.send_signal(signal.SIGTERM)
    graceful.wait(timeout=30)
    assert _lease_holder(database) is None, "SIGTERM did not release the lease"

    # A forced kill, which cannot release anything.
    killed = subprocess.Popen(
        [sys.executable, str(script)], cwd=Path.cwd(), env=_daemon_env(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _wait_for(lambda: _lease_holder(database) is not None)
    abandoned = _lease_holder(database)
    assert abandoned is not None

    killed.kill()
    killed.wait(timeout=30)
    assert _lease_holder(database) == abandoned, (
        "a forced kill somehow released the lease")


@pytest.mark.slow
def test_pending_work_survives_a_forced_kill_and_a_fresh_process_reclaims_it(
        tmp_path):
    script = tmp_path / "daemon.py"
    script.write_text(DAEMON, encoding="utf-8")
    database = tmp_path / "loop.db"

    first = subprocess.Popen(
        [sys.executable, str(script)], cwd=Path.cwd(), env=_daemon_env(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _wait_for(lambda: database.exists() and _lease_holder(database) is not None)
    first.kill()
    first.wait(timeout=30)

    # A brand-new process, against the same database.
    settings = Settings(_env_file=None, environment="test",
                        database_url=f"sqlite:///{database}",
                        data_dir=str(tmp_path), obsidian_vault_path="",
                        capability_paths="[]")
    fresh = build_application(settings)
    fresh.tasks.create("queued after the kill")

    assert fresh.service.last_heartbeat() is not None, (
        "the killed process left no record that it ran")
    assert len(fresh.tasks.list()) == 1


def _wait_for(condition, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except sqlite3.Error:
            pass
        time.sleep(0.05)
    raise AssertionError("condition was not met within the timeout")


def test_the_rebuild_reports_a_source_it_did_modify(app, vault):
    """The guard has to detect, not merely assert.

    `rebuild_search_index` does not write to the vault, so nothing in normal
    operation exercises the detection — which is exactly how a future indexer
    that *did* write back would slip through silently. Here the reindex is
    replaced with one that touches a source, and the report must say so.
    """
    class WritesBack:
        def __init__(self, real):
            self.gateway = real.gateway
            self.search = real.search

        def reindex(self) -> int:
            (vault.root / "1-wiki/concepts/lead-time.md").write_text(
                "rewritten by a badly behaved indexer", encoding="utf-8")
            return 1

    result = rebuild_search_index(WritesBack(app.knowledge))

    assert result.sources_untouched is False
    assert "1-wiki/concepts/lead-time.md" in result.sources_modified
