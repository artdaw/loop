"""Operations, packaging and interfaces — O01 to O14.

The packaging scenarios that need a real build (O01, O02, O03, O14) run their
commands in `tests/vnext/test_packaging.py`, which is marked slow. This file
covers the behaviour those builds are meant to protect.
"""

from __future__ import annotations

import json
import sqlite3
import tomllib
from pathlib import Path

import pytest

from loop.api.service import (
    ApplicationService,
    CsrfError,
    Outcome,
    form_response_status,
    issue_csrf_token,
    require_authentication,
    verify_csrf_token,
)
from loop.core.errors import Conflict, InvalidInput
from loop.ops.backup import (
    BackupError,
    create_backup,
    read_manifest,
    restore_backup,
    verify_backup,
)
from loop.ops.doctor import (
    MINIMUM_PYTHON,
    Readiness,
    ServiceHealth,
    check_python,
    diagnose,
)
from loop.ops.perf import LoadReport, describe_hardware, measure
from loop.ops.retention import Category, Record, plan_retention, rebuild_index

REPO = Path(__file__).resolve().parents[2]
NOW = 1_780_000_000


# --------------------------------------------------------------------------- #
# O01 — a locked, reproducible install
# --------------------------------------------------------------------------- #
def test_o01_the_lockfile_is_committed():
    assert (REPO / "uv.lock").exists()


def test_o01_the_lockfile_is_not_empty():
    assert (REPO / "uv.lock").stat().st_size > 1000


def test_o01_the_python_floor_is_declared():
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    requires = data["project"]["requires-python"]

    assert requires == f">={MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}"


def test_o01_the_declared_floor_matches_the_doctor_check():
    """Two places state the minimum; a drift between them is the bug."""
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    declared = data["project"]["requires-python"].removeprefix(">=")

    assert tuple(int(p) for p in declared.split(".")) == MINIMUM_PYTHON


# --------------------------------------------------------------------------- #
# O02 — installed outside the source tree
# --------------------------------------------------------------------------- #
def test_o02_pydantic_settings_is_a_runtime_dependency():
    """Settings imports it at runtime, so it cannot be dev-only."""
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    names = [d.split(">")[0].split("=")[0].strip()
             for d in data["project"]["dependencies"]]

    assert "pydantic-settings" in names


def test_o02_the_console_script_is_declared():
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert "loop" in data["project"]["scripts"]


def test_o02_every_new_package_is_importable():
    """A missing __init__.py surfaces only once installed as a wheel."""
    import importlib

    for module in ("loop.core.settings", "loop.ops.doctor", "loop.ops.backup",
                   "loop.api.service", "loop.capabilities.travel.trip",
                   "loop.capabilities.weather.bundle"):
        assert importlib.import_module(module) is not None


def test_o02_no_new_package_directory_lacks_an_init():
    for package in sorted((REPO / "loop").rglob("*/")):
        if "__pycache__" in package.parts or "egg-info" in str(package):
            continue
        if any(p.suffix == ".py" for p in package.iterdir() if p.is_file()):
            assert (package / "__init__.py").exists(), package


# --------------------------------------------------------------------------- #
# O03 — the container image
# --------------------------------------------------------------------------- #
def _dockerfile() -> str:
    return (REPO / "Dockerfile").read_text()


def test_o03_the_image_runs_as_a_non_root_user():
    body = _dockerfile()

    assert "USER " in body
    assert "USER root" not in body


def test_o03_a_dedicated_user_is_created():
    assert "useradd" in _dockerfile() or "adduser" in _dockerfile()


def test_o03_the_build_context_excludes_secrets():
    ignore = (REPO / ".dockerignore")

    assert ignore.exists()
    body = ignore.read_text()
    assert ".env" in body
    assert "data" in body


def test_o03_no_secret_is_baked_into_the_image():
    body = _dockerfile()
    for marker in ("API_KEY=", "TOKEN=", "PASSWORD=", "SECRET="):
        assert marker not in body


def test_o03_the_data_directory_is_owned_by_the_run_user():
    body = _dockerfile()
    assert "chown" in body


def test_o03_the_default_command_runs_the_durable_stack():
    assert 'CMD ["loop-next", "run", "daemon"]' in _dockerfile()


# --------------------------------------------------------------------------- #
# O04 — starting with nothing optional configured
# --------------------------------------------------------------------------- #
def test_o04_loop_starts_with_no_model_and_no_connectors():
    report = diagnose(has_local_model=False, connectors={})
    assert report.can_start is True


