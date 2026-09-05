"""The health check behind `loop status`.

Everything must degrade gracefully: an unconfigured integration, an unreachable
Ollama, or a missing vault are *states to report*, never errors to raise. The
whole point of `loop status` is to tell you what is wrong.
"""

from __future__ import annotations

from datetime import date, timedelta

from config.settings import Settings
from core.health import HealthChecker, IntegrationStatus


def _checker(settings, memory_store, *, probe=None) -> HealthChecker:
    return HealthChecker(settings, memory=memory_store, probe=probe or (lambda url: False))


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def test_reports_the_database_as_reachable(settings, memory_store):
    report = _checker(settings, memory_store).check()
    assert report.database.ok is True
    assert "loop.db" in report.database.detail


def test_reports_an_unreachable_database(tmp_path, memory_store):
    broken = Settings(database_url="sqlite:////nonexistent-dir/nope.db")
    report = HealthChecker(broken, memory=None, probe=lambda url: False).check()
    assert report.database.ok is False


# --------------------------------------------------------------------------- #
# Local model
# --------------------------------------------------------------------------- #
def test_reports_ollama_reachable(settings, memory_store):
    report = _checker(settings, memory_store, probe=lambda url: True).check()
    assert report.ollama.ok is True


def test_reports_ollama_unreachable_without_raising(settings, memory_store):
    report = _checker(settings, memory_store, probe=lambda url: False).check()
    assert report.ollama.ok is False
    assert "not reachable" in report.ollama.detail.lower()


def test_a_probe_that_explodes_is_treated_as_unreachable(settings, memory_store):
    def boom(url):
        raise OSError("network gone")

    report = _checker(settings, memory_store, probe=boom).check()
    assert report.ollama.ok is False


# --------------------------------------------------------------------------- #
# Integrations
# --------------------------------------------------------------------------- #
def test_unconfigured_integrations_are_reported_not_raised(settings, memory_store):
    report = _checker(settings, memory_store).check()

    by_name = {i.name: i for i in report.integrations}
    assert by_name["telegram"].configured is False
    assert by_name["wrike"].configured is False


def test_configured_telegram_is_detected(tmp_path, memory_store):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        telegram_bot_token="123:abc",
        telegram_chat_id="456",
    )
    report = HealthChecker(settings, memory=memory_store,
                           probe=lambda url: False).check()

    by_name = {i.name: i for i in report.integrations}
    assert by_name["telegram"].configured is True


def test_telegram_token_without_chat_id_is_incomplete(tmp_path, memory_store):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        telegram_bot_token="123:abc",
    )
    report = HealthChecker(settings, memory=memory_store,
                           probe=lambda url: False).check()

    by_name = {i.name: i for i in report.integrations}
    assert by_name["telegram"].configured is False
    assert "chat id" in by_name["telegram"].detail.lower()


def test_gmail_reports_a_missing_credentials_file(tmp_path, memory_store):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        gmail_credentials_path=str(tmp_path / "absent.json"),
    )
    report = HealthChecker(settings, memory=memory_store,
                           probe=lambda url: False).check()

    by_name = {i.name: i for i in report.integrations}
    assert by_name["gmail"].configured is False
    assert "not found" in by_name["gmail"].detail.lower()


def test_gmail_is_configured_when_the_credentials_file_exists(tmp_path, memory_store):
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        gmail_credentials_path=str(creds),
    )
    report = HealthChecker(settings, memory=memory_store,
                           probe=lambda url: False).check()

    by_name = {i.name: i for i in report.integrations}
    assert by_name["gmail"].configured is True


def test_obsidian_reports_a_missing_vault(settings, memory_store, tmp_path):
    missing = Settings(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        obsidian_vault_path=str(tmp_path / "no-vault"),
    )
    report = HealthChecker(missing, memory=memory_store,
                           probe=lambda url: False).check()

    by_name = {i.name: i for i in report.integrations}
    assert by_name["obsidian"].configured is False


def test_existing_vault_is_configured(settings, memory_store, vault):
    report = _checker(settings, memory_store).check()
    by_name = {i.name: i for i in report.integrations}
    assert by_name["obsidian"].configured is True


def test_every_integration_is_reported(settings, memory_store):
    names = {i.name for i in _checker(settings, memory_store).check().integrations}
    assert {"telegram", "gmail", "outlook", "teams", "wrike", "obsidian",
            "anthropic", "voice"} <= names


# --------------------------------------------------------------------------- #
# Work queue
# --------------------------------------------------------------------------- #
def test_counts_the_work_queue(settings, memory_store):
    memory_store.add_task("due", due_date=date.today())
    memory_store.add_task("late", due_date=date.today() - timedelta(days=1))
    memory_store.add_task("someday")
    memory_store.add_follow_up(thread_id="t1", status="waiting")

    report = _checker(settings, memory_store).check()

    assert report.queue.open_tasks == 3
    assert report.queue.due_today == 1
    assert report.queue.overdue == 1
    assert report.queue.open_follow_ups == 1


def test_empty_queue_reports_zeros(settings, memory_store):
    report = _checker(settings, memory_store).check()
    assert report.queue.open_tasks == 0
    assert report.queue.open_follow_ups == 0


# --------------------------------------------------------------------------- #
# Autonomy + rendering
# --------------------------------------------------------------------------- #
def test_reports_the_autonomy_default(settings, memory_store):
    report = _checker(settings, memory_store).check()
    assert report.autonomy_default == "approve"
    assert report.email_ceiling == "approve"


def test_render_produces_readable_text(settings, memory_store):
    text = _checker(settings, memory_store).check().render()

    assert "Loop status" in text
    assert "Integrations" in text
    assert "Work queue" in text


def test_render_flags_problems(settings, memory_store):
    text = _checker(settings, memory_store).check().render()
    # Ollama is unreachable in this fixture, so the report must say so.
    assert "not reachable" in text.lower()


def test_integration_status_is_a_value_object():
    a = IntegrationStatus(name="x", configured=True, detail="d")
    assert a.name == "x" and a.configured is True
