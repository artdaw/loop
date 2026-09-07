"""The vNext CLI, invoked as an installed command would be (interfaces §3).

Each test runs the actual Typer app through `CliRunner`, against a real
temporary database — not by calling the command functions directly in Python,
which would not prove the command-line parsing, exit codes or stdout
formatting a real terminal session depends on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loop.interfaces.cli import app

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> Path:
    """Point the CLI's own `Settings()` construction at a temp database.

    The CLI calls `build_application()` with no settings, exactly like a real
    terminal invocation would — so the environment, not an injected object, is
    what has to route it to an isolated database.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'loop.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OLLAMA_DEFAULT_MODEL", "llama3.1:8b")
    monkeypatch.setenv("CAPABILITY_PATHS", json.dumps([str(tmp_path / "packs")]))
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- #
# Status works with nothing configured (I6)
# --------------------------------------------------------------------------- #
def test_status_runs_with_no_model_and_no_packs(env):
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "triggers enabled: 0" in result.stdout


def test_status_reports_a_configuration_limitation_honestly(env, monkeypatch):
    monkeypatch.delenv("OLLAMA_DEFAULT_MODEL", raising=False)
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "note:" in result.stdout


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
def test_a_task_can_be_added_and_listed(env):
    add_result = runner.invoke(app, ["task", "add", "water the fern"])
    assert add_result.exit_code == 0
    task_id = add_result.stdout.split()[0]

    list_result = runner.invoke(app, ["task", "list"])
    assert task_id in list_result.stdout
    assert "water the fern" in list_result.stdout


def test_a_task_can_be_completed_through_the_real_command(env):
    add_result = runner.invoke(app, ["task", "add", "water the fern"])
    task_id = add_result.stdout.split()[0]

    complete_result = runner.invoke(app, ["task", "complete", task_id, "1"])
    assert complete_result.exit_code == 0

    list_result = runner.invoke(app, ["task", "list", "--status", "done"])
    assert task_id in list_result.stdout


def test_completing_with_a_stale_version_is_a_real_error(env):
    add_result = runner.invoke(app, ["task", "add", "water the fern"])
    task_id = add_result.stdout.split()[0]
    runner.invoke(app, ["task", "complete", task_id, "1"])

    result = runner.invoke(app, ["task", "complete", task_id, "1"])
    assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# Capability management, against a real shipped pack directory
# --------------------------------------------------------------------------- #
def _install_a_real_pack(env: Path) -> None:
    from tests.vnext.pack_fixtures import build_plant_care_pack

    build_plant_care_pack(env / "packs")


def test_capability_list_discovers_a_real_pack_directory(env):
    _install_a_real_pack(env)
    result = runner.invoke(app, ["capability", "list"])

    assert result.exit_code == 0
    assert "plantcare" in result.stdout


def test_capability_enable_through_the_real_command(env):
    _install_a_real_pack(env)
    result = runner.invoke(app, ["capability", "enable", "plantcare", "1.0.0"])
    assert result.exit_code == 0

    listed = runner.invoke(app, ["capability", "list"])
    assert "[enabled]" in listed.stdout


def test_capability_enablement_survives_between_cli_invocations(env):
    """Each `runner.invoke` is a fresh `build_application()` call — this is
    M3's registry persistence, proven through two separate CLI processes."""
    _install_a_real_pack(env)
    runner.invoke(app, ["capability", "enable", "plantcare", "1.0.0"])

    result = runner.invoke(app, ["capability", "list"])
    assert "[enabled]" in result.stdout


def test_capability_disable_through_the_real_command(env):
    _install_a_real_pack(env)
    runner.invoke(app, ["capability", "enable", "plantcare", "1.0.0"])

    result = runner.invoke(app, ["capability", "disable", "plantcare"])
    assert "plantcare@1.0.0" in result.stdout

    listed = runner.invoke(app, ["capability", "list"])
    assert "[enabled]" not in listed.stdout


def test_enabling_an_unknown_pack_is_a_real_error(env):
    result = runner.invoke(app, ["capability", "enable", "nonexistent", "1.0.0"])
    assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# The durable sweep
# --------------------------------------------------------------------------- #
def test_run_once_reports_what_it_did(env):
    result = runner.invoke(app, ["run", "once"])
    assert result.exit_code == 0
    assert "sweep(s)" in result.stdout


