"""Project discovery and matching.

Matching runs on every inbound email, task, and note, so it is deterministic
and LLM-free: cheap enough to run always, and testable without a model.
"""

from __future__ import annotations

from config.settings import Settings
from core.projects import ProjectMatcher, ProjectRegistry


def _registry(tmp_path, projects: str = "", vault: str | None = None) -> ProjectRegistry:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        obsidian_vault_path=vault if vault is not None else str(tmp_path / "missing"),
        projects=projects,
    )
    return ProjectRegistry(settings)


def test_missing_vault_directory_yields_an_empty_registry(tmp_path):
    assert _registry(tmp_path).discover() == []


def test_discovers_projects_from_the_para_folder(tmp_path, vault):
    projects_dir = vault / "Projects"
    projects_dir.mkdir(parents=True)
    (projects_dir / "Atlas Migration.md").write_text("# Atlas Migration\n")
    (projects_dir / "Q3 Roadmap").mkdir()

    names = {p.name for p in _registry(tmp_path, vault=str(vault)).discover()}

    assert names == {"Atlas Migration", "Q3 Roadmap"}


def test_ignores_dotfiles_and_non_markdown(tmp_path, vault):
    projects_dir = vault / "Projects"
    projects_dir.mkdir(parents=True)
    (projects_dir / ".DS_Store").write_text("")
    (projects_dir / "notes.txt").write_text("")
    (projects_dir / "Real.md").write_text("")

    names = {p.name for p in _registry(tmp_path, vault=str(vault)).discover()}

    assert names == {"Real"}


def test_reads_keywords_from_a_note(tmp_path, vault):
    projects_dir = vault / "Projects"
    projects_dir.mkdir(parents=True)
    (projects_dir / "Atlas.md").write_text("# Atlas\nkeywords: migration, cutover\n")

    project = _registry(tmp_path, vault=str(vault)).discover()[0]

    assert "migration" in project.keywords
    assert "cutover" in project.keywords
    assert "atlas" in project.keywords  # name is always seeded in


def test_settings_projects_are_included(tmp_path):
    projects = _registry(tmp_path, projects="Falcon:falcon|raptor").discover()

    assert projects[0].slug == "falcon"
    assert projects[0].source == "settings"
    assert "raptor" in projects[0].keywords


def test_settings_project_without_keywords_still_works(tmp_path):
    projects = _registry(tmp_path, projects="Solo Project").discover()
    assert projects[0].slug == "solo-project"


def test_vault_and_settings_projects_are_merged_without_duplicates(tmp_path, vault):
    projects_dir = vault / "Projects"
    projects_dir.mkdir(parents=True)
    (projects_dir / "Atlas.md").write_text("")

    projects = _registry(tmp_path, projects="Atlas:extra", vault=str(vault)).discover()

    assert len(projects) == 1
    assert "extra" in projects[0].keywords  # settings keywords merge into the vault entry


def test_discover_is_cached_until_refresh(tmp_path, vault):
    projects_dir = vault / "Projects"
    projects_dir.mkdir(parents=True)
    (projects_dir / "One.md").write_text("")
    registry = _registry(tmp_path, vault=str(vault))
    assert len(registry.discover()) == 1

    (projects_dir / "Two.md").write_text("")
    assert len(registry.discover()) == 1  # cached

    registry.refresh()
    assert len(registry.discover()) == 2


def test_matches_on_a_keyword(tmp_path):
    matcher = ProjectMatcher(_registry(tmp_path, projects="Atlas Migration:atlas"))
    match = matcher.match("Can you review the atlas rollout plan?")
    assert match is not None
    assert match.project.slug == "atlas-migration"


def test_matches_on_the_project_name(tmp_path):
    matcher = ProjectMatcher(_registry(tmp_path, projects="Atlas Migration:atlas"))
    assert matcher.match("notes from the Atlas Migration kickoff") is not None


def test_no_match_returns_none(tmp_path):
    matcher = ProjectMatcher(_registry(tmp_path, projects="Atlas Migration:atlas"))
    assert matcher.match("lunch tomorrow with mum") is None


def test_empty_text_returns_none(tmp_path):
    matcher = ProjectMatcher(_registry(tmp_path, projects="Atlas:atlas"))
    assert matcher.match("") is None
    assert matcher.match("   ") is None


def test_empty_registry_returns_none(tmp_path):
    matcher = ProjectMatcher(_registry(tmp_path))
    assert matcher.match("anything at all") is None


def test_keyword_matching_is_word_boundary_aware(tmp_path):
    """'api' must not match inside 'rapidly'."""
    matcher = ProjectMatcher(_registry(tmp_path, projects="API Work:api"))
    assert matcher.match("we shipped rapidly this week") is None


def test_longer_keyword_outranks_shorter(tmp_path):
    matcher = ProjectMatcher(
        _registry(tmp_path, projects="API Work:api,Atlas Migration:atlas migration")
    )
    match = matcher.match("notes on the atlas migration api")
    assert match.project.slug == "atlas-migration"


def test_hashtag_mention_matches(tmp_path):
    matcher = ProjectMatcher(_registry(tmp_path, projects="Atlas Migration:atlas"))
    match = matcher.match("progress update #atlas-migration looking good")
    assert match is not None
    assert match.project.slug == "atlas-migration"


def test_match_slug_convenience_returns_a_string_or_none(tmp_path):
    matcher = ProjectMatcher(_registry(tmp_path, projects="Atlas:atlas"))
    assert matcher.match_slug("the atlas plan") == "atlas"
    assert matcher.match_slug("unrelated text") is None
