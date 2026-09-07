"""Ordinary sentences, truthfully acted on (T04–T07).

`interpret.py` was complete and connected to nothing, so every interface only
understood explicit commands. These tests run free text through
`build_application()` and through the shipped surfaces.

Almost every assertion here is about a claim *not* made: a reminder is not
announced when no time was given, an answer does not become a second task, and
a fact is not turned into a task because it happened to contain a verb.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loop.app import build_application
from loop.core.settings import Settings
from loop.interfaces.cli import app as cli_app
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


# --------------------------------------------------------------------------- #
# T04 — a request with no time in it
# --------------------------------------------------------------------------- #
def test_a_vague_time_saves_the_task_and_asks_once(app):
    outcome = app.messages.handle("remind me later to call the plumber")

    assert outcome.task_id, "the request was not lost"
    assert outcome.scheduled_for is None
    assert outcome.awaiting_answer
    assert len(outcome.questions) == 1, "exactly one question, not a form"
    assert "plumber" in app.tasks.list()[0].title


def test_a_vague_time_never_claims_a_reminder_is_set(app):
    """The failure this prevents is a confident reply about nothing (T04)."""
    outcome = app.messages.handle("remind me later to call the plumber")

    assert "I'll remind you" not in outcome.reply
    assert "no reminder time yet" in outcome.reply
    assert app.triggers.for_subject("task", outcome.task_id) == []


def test_a_concrete_time_does_schedule_and_says_so(app):
    outcome = app.messages.handle("remind me tomorrow at 9 to call the plumber")

    assert outcome.scheduled_for is not None
    assert "I'll remind you" in outcome.reply
    assert not outcome.awaiting_answer
    triggers = app.triggers.for_subject("task", outcome.task_id)
    assert len(triggers) == 1


def test_a_time_with_no_day_is_not_assumed_to_mean_today(app):
    """Picking a day would schedule something at a time nobody asked for."""
    outcome = app.messages.handle("remind me at 9 to call the plumber")

    assert outcome.scheduled_for is None
    assert outcome.awaiting_answer


# --------------------------------------------------------------------------- #
# T05 — answering the question
# --------------------------------------------------------------------------- #
def test_an_answer_completes_the_waiting_task_rather_than_making_another(app):
    first = app.messages.handle("remind me later to call the plumber")

    answer = app.messages.handle("tomorrow at 10")

    assert answer.task_id == first.task_id, "the answer created a second task"
    assert answer.scheduled_for is not None
    assert len(app.tasks.list()) == 1
    assert len(app.triggers.for_subject("task", first.task_id)) == 1


def test_the_pending_question_survives_a_restart(settings, clock, vault):
    """The gap between "later" and the answer outlives a deploy."""
    configured = settings.model_copy(
        update={"obsidian_vault_path": str(vault.root)})
    first = build_application(configured, clock=clock)
    created = first.messages.handle("remind me later to call the plumber")

    restarted = build_application(configured, clock=clock)
    answer = restarted.messages.handle("tomorrow at 10")

    assert answer.task_id == created.task_id
    assert len(restarted.tasks.list()) == 1


def test_an_answer_that_is_still_vague_keeps_the_question_open(app):
    first = app.messages.handle("remind me later to call the plumber")

    answer = app.messages.handle("sometime soon")

    assert answer.task_id == first.task_id
    assert answer.awaiting_answer
    assert answer.scheduled_for is None
    assert len(app.tasks.list()) == 1


def test_once_answered_the_next_message_is_a_new_request(app):
    app.messages.handle("remind me later to call the plumber")
    app.messages.handle("tomorrow at 10")

    second = app.messages.handle("remind me tomorrow at 11 to email the vet")

    assert "vet" in second.interpretation.task_title
    assert len(app.tasks.list()) == 2


# --------------------------------------------------------------------------- #
# T06 — a fact is not an errand
# --------------------------------------------------------------------------- #
def test_remember_to_is_a_task_and_remember_that_is_knowledge(app):
    errand = app.messages.handle("remember to call the plumber")
    # The errand left a question open. This fact must not be read as its
    # answer — it would lose the fact and answer the question with nonsense.
    fact = app.messages.handle("remember that production takes six weeks")

    assert errand.task_id and not errand.capture_path
    assert fact.capture_path and not fact.task_id
    stored = app.knowledge.gateway.read_text(fact.capture_path)
    assert "production takes six weeks" in stored


def test_a_captured_fact_is_preserved_not_paraphrased(app):
    sentence = "remember that the supplier said six weeks | confirmed by email"

    outcome = app.messages.handle(sentence)

    stored = app.knowledge.gateway.read_text(outcome.capture_path)
    assert "six weeks | confirmed by email" in stored


# --------------------------------------------------------------------------- #
# T07 — one sentence, two separate outcomes
# --------------------------------------------------------------------------- #
def test_a_fact_and_a_reminder_each_get_their_own_truthful_result(app):
    outcome = app.messages.handle(
        "remember that the boiler is under warranty and remind me Friday at 10 "
        "to verify it")

    assert outcome.capture_path, "the fact was not captured"
    assert outcome.task_id, "the reminder was not saved"
    assert "Saved to your vault" in outcome.reply
    assert "remind you" in outcome.reply or "reminder time yet" in outcome.reply


def test_friday_at_ten_said_on_a_saturday_means_next_friday(app, clock):
    """2026-09-05 is a Saturday, so "Friday" is the 11th, not yesterday."""
    outcome = app.messages.handle("remind me Friday at 10 to verify the boiler")

    assert outcome.scheduled_for is not None
    local = outcome.scheduled_for.astimezone(
        dt.timezone(dt.timedelta(hours=2)))     # Europe/Berlin in September
    assert local.strftime("%A") == "Friday"
    assert outcome.scheduled_for > clock.now()


# --------------------------------------------------------------------------- #
# Through the shipped surfaces
# --------------------------------------------------------------------------- #
@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> Path:
    vault = build_minimal_vault(tmp_path / "vault")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'loop.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault.root))
    monkeypatch.setenv("CAPABILITY_PATHS", json.dumps([str(tmp_path / "packs")]))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_the_cli_asks_the_same_question(env):
    result = runner.invoke(cli_app, ["say", "remind me later to call the plumber"])

    assert result.exit_code == 0
    assert "no reminder time yet" in result.stdout
    assert "task:" in result.stdout


def test_the_cli_answer_resolves_the_same_task(env):
    first = runner.invoke(cli_app, ["say", "remind me later to call the plumber"])
    # The *reply* also contains "Saved as a task: ...", so read the trailing
    # `task: <id>` line rather than the first match.
    task_id = [line for line in first.stdout.splitlines()
               if line.startswith("task: ")][0].removeprefix("task: ").strip()

    answer = runner.invoke(cli_app, ["say", "tomorrow at 10"])

    assert "remind you" in answer.stdout
    assert task_id in answer.stdout
    listed = runner.invoke(cli_app, ["task", "list"])
    assert listed.stdout.count(task_id) == 1


def test_the_capture_is_the_fact_not_the_instruction_that_carried_it(app):
    """"remember that" is how the owner addressed Loop, not part of the fact.

    Storing the whole sentence would put an instruction into the knowledge
    base, and every later answer citing that page would quote it back.
    """
    outcome = app.messages.handle("remember that production takes six weeks")

    stored = app.knowledge.gateway.read_text(outcome.capture_path)
    body = stored.split("---", 2)[2]
    assert "production takes six weeks" in body
    assert "remember that" not in body.lower()


def test_a_weekday_that_has_already_passed_today_means_next_week(app, clock):
    """2026-09-05 is a Saturday and the clock says 09:00 Berlin.

    "Saturday at 8" cannot mean an hour ago. Scheduling it in the past would
    fire the reminder immediately, which is the opposite of what was asked.
    """
    outcome = app.messages.handle("remind me Saturday at 8 to call the plumber")

    assert outcome.scheduled_for is not None
    assert outcome.scheduled_for > clock.now(), "scheduled in the past"
    assert (outcome.scheduled_for - clock.now()) > dt.timedelta(days=6)