# --------------------------------------------------------------------------- #
# Generic invocation (agent-stack §2: no new command per pack)
# --------------------------------------------------------------------------- #
def test_do_invokes_a_capability_by_name_with_no_dedicated_command(env,
                                                                   monkeypatch):
    """Adding a pack must not require a new CLI command (agent-stack §2)."""
    _install_a_real_pack(env)
    runner.invoke(app, ["capability", "enable", "plantcare", "1.0.0"])

    from langchain_core.messages import AIMessage

    import loop.interfaces.cli as cli_module
    from tests.vnext.test_capability_runners import ToolCallingFakeChatModel

    real_build = cli_module.build_application

    def patched():
        application = real_build()
        from loop.ai.model_gateway import ModelGateway

        application.invoker.gateway = ModelGateway(
            settings=application.settings,
            local_model=ToolCallingFakeChatModel(messages=iter(
                [AIMessage(content=json.dumps({"answer": "watered",
                                              "sources": []}))])))
        return application

    monkeypatch.setattr(cli_module, "_app", patched)

    result = runner.invoke(app, ["do", "plantcare.advise",
                                 '{"query": "fern"}'], catch_exceptions=False)
    assert result.exit_code == 0
    assert "succeeded" in result.stdout


def test_do_reports_invalid_json_arguments_clearly(env):
    """Isolated from any other failure: the operation itself is unrelated and
    would fail differently (also with exit code 2) if it were even reached,
    so the message is what actually proves this is the JSON check firing."""
    result = runner.invoke(app, ["do", "nonexistent.operation", "not json"])

    assert result.exit_code == 2
    assert "must be JSON" in result.output


# --------------------------------------------------------------------------- #
# Routines (M5) — the shipped command, not a Python call
# --------------------------------------------------------------------------- #
#: `kind: at` with no instant schedules for "now", so these tests fire on the
#: next sweep whatever the wall clock says. A `local_schedule` routine would
#: make the test's outcome depend on what time the suite happens to run.
IMMEDIATE_ROUTINE = """---
schema_version: 1
id: rain-check
title: Rain check
trigger:
  kind: at
  timezone: Europe/Berlin
steps:
  - capability: weather.prepare
    arguments:
      location_ref: home
notification:
  mode: each_occurrence
  destination: "4242"
---

Fires once, immediately.
"""


def _save_routine(env: Path, body: str = IMMEDIATE_ROUTINE) -> Path:
    document = env / "rain-check.md"
    document.write_text(body, encoding="utf-8")
    return document


def test_no_routines_is_said_plainly(env):
    result = runner.invoke(app, ["routine", "list"])

    assert result.exit_code == 0
    assert "no routines saved" in result.stdout


def test_a_routine_is_saved_but_not_activated_by_saving_it(env):
    """Presence is not activation (V28/P28)."""
    document = _save_routine(env)

    result = runner.invoke(app, ["routine", "add", str(document)])

    assert result.exit_code == 0
    assert "rain-check saved (proposed)" in result.stdout
    assert "unscheduled" in runner.invoke(app, ["routine", "list"]).stdout
    assert "triggers enabled: 0" in runner.invoke(app, ["status"]).stdout


def test_activating_through_the_command_schedules_the_routine(env):
    _save_routine(env)
    runner.invoke(app, ["routine", "add", str(env / "rain-check.md")])

    result = runner.invoke(app, ["routine", "activate", "rain-check",
                                 "--event-id", "evt-1"])

    assert result.exit_code == 0
    assert "rain-check active" in result.stdout
    assert "next run" in result.stdout
    assert "triggers enabled: 1" in runner.invoke(app, ["status"]).stdout


def test_activation_requires_naming_the_authorising_event(env):
    """A routine that activates itself has no record of who asked for it."""
    _save_routine(env)
    runner.invoke(app, ["routine", "add", str(env / "rain-check.md")])

    result = runner.invoke(app, ["routine", "activate", "rain-check"])

    assert result.exit_code != 0


def test_pausing_through_the_command_unschedules_it(env):
    _save_routine(env)
    runner.invoke(app, ["routine", "add", str(env / "rain-check.md")])
    runner.invoke(app, ["routine", "activate", "rain-check", "--event-id", "e"])

    result = runner.invoke(app, ["routine", "pause", "rain-check"])

    assert result.exit_code == 0
    assert "rain-check paused" in result.stdout
    assert "triggers enabled: 0" in runner.invoke(app, ["status"]).stdout


def test_sweep_only_queues_the_work_without_running_it(env):
    _save_routine(env)
    runner.invoke(app, ["routine", "add", str(env / "rain-check.md")])
    runner.invoke(app, ["routine", "activate", "rain-check", "--event-id", "e"])

    result = runner.invoke(app, ["run", "once", "--sweep-only"])

    assert result.exit_code == 0
    assert "1 trigger(s) fired" in result.stdout
    assert "0 routine(s) run" in result.stdout
    assert "jobs queued/retrying: 1" in runner.invoke(app, ["status"]).stdout


