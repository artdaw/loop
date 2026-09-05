"""Project context wired into tasks, email triage, and storage."""

from __future__ import annotations

from config.settings import Settings
from core.projects import ProjectMatcher, ProjectRegistry
from specialists.email import EmailSpecialist


def _matcher(tmp_path, projects: str) -> ProjectMatcher:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        obsidian_vault_path=str(tmp_path / "missing"),
        projects=projects,
    )
    return ProjectMatcher(ProjectRegistry(settings))


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def test_add_task_stores_a_project(memory_store):
    task = memory_store.add_task("ship it", project="atlas")
    assert task.project == "atlas"


def test_project_defaults_to_none(memory_store):
    assert memory_store.add_task("no project").project is None


def test_list_open_tasks_filters_by_project(memory_store):
    memory_store.add_task("a", project="atlas")
    memory_store.add_task("b", project="falcon")
    memory_store.add_task("c")

    names = [t.description for t in memory_store.list_open_tasks(project="atlas")]

    assert names == ["a"]


def test_list_open_tasks_without_filter_returns_all(memory_store):
    memory_store.add_task("a", project="atlas")
    memory_store.add_task("b")
    assert len(memory_store.list_open_tasks()) == 2


def test_set_task_project_updates_an_existing_row(memory_store):
    task = memory_store.add_task("later tagged")
    assert memory_store.set_task_project(task.id, "atlas") is True
    assert memory_store.get_task(task.id).project == "atlas"


def test_set_task_project_on_missing_task_returns_false(memory_store):
    assert memory_store.set_task_project(9999, "atlas") is False


def test_known_projects_lists_distinct_slugs(memory_store):
    memory_store.add_task("a", project="atlas")
    memory_store.add_task("b", project="atlas")
    memory_store.add_task("c", project="falcon")
    memory_store.add_task("d")

    assert memory_store.known_task_projects() == ["atlas", "falcon"]


# --------------------------------------------------------------------------- #
# Task capture
# --------------------------------------------------------------------------- #
def test_save_task_tags_the_project(tmp_path, memory_store):
    from specialists.tasks import ParsedTask, TaskSpecialist

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'loop.db'}")
    spec = TaskSpecialist(settings, router=object(), memory=memory_store,
                          matcher=_matcher(tmp_path, "Atlas:atlas"))

    task = spec.save_task(ParsedTask(description="ship the atlas migration"))

    assert task.project == "atlas"


def test_save_task_without_a_matcher_leaves_project_unset(tmp_path, memory_store):
    """Default behaviour is unchanged when no matcher is injected."""
    from specialists.tasks import ParsedTask, TaskSpecialist

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'loop.db'}")
    spec = TaskSpecialist(settings, router=object(), memory=memory_store)

    assert spec.save_task(ParsedTask(description="ship the atlas thing")).project is None


def test_explicit_project_beats_the_matcher(tmp_path, memory_store):
    from specialists.tasks import ParsedTask, TaskSpecialist

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'loop.db'}")
    spec = TaskSpecialist(settings, router=object(), memory=memory_store,
                          matcher=_matcher(tmp_path, "Atlas:atlas"))

    task = spec.save_task(ParsedTask(description="atlas work"), project="manual")

    assert task.project == "manual"


# --------------------------------------------------------------------------- #
# Email triage
# --------------------------------------------------------------------------- #
def test_project_match_boosts_importance(tmp_path):
    settings = Settings(obsidian_vault_path=str(tmp_path), projects="Atlas:atlas")
    spec = EmailSpecialist(settings, vector_store=object(),
                           matcher=_matcher(tmp_path, "Atlas:atlas"))

    plain = spec.triage_email({"subject": "hello", "sender": "a@b.c", "body": "hi"})
    tagged = spec.triage_email({"subject": "atlas status", "sender": "a@b.c",
                                "body": "the atlas rollout"})

    assert tagged.importance == plain.importance + 1
    assert tagged.project == "atlas"


def test_no_matcher_means_no_boost_and_no_project(tmp_path):
    settings = Settings(obsidian_vault_path=str(tmp_path))
    spec = EmailSpecialist(settings, vector_store=object())

    score = spec.triage_email({"subject": "atlas status", "sender": "a@b.c", "body": ""})

    assert score.project is None


def test_importance_boost_still_clamps_at_five(tmp_path):
    settings = Settings(
        obsidian_vault_path=str(tmp_path),
        projects="Atlas:atlas",
        vip_senders="boss@example.com",
    )
    spec = EmailSpecialist(settings, vector_store=object(),
                           matcher=_matcher(tmp_path, "Atlas:atlas"))

    score = spec.triage_email({
        "subject": "atlas urgent", "sender": "boss@example.com", "body": "atlas",
        "sender_in_contacts": True, "thread_length": 5, "cc": ["a", "b", "c"],
    })

    assert score.importance == 5


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def test_tasks_page_filters_by_project(web_client, memory_store):
    memory_store.add_task("atlas task", project="atlas")
    memory_store.add_task("falcon task", project="falcon")

    body = web_client.get("/tasks?project=atlas").text

    assert "atlas task" in body
    assert "falcon task" not in body


def test_tasks_page_unfiltered_shows_everything(web_client, memory_store):
    memory_store.add_task("atlas task", project="atlas")
    memory_store.add_task("falcon task", project="falcon")

    body = web_client.get("/tasks").text

    assert "atlas task" in body and "falcon task" in body