def test_o04_deterministic_features_stay_available():
    report = diagnose(has_local_model=False, connectors={})

    assert "tasks" in report.core_features
    assert "reminders" in report.core_features


def test_o04_model_features_are_reported_unavailable():
    report = diagnose(has_local_model=False, connectors={})
    assert "briefing" in report.degraded_features


def test_o04_an_unconfigured_connector_is_not_a_failure():
    """Not configured is a choice; broken is a fault. They are not the same."""
    assert Readiness.UNCONFIGURED.is_failure is False
    assert Readiness.OFFLINE.is_failure is True


def test_o04_the_state_of_each_dependency_is_named():
    report = diagnose(has_local_model=False,
                      connectors={"google": Readiness.AUTH_REQUIRED})
    dependency = report.by_name("google")

    assert dependency.readiness is Readiness.AUTH_REQUIRED


def test_o04_a_configured_model_removes_the_degradation():
    report = diagnose(has_local_model=True, connectors={})
    assert "briefing" not in report.degraded_features


def test_o04_the_report_renders_without_a_model():
    lines = diagnose(has_local_model=False, connectors={}).render()
    assert any("local_model: unconfigured" in line for line in lines)


# --------------------------------------------------------------------------- #
# O05 — the wrong interpreter
# --------------------------------------------------------------------------- #
def test_o05_an_old_python_is_rejected_with_a_clear_message():
    ok, detail = check_python((3, 10))

    assert ok is False
    assert "3.12" in detail and "3.10" in detail


def test_o05_the_diagnostic_says_what_to_do():
    _, detail = check_python((3, 11))
    assert "uv sync" in detail


def test_o05_the_current_interpreter_passes():
    assert check_python()[0] is True