def test_run_once_runs_the_queued_routine_and_reports_what_it_could_not_do(env):
    """No vault is configured here, so "home" resolves to nothing.

    The honest outcome is the question, not a forecast for a guessed city —
    and no provider is contacted at all, because the request never resolves
    far enough to have coordinates to send.
    """
    _save_routine(env)
    runner.invoke(app, ["routine", "add", str(env / "rain-check.md")])
    runner.invoke(app, ["routine", "activate", "rain-check", "--event-id", "e"])

    result = runner.invoke(app, ["run", "once"])

    assert result.exit_code == 0
    assert "1 routine(s) run" in result.stdout
    assert "rain-check" in result.stdout
    assert "location" in result.stdout.lower()
    assert "0 notification(s) sent" in result.stdout


# --------------------------------------------------------------------------- #
# The vault (M5) — through the shipped command
# --------------------------------------------------------------------------- #
@pytest.fixture
def vault_env(env: Path, monkeypatch) -> Path:
    from tests.vnext.vault_fixtures import build_minimal_vault

    vault = build_minimal_vault(env / "vault")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault.root))
    return vault.root


def test_vault_commands_say_plainly_when_no_vault_is_configured(env):
    result = runner.invoke(app, ["vault", "ask", "anything"])

    assert result.exit_code == 4
    assert "no vault is configured" in result.stderr


def test_a_capture_through_the_command_reports_what_actually_happened(vault_env):
    result = runner.invoke(app, ["vault", "capture",
                                 "Anodised brackets arrive Thursday."])

    assert result.exit_code == 0
    assert "Saved to your vault: 0-raw/inbox/" in result.stdout
    path = result.stdout.split("Saved to your vault: ")[1].strip()
    assert (vault_env / path).read_text(encoding="utf-8").endswith(
        "Anodised brackets arrive Thursday.")


def test_asking_finds_the_capture_and_labels_it_uncompiled(vault_env):
    runner.invoke(app, ["vault", "capture", "Anodised brackets arrive Thursday."])

    result = runner.invoke(app, ["vault", "ask", "anodised brackets"])

    assert result.exit_code == 0
    assert "uncompiled" in result.stdout.lower()
    assert "0-raw/inbox/" in result.stdout


def test_asking_about_nothing_reports_a_knowledge_gap(vault_env):
    result = runner.invoke(app, ["vault", "ask", "quarterly revenue"])

    assert result.exit_code == 0
    assert "nothing in the vault" in result.stdout.lower()


def test_reindexing_through_the_command_picks_up_the_existing_vault(vault_env):
    result = runner.invoke(app, ["vault", "reindex"])

    assert result.exit_code == 0
    assert "document(s) indexed" in result.stdout
    assert int(result.stdout.split()[0]) > 0

    answer = runner.invoke(app, ["vault", "ask", "time between order delivery"])
    assert "1-wiki/concepts/lead-time.md" in answer.stdout


def test_compiling_with_no_model_configured_says_so_and_keeps_the_capture(
        vault_env, monkeypatch):
    """No model is a stated gap, not an empty page written to look busy."""
    monkeypatch.delenv("OLLAMA_DEFAULT_MODEL", raising=False)
    capture = runner.invoke(app, ["vault", "capture", "Six week lead time."])
    path = capture.stdout.split("Saved to your vault: ")[1].strip()

    result = runner.invoke(app, ["vault", "compile", path])

    assert result.exit_code == 0
    assert "deferred" in result.stdout
    assert "No model is configured" in result.stdout
    assert (vault_env / path).exists()


# --------------------------------------------------------------------------- #
# Learning (M6) — through the shipped command
# --------------------------------------------------------------------------- #
def test_learning_review_with_nothing_recorded_says_so(env):
    result = runner.invoke(app, ["learning", "review"])

    assert result.exit_code == 0
    assert "no feedback recorded yet" in result.stdout


def test_a_snooze_is_recorded_once_per_event(env):
    first = runner.invoke(app, ["learning", "snooze", "routine:x", "30",
                                "--event-id", "e1"])
    second = runner.invoke(app, ["learning", "snooze", "routine:x", "30",
                                 "--event-id", "e1"])

    assert first.stdout.strip() == "recorded"
    assert second.stdout.strip() == "already recorded"


def test_too_little_evidence_is_reported_as_no_proposal(env):
    for index in range(2):
        runner.invoke(app, ["learning", "snooze", "routine:x", "30",
                            "--event-id", f"e{index}"])

    result = runner.invoke(app, ["learning", "review"])

    assert result.exit_code == 0
    assert "no proposal" in result.stdout
    assert "thresholds" in result.stdout


def test_a_snooze_must_move_a_notification_later(env):
    result = runner.invoke(app, ["learning", "snooze", "routine:x", "0",
                                 "--event-id", "e1"])

    assert result.exit_code != 0
    assert "later" in result.stderr


def test_confirming_a_proposal_that_does_not_exist_fails_cleanly(env):
    result = runner.invoke(app, ["learning", "confirm", "no-such-id"])

    assert result.exit_code != 0
    assert "No proposal" in result.stderr