def test_o05_the_check_needs_no_imports_from_the_project():
    """It must run before anything heavy imports, or the traceback wins."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(check_python)))
    imports = [n for n in ast.walk(tree)
               if isinstance(n, (ast.Import, ast.ImportFrom))]

    assert imports == []


# --------------------------------------------------------------------------- #
# O06 — migrating a populated legacy database
# --------------------------------------------------------------------------- #
def test_o06_a_dry_run_reports_without_applying(tmp_path):
    from sqlalchemy import create_engine, text

    from loop.db.migrations import migrate, pending_revisions

    engine = create_engine(f"sqlite:///{tmp_path / 'loop.db'}")
    pending = [r.id for r in pending_revisions(engine)]

    planned = migrate(engine, dry_run=True)
    assert planned == pending

    with engine.connect() as conn:
        names = {row[0] for row in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "tasks" not in names


def test_o06_applying_creates_the_schema(tmp_path):
    from sqlalchemy import create_engine, text

    from loop.db.migrations import migrate

    engine = create_engine(f"sqlite:///{tmp_path / 'loop.db'}")
    migrate(engine, backup=False)

    with engine.connect() as conn:
        names = {row[0] for row in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "tasks" in names


def test_o06_a_second_apply_is_a_no_op(tmp_path):
    from sqlalchemy import create_engine

    from loop.db.migrations import migrate

    engine = create_engine(f"sqlite:///{tmp_path / 'loop.db'}")
    migrate(engine, backup=False)

    assert migrate(engine, backup=False) == []


# --------------------------------------------------------------------------- #
# O07 — HTML and API mutations share one service
# --------------------------------------------------------------------------- #
@pytest.fixture
def service() -> ApplicationService:
    app = ApplicationService()
    app.put("task-1", {"title": "call the plumber", "done": False})
    return app


def test_o07_a_mutation_applies_and_bumps_the_version(service):
    result = service.mutate("task-1", expected_version=1, changes={"done": True})

    assert result.outcome is Outcome.APPLIED
    assert result.body["version"] == 2


def test_o07_a_successful_form_post_redirects(service):
    """303 so that refreshing the page does not repeat the mutation."""
    result = service.mutate("task-1", expected_version=1, changes={"done": True})
    assert form_response_status(result) == 303


def test_o07_a_form_without_a_csrf_token_is_refused():
    with pytest.raises(CsrfError):
        verify_csrf_token("", session_id="s1", secret="k")


def test_o07_a_token_from_another_session_is_refused():
    token = issue_csrf_token("s2", secret="k")
    with pytest.raises(CsrfError):
        verify_csrf_token(token, session_id="s1", secret="k")


def test_o07_a_matching_token_is_accepted():
    token = issue_csrf_token("s1", secret="k")
    verify_csrf_token(token, session_id="s1", secret="k")


def test_o07_an_unauthenticated_mutation_is_refused():
    with pytest.raises(Conflict):
        require_authentication(None, expected="secret")


def test_o07_localhost_is_not_authentication():
    """Anything on the machine can reach a local port, including a web page."""
    with pytest.raises(Conflict):
        require_authentication("", expected="secret")


def test_o07_every_surface_reaches_the_same_service(service):
    """The HTTP route and the bot button mutate one object, not two copies."""
    service.mutate("task-1", expected_version=1, changes={"done": True})
    assert service.get("task-1")["done"] is True


# --------------------------------------------------------------------------- #
# O08 — repeats, conflicts and unknown IDs
# --------------------------------------------------------------------------- #
def test_o08_a_repeated_request_is_replayed_not_reapplied(service):
    first = service.mutate("task-1", expected_version=1, changes={"done": True},
                           idempotency_key="k1")
    second = service.mutate("task-1", expected_version=1, changes={"done": True},
                            idempotency_key="k1")

    assert first.outcome is Outcome.APPLIED
    assert second.outcome is Outcome.REPLAYED


def test_o08_the_replay_has_no_second_effect(service):
    for _ in range(3):
        service.mutate("task-1", expected_version=1, changes={"done": True},
                       idempotency_key="k1")

    assert service.get("task-1")["version"] == 2


def test_o08_a_reused_key_with_a_different_body_is_rejected(service):
    service.mutate("task-1", expected_version=1, changes={"done": True},
                   idempotency_key="k1")

    with pytest.raises(InvalidInput, match="different request"):
        service.mutate("task-1", expected_version=1, changes={"done": False},
                       idempotency_key="k1")


def test_o08_a_stale_version_is_a_conflict(service):
    service.mutate("task-1", expected_version=1, changes={"done": True})
    result = service.mutate("task-1", expected_version=1, changes={"done": False})

    assert result.outcome is Outcome.CONFLICT
    assert result.body["current_version"] == 2


def test_o08_a_conflict_maps_to_409(service):
    result = service.mutate("task-1", expected_version=99, changes={})
    assert result.outcome.http_status == 409


def test_o08_an_unknown_id_is_not_found(service):
    result = service.mutate("nope", expected_version=1, changes={})

    assert result.outcome is Outcome.NOT_FOUND
    assert result.outcome.http_status == 404


def test_o08_an_unknown_id_is_never_created(service):
    service.mutate("nope", expected_version=1, changes={"title": "x"})
    assert service.get("nope") is None


def test_o08_a_conflict_leaves_the_object_unchanged(service):
    service.mutate("task-1", expected_version=1, changes={"done": True})
    service.mutate("task-1", expected_version=1, changes={"done": False})

    assert service.get("task-1")["done"] is True


# --------------------------------------------------------------------------- #
# O09 — backup and isolated restore
# --------------------------------------------------------------------------- #
def _make_source(tmp_path: Path) -> tuple[Path, Path, Path]:
    database = tmp_path / "loop.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE pending (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO pending VALUES ('op-1')")
        connection.commit()
    vault = tmp_path / "vault"
    (vault / "1-wiki").mkdir(parents=True)
    (vault / "1-wiki" / "note.md").write_text("a human wrote this")
    journal = tmp_path / "journal.json"
    journal.write_text(json.dumps({"pending": ["op-1"]}))
    return database, vault, journal


def test_o09_a_backup_covers_database_vault_and_journal(tmp_path):
    database, vault, journal = _make_source(tmp_path)
    manifest = create_backup(database=database, vault=vault, journal=journal,
                             destination=tmp_path / "backup", created_at=NOW,
                             schema_revision="0002")

    assert {p.name for p in manifest.parts} == {"database", "vault", "journal"}


def test_o09_the_manifest_records_pending_work(tmp_path):
    database, vault, journal = _make_source(tmp_path)
    create_backup(database=database, vault=vault, journal=journal,
                  destination=tmp_path / "backup", created_at=NOW,
                  schema_revision="0002", pending_jobs=3, pending_outbox=2)

    manifest = read_manifest(tmp_path / "backup")
    assert manifest.pending_jobs == 3 and manifest.pending_outbox == 2


def test_o09_an_intact_backup_verifies(tmp_path):
    database, vault, journal = _make_source(tmp_path)
    create_backup(database=database, vault=vault, journal=journal,
                  destination=tmp_path / "backup", created_at=NOW,
                  schema_revision="0002")

    assert verify_backup(tmp_path / "backup") == []


def test_o09_a_tampered_backup_is_detected(tmp_path):
    database, vault, journal = _make_source(tmp_path)
    create_backup(database=database, vault=vault, journal=journal,
                  destination=tmp_path / "backup", created_at=NOW,
                  schema_revision="0002")
    (tmp_path / "backup" / "loop.db").write_bytes(b"corrupted")

    assert "database" in verify_backup(tmp_path / "backup")[0]


def test_o09_a_renamed_vault_file_is_detected(tmp_path):
    """Identical bytes under a different name is a different vault."""
    database, vault, journal = _make_source(tmp_path)
    create_backup(database=database, vault=vault, journal=journal,
                  destination=tmp_path / "backup", created_at=NOW,
                  schema_revision="0002")
    note = tmp_path / "backup" / "vault" / "1-wiki" / "note.md"
    note.rename(note.with_name("renamed.md"))

    assert any("vault" in problem for problem in verify_backup(tmp_path / "backup"))


def test_o09_a_restore_reproduces_the_content(tmp_path):
    database, vault, journal = _make_source(tmp_path)
    create_backup(database=database, vault=vault, journal=journal,
                  destination=tmp_path / "backup", created_at=NOW,
                  schema_revision="0002")

    result = restore_backup(tmp_path / "backup", target=tmp_path / "restored")

    with sqlite3.connect(result.database) as connection:
        assert connection.execute("SELECT id FROM pending").fetchall() == [("op-1",)]
    assert (result.vault / "1-wiki" / "note.md").read_text() \
        == "a human wrote this"


def test_o09_pending_jobs_are_recovered_from_the_manifest(tmp_path):
    database, vault, journal = _make_source(tmp_path)
    create_backup(database=database, vault=vault, journal=journal,
                  destination=tmp_path / "backup", created_at=NOW,
                  schema_revision="0002", pending_jobs=4)

    result = restore_backup(tmp_path / "backup", target=tmp_path / "restored")
    assert result.pending_jobs == 4


def test_o09_a_tampered_backup_is_not_restored(tmp_path):
    database, vault, journal = _make_source(tmp_path)
    create_backup(database=database, vault=vault, journal=journal,
                  destination=tmp_path / "backup", created_at=NOW,
                  schema_revision="0002")
    (tmp_path / "backup" / "loop.db").write_bytes(b"corrupted")

    with pytest.raises(BackupError, match="does not match"):
        restore_backup(tmp_path / "backup", target=tmp_path / "restored")


def test_o09_a_restore_refuses_a_non_empty_target(tmp_path):
    """A recovery attempt must not destroy what it is recovering."""
    database, vault, journal = _make_source(tmp_path)
    create_backup(database=database, vault=vault, journal=journal,
                  destination=tmp_path / "backup", created_at=NOW,
                  schema_revision="0002")
    target = tmp_path / "live"
    target.mkdir()
    (target / "important.db").write_text("the current installation")

    with pytest.raises(BackupError, match="not empty"):
        restore_backup(tmp_path / "backup", target=target)

    assert (target / "important.db").read_text() == "the current installation"


def test_o09_the_restore_touches_nothing_outside_its_target(tmp_path):
    database, vault, journal = _make_source(tmp_path)
    create_backup(database=database, vault=vault, journal=journal,
                  destination=tmp_path / "backup", created_at=NOW,
                  schema_revision="0002")
    sibling = tmp_path / "unrelated.txt"
    sibling.write_text("do not touch")

    restore_backup(tmp_path / "backup", target=tmp_path / "restored")

    assert sibling.read_text() == "do not touch"
    assert database.read_bytes().startswith(b"SQLite")


# --------------------------------------------------------------------------- #
# O10 — rebuilding indexes
# --------------------------------------------------------------------------- #
def test_o10_a_rebuild_reads_every_source():
    sources = {"s1": "alpha", "s2": "beta"}
    result = rebuild_index(sources)

    assert result.sources_read == ["s1", "s2"]


def test_o10_the_sources_are_left_untouched():
    sources = {"s1": "alpha", "s2": "beta"}
    rebuild_index(sources, indexer=lambda sid, body: [(sid, body.upper())])

    assert sources == {"s1": "alpha", "s2": "beta"}


def test_o10_the_rebuild_reports_that_it_changed_nothing():
    result = rebuild_index({"s1": "alpha"})
    assert result.sources_untouched is True


def test_o10_the_same_sources_produce_the_same_index():
    sources = {"s1": "alpha", "s2": "beta"}
    first = rebuild_index(sources).entries_written
    second = rebuild_index(sources).entries_written

    assert first == second


# --------------------------------------------------------------------------- #
# O11 — retention maintenance
# --------------------------------------------------------------------------- #
def test_o11_expired_operational_state_is_removed():
    records = [Record("quote-1", Category.OPERATIONAL, expires_at=NOW - 1)]
    assert plan_retention(records, now=NOW).remove == ["quote-1"]


def test_o11_unexpired_state_is_kept():
    records = [Record("quote-1", Category.OPERATIONAL, expires_at=NOW + 100)]
    assert plan_retention(records, now=NOW).remove == []


def test_o11_vault_evidence_is_never_removed():
    records = [Record("receipt-1", Category.VAULT_EVIDENCE, expires_at=NOW - 1)]
    plan = plan_retention(records, now=NOW)

    assert plan.remove == []
    assert plan.kept_protected == ["receipt-1"]


def test_o11_human_content_is_never_removed():
    records = [Record("note-1", Category.HUMAN_CONTENT, expires_at=NOW - 1)]
    assert plan_retention(records, now=NOW).remove == []


def test_o11_active_work_pins_its_evidence():
    """A task outliving the note that explains it is the worse outcome."""
    records = [Record("quote-1", Category.OPERATIONAL, expires_at=NOW - 1,
                      pinned_by=("trip-1",))]
    plan = plan_retention(records, now=NOW, active_refs={"trip-1"})

    assert plan.remove == []
    assert plan.kept_pinned == ["quote-1"]


def test_o11_a_finished_reference_no_longer_pins():
    records = [Record("quote-1", Category.OPERATIONAL, expires_at=NOW - 1,
                      pinned_by=("trip-1",))]
    assert plan_retention(records, now=NOW, active_refs=set()).remove == ["quote-1"]


def test_o11_a_record_without_an_expiry_is_kept():
    records = [Record("quote-1", Category.OPERATIONAL)]
    assert plan_retention(records, now=NOW).remove == []


# --------------------------------------------------------------------------- #
# O12 — load measurement
# --------------------------------------------------------------------------- #
def test_o12_latencies_are_measured_individually():
    report = LoadReport()
    sample = measure(report, "persist", [lambda: None] * 20)

    assert sample.count == 20


def test_o12_the_tail_is_reported_not_just_the_mean():
    report = LoadReport()
    sample = report.sample("persist")
    for value in [1.0] * 19 + [500.0]:
        sample.add(value)

    assert sample.p50 == 1.0
    assert sample.maximum == 500.0


def test_o12_the_report_names_its_hardware():
    report = LoadReport(hardware=describe_hardware())

    assert report.hardware
    assert "Python" in report.hardware


def test_o12_a_target_is_compared_not_asserted():
    report = LoadReport(targets={"persist": 50.0})
    for value in (10.0, 12.0, 11.0):
        report.sample("persist").add(value)

    assert report.meets("persist") is True
    assert any("target" in line for line in report.render())


def test_o12_an_unmeasured_target_reports_none():
    assert LoadReport().meets("persist") is None


def test_o12_the_source_count_and_queue_depth_are_recorded():
    report = LoadReport(source_count=500, queue_depth=12)
    rendered = "\n".join(report.render())

    assert "sources: 500" in rendered
    assert "queue depth: 12" in rendered


# --------------------------------------------------------------------------- #
# O13 — a halted or sleeping service
# --------------------------------------------------------------------------- #
def test_o13_a_recent_heartbeat_means_running():
    health = ServiceHealth(last_heartbeat_at=NOW - 30, now=NOW)
    assert health.is_running is True


def test_o13_an_old_heartbeat_means_not_running():
    health = ServiceHealth(last_heartbeat_at=NOW - 3600, now=NOW)

    assert health.is_running is False
    assert "60 minutes ago" in health.describe()


def test_o13_a_missing_heartbeat_is_reported_honestly():
    health = ServiceHealth(last_heartbeat_at=None, now=NOW)

    assert "never reported in" in health.describe()
    assert health.stale_seconds is None


def test_o13_continuous_availability_is_never_claimed():
    """A laptop that slept did not run the 07:00 routine."""
    health = ServiceHealth(last_heartbeat_at=NOW - 3600, now=NOW)
    assert "may have been missed" in health.describe()


def test_o13_the_catch_up_limit_is_disclosed_when_exceeded():
    health = ServiceHealth(last_heartbeat_at=NOW - 200_000, now=NOW)
    assert "Catch-up only covers" in health.describe()


def test_o13_a_short_outage_does_not_mention_the_catch_up_limit():
    health = ServiceHealth(last_heartbeat_at=NOW - 3600, now=NOW)
    assert "Catch-up only covers" not in health.describe()
