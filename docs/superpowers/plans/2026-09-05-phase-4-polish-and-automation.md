# Phase 4 — Polish & Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Phase 4 of Loop — autonomy levels, weekly review, project-context awareness, Wrike bidirectional sync, voice-to-note, and a metrics dashboard — with the repo's first test suite.

**Architecture:** A second gate (`AutonomyGate`) governs *whether Loop may act*, orthogonal to the existing `LLMRouter` privacy gate that governs *which model may see data*. Privacy fails closed (`PrivacyError`); autonomy fails to asking (`ApprovalRequiredError`). New pure-logic modules (`core/autonomy.py`, `core/projects.py`, `core/wrike_sync.py`, `core/metrics.py`, `specialists/review.py`) sit in `core`/`specialists` and are consumed by the CLI, web dashboard, and scheduler.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (SQLite), pytest, Typer, FastAPI + Jinja2 + HTMX + Tailwind CDN, httpx (async), APScheduler, faster-whisper (optional extra).

**Spec:** `docs/superpowers/specs/2026-09-05-phase-4-polish-and-automation-design.md`

## Global Constraints

- Python `>=3.12`; every module starts `from __future__ import annotations`.
- `ruff` line-length **100**, target `py312`. `mypy` with `ignore_missing_imports = true`. Both must pass.
- **Layering rule:** `integrations`/`delivery` never import `specialists`; `specialists` depend on `core`; `core` depends on nothing above it.
- **Privacy invariant:** cloud calls originate only in `LLMRouter.ask_anthropic`. Nothing in Phase 4 adds a second cloud egress. Autonomy MUST NOT influence backend choice; privacy MUST NOT influence whether to act.
- **Graceful degradation:** a missing credential, absent optional package, or unreachable service returns a typed "not configured" result — never a traceback to the user.
- **Auditability:** log that an action happened, never its content. No prompt or message bodies in any table.
- Heavy/optional SDKs are imported **lazily inside methods**, matching `LLMRouter._get_ollama_async`.
- Store UTC timestamps in SQLite; scheduling honours `settings.timezone`.
- All new settings get a safe default, an entry in `config/.env.example`, and a row in the `SPECIFICATION.md` §6 table.
- Commit after every task. Branch: `phase-4`.

---

## File Structure

**Create:**
| File | Responsibility |
|---|---|
| `tests/conftest.py` | pytest fixtures: temp-SQLite `MemoryStore`, settings override, cache clearing |
| `core/autonomy.py` | `AutonomyLevel`, `ActionType`, `AutonomyDecision`, `AutonomyGate` |
| `core/projects.py` | `Project`, `ProjectRegistry`, `ProjectMatcher` |
| `core/wrike_sync.py` | `SyncReport`, `WrikeSync` (pull/push/conflict policy) |
| `core/metrics.py` | `MetricsCollector` + metric dataclasses |
| `specialists/review.py` | `ReviewStats`, `WeeklyReview` |
| `integrations/transcribe.py` | `Transcript`, `Transcriber` protocol, `FasterWhisperTranscriber` |
| `web/templates/autonomy.html`, `metrics.html` | new dashboard pages |
| `tests/test_*.py` | one per new module |

**Modify:**
| File | Change |
|---|---|
| `core/exceptions.py` | add `ApprovalRequiredError`, `WrikeNotConfiguredError` |
| `core/memory.py` | new columns, `AutonomyAudit` model, additive `_migrate()`, project/wrike accessors |
| `core/scheduler.py` | `schedule_weekly_review`, `schedule_wrike_sync` |
| `config/settings.py` | autonomy, review, projects, whisper settings |
| `integrations/wrike.py` | replace stubs with a real async client |
| `specialists/email.py` | project-aware importance boost |
| `specialists/knowledge.py` | `note_from_audio` |
| `specialists/tasks.py` | project tagging on capture |
| `integrations/telegram_bot.py` | voice message handler |
| `cli/main.py` | `autonomy`, `review`, `sync`, `metrics`, `note-from-audio` |
| `web/main.py` | `/autonomy`, `/metrics`, project filters |
| `pyproject.toml` | `[voice]` extra, pytest config |

---

## Task 1: Test infrastructure + additive schema migration

Foundation. Everything after this depends on being able to spin up a temp database with the Phase 4 columns.

**Files:**
- Create: `tests/conftest.py`, `tests/test_migration.py`
- Modify: `pyproject.toml`, `core/memory.py`, `core/exceptions.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `memory_store` pytest fixture → `MemoryStore` on a temp SQLite file; `MemoryStore.bootstrap()` now additive-migrates; `ApprovalRequiredError`, `WrikeNotConfiguredError` in `core.exceptions`.

- [ ] **Step 1: Add pytest config and the voice extra to `pyproject.toml`**

```toml
[project.optional-dependencies]
dev = ["pytest>=8.0", "ruff>=0.6", "mypy>=1.11"]
voice = ["faster-whisper>=1.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 2: Write `tests/conftest.py`**

```python
from __future__ import annotations

import pytest

from config.settings import Settings, get_settings
from core.memory import MemoryStore


@pytest.fixture
def settings(tmp_path) -> Settings:
    get_settings.cache_clear()
    return Settings(
        database_url=f"sqlite:///{tmp_path/'loop.db'}",
        chroma_persist_dir=str(tmp_path / "chroma"),
        obsidian_vault_path=str(tmp_path / "vault"),
    )


@pytest.fixture
def memory_store(settings) -> MemoryStore:
    store = MemoryStore(settings)
    store.bootstrap()
    return store
```

- [ ] **Step 3: Write the failing migration test**

```python
# tests/test_migration.py
from __future__ import annotations

import sqlalchemy as sa

from core.memory import MemoryStore


def _columns(store: MemoryStore, table: str) -> set[str]:
    with store.session() as session:
        rows = session.execute(sa.text(f"PRAGMA table_info({table})")).all()
    return {r[1] for r in rows}


def test_bootstrap_adds_phase4_columns_to_existing_db(settings):
    store = MemoryStore(settings)
    store.bootstrap()
    # Simulate a pre-Phase-4 database by dropping the new columns is not
    # possible in SQLite; instead assert the columns exist after bootstrap.
    cols = _columns(store, "tasks")
    assert {"project", "wrike_id", "last_synced_at", "remote_updated_at"} <= cols
    assert "project" in _columns(store, "follow_ups")


def test_bootstrap_is_idempotent(settings):
    store = MemoryStore(settings)
    store.bootstrap()
    before = _columns(store, "tasks")
    store.bootstrap()
    assert _columns(store, "tasks") == before


def test_migration_adds_column_to_legacy_table(settings):
    """A table created without the new columns gains them on bootstrap."""
    store = MemoryStore(settings)
    with store.session() as session:
        session.execute(sa.text(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY, description VARCHAR(500), "
            "due_date DATE, priority VARCHAR(16), status VARCHAR(16), "
            "source VARCHAR(16), created_at DATETIME, completed_at DATETIME)"
        ))
        session.execute(sa.text(
            "INSERT INTO tasks (id, description, status) VALUES (1, 'legacy', 'open')"
        ))
        session.commit()
    store.bootstrap()
    assert "wrike_id" in _columns(store, "tasks")
    with store.session() as session:
        row = session.execute(sa.text("SELECT description FROM tasks WHERE id=1")).one()
    assert row[0] == "legacy"
```

- [ ] **Step 4: Run to verify failure**

Run: `pytest tests/test_migration.py -v`
Expected: FAIL — `project`/`wrike_id` columns do not exist.

- [ ] **Step 5: Add the new exceptions**

```python
# core/exceptions.py — append
class ApprovalRequiredError(LoopError):
    """Raised when an action needs explicit approval that was not given."""


class WrikeNotConfiguredError(LoopError):
    """Raised when a Wrike call is attempted without an API key."""
```

(Use whatever base class the module already defines; if exceptions derive from
`Exception` directly, match that.)

- [ ] **Step 6: Add the Phase 4 columns and `AutonomyAudit` model to `core/memory.py`**

On `Task`: `project` (`String(64)`, nullable, indexed), `wrike_id`
(`String(64)`, nullable, indexed), `last_synced_at` (`DateTime`, nullable),
`remote_updated_at` (`DateTime`, nullable). On `FollowUp`: `project`
(`String(64)`, nullable, indexed). New model:

```python
class AutonomyAudit(Base):
    __tablename__ = "autonomy_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    level: Mapped[str] = mapped_column(String(16))
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text, default="")
```

- [ ] **Step 7: Implement the additive migration in `MemoryStore`**

```python
_SQLITE_TYPES = {
    "INTEGER": "INTEGER", "VARCHAR": "VARCHAR", "DATETIME": "DATETIME",
    "DATE": "DATE", "BOOLEAN": "BOOLEAN", "TEXT": "TEXT", "FLOAT": "FLOAT",
}


def bootstrap(self) -> None:
    """Create tables, then additively migrate any that predate this version."""
    Base.metadata.create_all(self._engine)
    self._migrate()


def _migrate(self) -> None:
    """Add mapped columns missing from existing tables.

    ``create_all`` creates missing *tables* but never alters existing ones, so a
    database written by an earlier Loop version lacks the Phase 4 columns. This
    is additive only: it never drops, renames, or retypes a column.
    """
    inspector = sa.inspect(self._engine)
    existing_tables = set(inspector.get_table_names())
    with self._engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = column.type.compile(dialect=self._engine.dialect)
                default = ""
                if column.default is not None and column.default.is_scalar:
                    default = f" DEFAULT {column.default.arg!r}"
                conn.execute(sa.text(
                    f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl}{default}"
                ))
                logger.info("Migrated: added %s.%s", table.name, column.name)
```

- [ ] **Step 8: Run tests**

Run: `pytest tests/test_migration.py -v`
Expected: PASS (3 tests).

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml tests core/memory.py core/exceptions.py
git commit -m "Phase 4: test suite scaffold + additive SQLite migration"
```

---

## Task 2: Autonomy core

**Files:**
- Create: `core/autonomy.py`, `tests/test_autonomy.py`
- Modify: `config/settings.py`

**Interfaces:**
- Consumes: `MemoryStore.get_preference/set_preference`, `ApprovalRequiredError`.
- Produces: `AutonomyLevel` (IntEnum `OBSERVE=0,SUGGEST=1,APPROVE=2,ACT=3`), `ActionType` (StrEnum), `AutonomyDecision(action, level, allowed, requires_approval, reason, capped)`, `AutonomyGate(settings=None, memory=None)` with `level_for`, `set_level`, `decide`, `guard`, `record`.

- [ ] **Step 1: Add settings**

```python
    # --- Autonomy (Phase 4) -------------------------------------------------
    default_autonomy_level: str = "approve"      # observe | suggest | approve | act
    max_autonomy_email_send: str = "approve"     # hard ceiling for outbound email
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_autonomy.py
from __future__ import annotations

import pytest

from core.autonomy import ActionType, AutonomyGate, AutonomyLevel
from core.exceptions import ApprovalRequiredError


def test_defaults_to_settings_level(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    assert gate.level_for(ActionType.TASK_CREATE) is AutonomyLevel.APPROVE


def test_per_action_override_beats_default(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(None, AutonomyLevel.SUGGEST)
    gate.set_level(ActionType.TASK_CREATE, AutonomyLevel.ACT)
    assert gate.level_for(ActionType.TASK_CREATE) is AutonomyLevel.ACT
    assert gate.level_for(ActionType.NOTE_WRITE) is AutonomyLevel.SUGGEST


def test_email_send_is_capped_at_approve(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(ActionType.EMAIL_SEND, AutonomyLevel.ACT)
    decision = gate.decide(ActionType.EMAIL_SEND)
    assert decision.level is AutonomyLevel.APPROVE
    assert decision.capped is True
    assert decision.requires_approval is True


def test_ceiling_can_be_raised_by_config(tmp_path, memory_store):
    from config.settings import Settings
    settings = Settings(
        database_url=f"sqlite:///{tmp_path/'x.db'}",
        max_autonomy_email_send="act",
    )
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(ActionType.EMAIL_SEND, AutonomyLevel.ACT)
    assert gate.decide(ActionType.EMAIL_SEND).level is AutonomyLevel.ACT


def test_act_does_not_require_approval(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(ActionType.NOTE_WRITE, AutonomyLevel.ACT)
    decision = gate.decide(ActionType.NOTE_WRITE)
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_observe_is_not_allowed(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.set_level(ActionType.WRIKE_WRITE, AutonomyLevel.OBSERVE)
    assert gate.decide(ActionType.WRIKE_WRITE).allowed is False


def test_guard_raises_without_approval(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    with pytest.raises(ApprovalRequiredError):
        gate.guard(ActionType.TASK_CREATE)


def test_guard_passes_with_approval(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    decision = gate.guard(ActionType.TASK_CREATE, approved=True)
    assert decision.requires_approval is True


def test_corrupt_preference_falls_back(settings, memory_store):
    memory_store.set_preference("autonomy.task_create", "banana")
    gate = AutonomyGate(settings, memory_store)
    assert gate.level_for(ActionType.TASK_CREATE) is AutonomyLevel.APPROVE


def test_record_writes_audit_row(settings, memory_store):
    gate = AutonomyGate(settings, memory_store)
    gate.record(ActionType.NOTE_WRITE, level=AutonomyLevel.ACT,
                executed=True, approved=False, detail="note.md")
    rows = gate.recent_audit(limit=10)
    assert len(rows) == 1
    assert rows[0].action == "note_write"
    assert rows[0].executed is True
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_autonomy.py -v` → FAIL, module not found.

- [ ] **Step 4: Implement `core/autonomy.py`**

Key logic: `level_for` reads `autonomy.<action>` then `autonomy.default` then
`settings.default_autonomy_level`, parsing with a tolerant `_parse_level` that
returns `None` on garbage (logging a warning). `_ceiling(action)` returns
`AutonomyLevel` from `settings.max_autonomy_email_send` for `EMAIL_SEND`, else
`ACT`. `decide` computes `capped = raw > ceiling`, `level = min(raw, ceiling)`,
`allowed = level >= SUGGEST`, `requires_approval = level <= APPROVE and allowed`.
`record` inserts an `AutonomyAudit` row; `recent_audit(limit)` returns detached
copies.

- [ ] **Step 5: Run tests** → `pytest tests/test_autonomy.py -v`, expect 10 PASS.

- [ ] **Step 6: Commit**

```bash
git add core/autonomy.py tests/test_autonomy.py config/settings.py
git commit -m "Phase 4: autonomy levels with per-action gate and email ceiling"
```

---

## Task 3: Autonomy surfaces (CLI + dashboard)

**Files:**
- Create: `web/templates/autonomy.html`
- Modify: `cli/main.py`, `web/main.py`, `web/templates/base.html`
- Test: `tests/test_web_autonomy.py`

**Interfaces:**
- Consumes: `AutonomyGate`, `ActionType`, `AutonomyLevel` from Task 2.
- Produces: `GET /autonomy`, `POST /autonomy/{action}` (form field `level`, 303 redirect); `loop autonomy`, `loop autonomy-set <action> <level>`.

- [ ] **Step 1: Write the failing web test**

```python
# tests/test_web_autonomy.py
from __future__ import annotations

from fastapi.testclient import TestClient


def test_autonomy_page_lists_actions(web_client):
    response = web_client.get("/autonomy")
    assert response.status_code == 200
    assert "email_send" in response.text


def test_post_sets_level_and_redirects(web_client):
    response = web_client.post("/autonomy/note_write", data={"level": "act"},
                               follow_redirects=False)
    assert response.status_code == 303
    assert "act" in web_client.get("/autonomy").text


def test_email_send_shows_ceiling(web_client):
    web_client.post("/autonomy/email_send", data={"level": "act"})
    body = web_client.get("/autonomy").text
    assert "capped" in body.lower() or "ceiling" in body.lower()
```

Add a `web_client` fixture to `conftest.py` that points `web.main` at the temp
store and returns `TestClient(app)`.

- [ ] **Step 2: Run to verify failure** → 404.

- [ ] **Step 3: Implement the route, template, and nav entry.** Follow the
existing act-then-`RedirectResponse(status_code=303)` pattern from
`web/main.py:118-145`. Add `("autonomy", "/autonomy", "Autonomy")` to the
`tabs` list in `base.html`.

- [ ] **Step 4: Implement the CLI commands** in `cli/main.py`, printing a table
of action → level → ceiling.

- [ ] **Step 5: Run tests** → `pytest tests/test_web_autonomy.py -v`, expect PASS.

- [ ] **Step 6: Commit**

```bash
git add cli/main.py web tests/test_web_autonomy.py
git commit -m "Phase 4: autonomy CLI commands and dashboard page"
```

---

## Task 4: Project registry and matcher

**Files:**
- Create: `core/projects.py`, `tests/test_projects.py`
- Modify: `config/settings.py`

**Interfaces:**
- Consumes: `Settings.obsidian_vault_path`, new `Settings.projects`.
- Produces: `Project(slug, name, keywords, source)`, `ProjectMatch(project, score)`, `ProjectRegistry(settings=None)` with `discover()`/`refresh()`, `ProjectMatcher(registry)` with `match(text, *, threshold=0.35) -> ProjectMatch | None`.

- [ ] **Step 1: Add the `projects` setting**

```python
    projects: str = ""   # CSV of "Name:kw1|kw2" project definitions
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_projects.py
from __future__ import annotations

from core.projects import ProjectMatcher, ProjectRegistry


def test_missing_vault_dir_yields_empty_registry(settings):
    assert ProjectRegistry(settings).discover() == []


def test_discovers_projects_from_para_folder(settings, tmp_path):
    projects_dir = tmp_path / "vault" / "Projects"
    projects_dir.mkdir(parents=True)
    (projects_dir / "Atlas Migration.md").write_text("# Atlas Migration\nkeywords: atlas, migration")
    (projects_dir / "Q3 Roadmap").mkdir()
    found = {p.name for p in ProjectRegistry(settings).discover()}
    assert found == {"Atlas Migration", "Q3 Roadmap"}


def test_settings_projects_are_included(tmp_path):
    from config.settings import Settings
    settings = Settings(obsidian_vault_path=str(tmp_path / "none"),
                        projects="Falcon:falcon|raptor")
    projects = ProjectRegistry(settings).discover()
    assert projects[0].slug == "falcon"
    assert "raptor" in projects[0].keywords


def test_matches_on_keyword(settings, tmp_path):
    from config.settings import Settings
    s = Settings(obsidian_vault_path=str(tmp_path), projects="Atlas Migration:atlas")
    matcher = ProjectMatcher(ProjectRegistry(s))
    match = matcher.match("Can you review the atlas rollout plan?")
    assert match is not None and match.project.slug == "atlas-migration"


def test_no_match_below_threshold(tmp_path):
    from config.settings import Settings
    s = Settings(obsidian_vault_path=str(tmp_path), projects="Atlas Migration:atlas")
    matcher = ProjectMatcher(ProjectRegistry(s))
    assert matcher.match("lunch tomorrow with mum") is None


def test_longer_keyword_outranks_shorter(tmp_path):
    from config.settings import Settings
    s = Settings(obsidian_vault_path=str(tmp_path),
                 projects="API Work:api,Atlas Migration:atlas migration")
    matcher = ProjectMatcher(ProjectRegistry(s))
    match = matcher.match("notes on the atlas migration api")
    assert match.project.slug == "atlas-migration"
```

- [ ] **Step 3: Run to verify failure** → module not found.

- [ ] **Step 4: Implement `core/projects.py`.** `discover()` lists
`<vault>/Projects/*` (files stripped of `.md`, plus directories), seeds keywords
from the name, slug, and any `keywords:` line in a note; merges CSV settings
projects. `match()` scores `sum(len(kw) for kw in keywords if kw in lowered)`
normalised by the longest project's keyword length, plus
`difflib.SequenceMatcher(None, name.lower(), text.lower()).ratio() * 0.3`, and
returns the best above `threshold`.

- [ ] **Step 5: Run tests** → expect 6 PASS.

- [ ] **Step 6: Commit**

```bash
git add core/projects.py tests/test_projects.py config/settings.py
git commit -m "Phase 4: deterministic project registry and matcher"
```

---

## Task 5: Project wiring

**Files:**
- Modify: `specialists/tasks.py`, `specialists/email.py`, `web/main.py`, `web/templates/tasks.html`, `web/templates/follow_ups.html`, `core/memory.py`
- Test: `tests/test_project_wiring.py`

**Interfaces:**
- Consumes: `ProjectMatcher` (Task 4), `project` columns (Task 1).
- Produces: `MemoryStore.add_task(..., project=None)`, `MemoryStore.set_task_project(task_id, project)`, `MemoryStore.list_open_tasks(*, project=None)`, `TaskSpecialist(..., matcher=None)` auto-tagging, `EmailSpecialist` project boost.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_project_wiring.py
from __future__ import annotations

from core.projects import ProjectMatcher, ProjectRegistry
from specialists.email import EmailSpecialist


def test_capture_tags_project(settings, memory_store, tmp_path):
    from config.settings import Settings
    from specialists.tasks import ParsedTask, TaskSpecialist
    s = Settings(database_url=settings.database_url,
                 obsidian_vault_path=str(tmp_path), projects="Atlas:atlas")
    spec = TaskSpecialist(s, router=None, memory=memory_store,
                          matcher=ProjectMatcher(ProjectRegistry(s)))
    task = spec.save_task(ParsedTask(description="ship the atlas migration"))
    assert task.project == "atlas"


def test_list_open_tasks_filters_by_project(memory_store):
    memory_store.add_task("a", project="atlas")
    memory_store.add_task("b", project="falcon")
    assert [t.description for t in memory_store.list_open_tasks(project="atlas")] == ["a"]


def test_project_match_boosts_importance(settings, tmp_path):
    from config.settings import Settings
    s = Settings(obsidian_vault_path=str(tmp_path), projects="Atlas:atlas")
    matcher = ProjectMatcher(ProjectRegistry(s))
    spec = EmailSpecialist(s, matcher=matcher)
    plain = spec.triage_email({"subject": "hello", "sender": "a@b.c", "body": ""})
    tagged = spec.triage_email({"subject": "atlas status", "sender": "a@b.c", "body": "atlas"})
    assert tagged.importance > plain.importance
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** Add the optional `matcher` kwarg to both
specialists (default `None` ⇒ no tagging, preserving current behaviour), the
`project` kwarg on `add_task`, and the `project` filter on the listing methods
and their dashboard routes (`?project=atlas` query param, rendered as filter
chips).

- [ ] **Step 4: Run tests** → expect PASS.

- [ ] **Step 5: Commit**

```bash
git add specialists web core/memory.py tests/test_project_wiring.py
git commit -m "Phase 4: project-context awareness across tasks, email, dashboard"
```

---

## Task 6: Real Wrike client

**Files:**
- Modify: `integrations/wrike.py`
- Test: `tests/test_wrike_client.py`

**Interfaces:**
- Consumes: `settings.wrike_api_key`, `WrikeNotConfiguredError`.
- Produces: `WrikeTask(task_id, title, due=None, status="Active", updated_at=None, permalink="")`; `WrikeClient(settings=None)` with `configured` property and async `list_tasks(*, updated_since=None)`, `create_task(*, title, due=None, folder_id=None)`, `update_task(task_id, *, title=None, due=None)`, `complete_task(task_id)`, `aclose()`.

- [ ] **Step 1: Write the failing tests** using `httpx.MockTransport` so no
network is touched:

```python
# tests/test_wrike_client.py
from __future__ import annotations

import httpx
import pytest

from config.settings import Settings
from core.exceptions import WrikeNotConfiguredError
from integrations.wrike import WrikeClient


def _client(handler, **kw) -> WrikeClient:
    settings = Settings(wrike_api_key="test-key", **kw)
    client = WrikeClient(settings)
    client._http = httpx.AsyncClient(base_url=WrikeClient.BASE_URL,
                                     transport=httpx.MockTransport(handler))
    return client


def test_unconfigured_client_reports_not_configured():
    client = WrikeClient(Settings(wrike_api_key=""))
    assert client.configured is False


@pytest.mark.asyncio
async def test_unconfigured_list_tasks_raises():
    client = WrikeClient(Settings(wrike_api_key=""))
    with pytest.raises(WrikeNotConfiguredError):
        await client.list_tasks()


@pytest.mark.asyncio
async def test_list_tasks_normalises_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"data": [
            {"id": "IEAA", "title": "Ship it", "status": "Active",
             "dates": {"due": "2026-09-10"}, "updatedDate": "2026-09-01T10:00:00Z",
             "permalink": "https://wrike.com/open.htm?id=1"},
        ]})

    tasks = await _client(handler).list_tasks()
    assert tasks[0].task_id == "IEAA"
    assert tasks[0].due.isoformat() == "2026-09-10"


@pytest.mark.asyncio
async def test_create_task_posts_and_returns_task():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, json={"data": [
            {"id": "NEW1", "title": "Created", "status": "Active"},
        ]})

    task = await _client(handler).create_task(title="Created")
    assert task.task_id == "NEW1"
```

Add `pytest-asyncio` to the dev extra and `asyncio_mode = "auto"` to the pytest
config so `async def` tests run.

- [ ] **Step 2: Run to verify failure** → `NotImplementedError`.

- [ ] **Step 3: Implement the client.** Lazy `httpx.AsyncClient` with
`Authorization: Bearer <key>`; `configured` is `bool(api_key.strip())`; every
public method calls `_require_configured()` first. Parse `dates.due` with
`date.fromisoformat`, tolerate missing keys, map `status` straight through.
Completion is `PUT /tasks/{id}` with `{"status": "Completed"}`.

- [ ] **Step 4: Run tests** → expect 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/wrike.py tests/test_wrike_client.py pyproject.toml
git commit -m "Phase 4: real async Wrike API client"
```

---

## Task 7: Wrike sync engine

**Files:**
- Create: `core/wrike_sync.py`, `tests/test_wrike_sync.py`
- Modify: `core/memory.py` (wrike accessors), `core/scheduler.py`, `cli/main.py`

**Interfaces:**
- Consumes: `WrikeClient` (Task 6), `AutonomyGate`/`ActionType.WRIKE_WRITE` (Task 2), `Task.wrike_id` (Task 1).
- Produces: `SyncReport(configured, pulled_new, pulled_updated, pushed_new, pushed_completed, orphaned, errors, dry_run)`; `WrikeSync(memory, client=None, gate=None, settings=None)` with `async pull()`, `async push()`, `async sync(*, dry_run=False)`; `Scheduler.schedule_wrike_sync(sync, *, minutes=30)`; `loop sync [--dry-run]`.

- [ ] **Step 1: Write the failing tests** — one per row of the spec's §5.2
conflict table, against a `FakeWrikeClient` holding an in-memory list:

```python
# tests/test_wrike_sync.py — abridged; write one test per conflict row
@pytest.mark.asyncio
async def test_remote_wins_for_linked_task(memory_store, fake_client):
    task = memory_store.add_task("old title")
    memory_store.link_wrike(task.id, "W1")
    fake_client.tasks = [WrikeTask(task_id="W1", title="new title")]
    report = await WrikeSync(memory_store, fake_client, gate=act_gate).sync()
    assert report.pulled_updated == 1
    assert memory_store.get_task(task.id).description == "new title"


@pytest.mark.asyncio
async def test_unconfigured_returns_report_and_does_not_raise(memory_store):
    report = await WrikeSync(memory_store, WrikeClient(Settings(wrike_api_key=""))).sync()
    assert report.configured is False
    assert report.errors == []


@pytest.mark.asyncio
async def test_local_only_task_is_pushed(memory_store, fake_client, act_gate):
    memory_store.add_task("local task")
    report = await WrikeSync(memory_store, fake_client, gate=act_gate).sync()
    assert report.pushed_new == 1
    assert fake_client.created[0]["title"] == "local task"


@pytest.mark.asyncio
async def test_push_blocked_when_autonomy_requires_approval(memory_store, fake_client, approve_gate):
    memory_store.add_task("local task")
    report = await WrikeSync(memory_store, fake_client, gate=approve_gate).sync()
    assert report.pushed_new == 0


@pytest.mark.asyncio
async def test_deleted_remote_marks_orphaned_not_deleted(memory_store, fake_client, act_gate):
    task = memory_store.add_task("gone")
    memory_store.link_wrike(task.id, "W9")
    fake_client.tasks = []
    report = await WrikeSync(memory_store, fake_client, gate=act_gate).sync()
    assert report.orphaned == 1
    assert memory_store.get_task(task.id) is not None


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(memory_store, fake_client, act_gate):
    memory_store.add_task("local task")
    report = await WrikeSync(memory_store, fake_client, gate=act_gate).sync(dry_run=True)
    assert report.dry_run is True and fake_client.created == []


@pytest.mark.asyncio
async def test_per_task_error_is_collected_and_run_continues(memory_store, failing_client, act_gate):
    memory_store.add_task("a")
    memory_store.add_task("b")
    report = await WrikeSync(memory_store, failing_client, gate=act_gate).sync()
    assert len(report.errors) == 1 and report.pushed_new == 1
```

Also cover: remote-only task creates locally with `source="wrike"`; local
completion pushes; remote completion completes locally.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement `WrikeSync`** following the §5.2 table exactly. Wrap
each per-task operation in `try/except` appending to `report.errors`. Check
`gate.decide(ActionType.WRIKE_WRITE).requires_approval` once before the push
loop and skip pushing when approval is required.

- [ ] **Step 4: Add `MemoryStore` helpers** — `get_task(id)`, `link_wrike(task_id,
wrike_id)`, `list_tasks_for_sync()`, `set_task_status(task_id, status)` — each
returning detached copies like the existing methods.

- [ ] **Step 5: Add `Scheduler.schedule_wrike_sync`** mirroring
`schedule_calendar_conflicts` (drives the coroutine with `asyncio.run`, catches
everything, logs).

- [ ] **Step 6: Add `loop sync [--dry-run]`** printing the report.

- [ ] **Step 7: Run tests** → `pytest tests/test_wrike_sync.py -v`, expect PASS.

- [ ] **Step 8: Commit**

```bash
git add core/wrike_sync.py core/memory.py core/scheduler.py cli/main.py tests/test_wrike_sync.py
git commit -m "Phase 4: Wrike bidirectional sync with remote-wins conflict policy"
```

---

## Task 8: Weekly review

**Files:**
- Create: `specialists/review.py`, `tests/test_review.py`
- Modify: `core/scheduler.py`, `cli/main.py`, `config/settings.py`

**Interfaces:**
- Consumes: `MemoryStore`, `LLMRouter`, `ProjectMatcher` (optional).
- Produces: `ReviewStats` (fields per spec §3.1), `WeeklyReview(settings=None, memory=None, router=None)` with `collect(week_start=None) -> ReviewStats` and `compose(stats=None) -> str`; `Scheduler.schedule_weekly_review(review, deliveries, *, day_of_week="sun", hour=18, minute=0)`; `loop review [--weeks-ago N]`.

- [ ] **Step 1: Add settings** — `weekly_review_day: str = "sun"`,
`weekly_review_time: str = "18:00"`.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_review.py
from __future__ import annotations

from datetime import date, timedelta

from specialists.review import WeeklyReview


def test_collect_counts_tasks(settings, memory_store):
    memory_store.add_task("a")
    t = memory_store.add_task("b")
    memory_store.complete_task(t.id)
    stats = WeeklyReview(settings, memory_store, router=None).collect()
    assert stats.tasks_created == 2
    assert stats.tasks_completed == 1
    assert stats.completion_rate == 0.5


def test_completion_rate_is_zero_not_error_when_nothing_created(settings, memory_store):
    stats = WeeklyReview(settings, memory_store, router=None).collect()
    assert stats.completion_rate == 0.0
    assert stats.local_share == 0.0


def test_local_share_from_usage_log(settings, memory_store):
    for backend in ("local", "local", "cloud"):
        memory_store.log_llm_usage(backend=backend, prompt_hash="x",
                                   latency_ms=10, local_only=False)
    stats = WeeklyReview(settings, memory_store, router=None).collect()
    assert stats.llm_local == 2 and stats.llm_cloud == 1
    assert round(stats.local_share, 2) == 0.67


def test_compose_without_router_still_returns_stats(settings, memory_store):
    text = WeeklyReview(settings, memory_store, router=None).compose()
    assert "Weekly review" in text


def test_compose_survives_router_failure(settings, memory_store):
    class Boom:
        def route(self, *a, **k):
            raise RuntimeError("ollama down")
    text = WeeklyReview(settings, memory_store, router=Boom()).compose()
    assert "Weekly review" in text


def test_weeks_ago_shifts_window(settings, memory_store):
    stats = WeeklyReview(settings, memory_store, router=None).collect(
        week_start=date.today() - timedelta(days=14))
    assert stats.week_end - stats.week_start == timedelta(days=6)
```

- [ ] **Step 3: Run to verify failure.**

- [ ] **Step 4: Implement.** `collect()` runs SQL aggregates scoped to the
Monday–Sunday window. `compose()` builds the deterministic block first, then
tries the narrative in a `try/except` that logs and continues.

- [ ] **Step 5: Add the scheduler job and CLI command.**

- [ ] **Step 6: Run tests** → expect 6 PASS.

- [ ] **Step 7: Commit**

```bash
git add specialists/review.py core/scheduler.py cli/main.py config/settings.py tests/test_review.py
git commit -m "Phase 4: weekly review with deterministic stats and local narrative"
```

---

## Task 9: Voice-to-note

**Files:**
- Create: `integrations/transcribe.py`, `tests/test_transcribe.py`
- Modify: `specialists/knowledge.py`, `integrations/telegram_bot.py`, `cli/main.py`, `config/settings.py`

**Interfaces:**
- Consumes: `LLMRouter` (local-only), `AutonomyGate`/`ActionType.NOTE_WRITE`.
- Produces: `Transcript(text, language, duration_seconds)`; `Transcriber` Protocol with `available() -> bool` and `transcribe(path) -> Transcript`; `FasterWhisperTranscriber(settings=None)`; `KnowledgeSpecialist.note_from_audio(path, *, source="telegram_voice") -> NoteDraft`; `loop note-from-audio <path>`.

- [ ] **Step 1: Add settings** — `whisper_model_size: str = "base"`,
`whisper_device: str = "cpu"`, `whisper_compute_type: str = "int8"`.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_transcribe.py
from __future__ import annotations

import pytest

from integrations.transcribe import FasterWhisperTranscriber, Transcript


def test_reports_unavailable_when_package_missing(settings, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", None)
    transcriber = FasterWhisperTranscriber(settings)
    assert transcriber.available() is False


def test_transcribe_without_package_raises_clear_error(settings, monkeypatch, tmp_path):
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", None)
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"")
    with pytest.raises(RuntimeError, match="faster-whisper"):
        FasterWhisperTranscriber(settings).transcribe(audio)


def test_note_from_audio_routes_local_only(settings, memory_store, tmp_path):
    """The privacy invariant: audio-derived text never reaches the cloud."""
    from specialists.knowledge import KnowledgeSpecialist

    seen: list[dict] = []

    class RecordingRouter:
        def route(self, prompt, metadata=None, **kw):
            seen.append(metadata or {})
            return '{"title": "Voice note", "body": "hello"}'

    class FakeTranscriber:
        def available(self): return True
        def transcribe(self, path): return Transcript(text="hello", language="en",
                                                      duration_seconds=1.0)

    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"")
    spec = KnowledgeSpecialist(settings, router=RecordingRouter(),
                               transcriber=FakeTranscriber())
    spec.note_from_audio(audio)
    assert all(m.get("local_only") is True for m in seen)
    assert seen[0].get("source") == "obsidian_private"
```

- [ ] **Step 3: Run to verify failure.**

- [ ] **Step 4: Implement `integrations/transcribe.py`.** `available()` does a
guarded `importlib.util.find_spec("faster_whisper")`. `transcribe()` lazily
constructs `WhisperModel(model_size, device=..., compute_type=...)`, joins the
segment texts, and raises `RuntimeError` naming `pip install -e ".[voice]"`
when the package is absent.

- [ ] **Step 5: Implement `KnowledgeSpecialist.note_from_audio`.** Transcribe,
then reuse the existing Zettelkasten formatting path with
`{"local_only": True, "source": "obsidian_private"}`, then write to the private
vault when configured (else the main vault), gated by `NOTE_WRITE`.

- [ ] **Step 6: Add the Telegram voice handler and the CLI command.** The
handler downloads the OGG to a `tempfile.NamedTemporaryFile` and calls
`note_from_audio`; it is a module-level function tested against a fake bot.

- [ ] **Step 7: Run tests** → expect PASS.

- [ ] **Step 8: Commit**

```bash
git add integrations/transcribe.py specialists/knowledge.py integrations/telegram_bot.py cli/main.py config/settings.py tests/test_transcribe.py
git commit -m "Phase 4: local voice-to-note via faster-whisper, local-only by construction"
```

---

## Task 10: Metrics collector and dashboard

**Files:**
- Create: `core/metrics.py`, `web/templates/metrics.html`, `tests/test_metrics.py`
- Modify: `web/main.py`, `web/templates/base.html`, `cli/main.py`

**Interfaces:**
- Consumes: `MemoryStore` (`llm_usage_log`, `tasks`, `follow_ups`, `autonomy_audit`).
- Produces: `LLMUsageMetrics`, `TaskMetrics`, `FollowUpMetrics`, `AutonomyMetrics`, `DashboardMetrics`; `MetricsCollector(memory)` with `llm_usage/task_metrics/follow_up_metrics/autonomy_metrics/summary(days=30)`; `GET /metrics`; `loop metrics [--days N]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_metrics.py
from __future__ import annotations

from core.metrics import MetricsCollector


def test_empty_database_yields_zero_not_error(memory_store):
    summary = MetricsCollector(memory_store).summary()
    assert summary.llm.local_share == 0.0
    assert summary.llm.total == 0


def test_local_share_is_the_headline(memory_store):
    for backend in ("local", "local", "local", "cloud"):
        memory_store.log_llm_usage(backend=backend, prompt_hash="h",
                                   latency_ms=100, local_only=False)
    metrics = MetricsCollector(memory_store).llm_usage()
    assert metrics.local_share == 0.75


def test_p95_on_small_sample_does_not_crash(memory_store):
    memory_store.log_llm_usage(backend="local", prompt_hash="h",
                               latency_ms=42, local_only=True)
    assert MetricsCollector(memory_store).llm_usage().p95_latency_ms == 42


def test_task_completion_rate(memory_store):
    memory_store.add_task("a")
    t = memory_store.add_task("b")
    memory_store.complete_task(t.id)
    assert MetricsCollector(memory_store).task_metrics().completion_rate == 0.5


def test_days_window_excludes_older_rows(memory_store):
    # rows older than the window must not be counted
    metrics = MetricsCollector(memory_store).llm_usage(days=1)
    assert metrics.total == 0
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement `core/metrics.py`** with guarded division everywhere
(`_share(n, d)` returning `0.0` when `d == 0`) and a `_percentile` helper that
handles samples of length 0 and 1.

- [ ] **Step 4: Implement `/metrics` and the template.** Headline card: "N% of
requests served locally". Bars are `<div style="width: {{ pct }}%">` with
Tailwind classes — no chart library. Empty state renders "No data yet".

- [ ] **Step 5: Add the nav entry and `loop metrics`.**

- [ ] **Step 6: Run tests** → expect PASS.

- [ ] **Step 7: Commit**

```bash
git add core/metrics.py web cli/main.py tests/test_metrics.py
git commit -m "Phase 4: metrics collector and dashboard"
```

---

## Task 11: Documentation and final verification

**Files:**
- Modify: `docs/SPECIFICATION.md`, `README.md`, `config/.env.example`, `CLAUDE.md`

- [ ] **Step 1: Update `config/.env.example`** with every new setting and a
comment per block.

- [ ] **Step 2: Update `docs/SPECIFICATION.md`** — the two-gate principle in §2,
new sections for autonomy/review/projects/wrike-sync/voice/metrics, the §6
settings table rows, the migration rule, and Phase 4 acceptance checks.

- [ ] **Step 3: Update `README.md`** — roadmap Phase 4 → ✅, the new CLI
commands table rows, and the `[voice]` extra in the install instructions.

- [ ] **Step 4: Update `CLAUDE.md`** — the autonomy gate alongside the privacy
gate, the additive-migration rule, and the now-existing test suite (replacing
the "no test suite yet" paragraph).

- [ ] **Step 5: Full verification**

```bash
ruff check .
mypy .
pytest -q
```

All three must pass. Record the actual test count in the commit message.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "Phase 4: documentation and settings reference"
```

---

## Self-Review Notes

**Spec coverage:** §2 autonomy → Tasks 2–3; §3 weekly review → Task 8; §4
projects → Tasks 4–5; §5 Wrike → Tasks 6–7; §6 voice → Task 9; §7 metrics →
Task 10; §8 migration → Task 1; §9 testing → every task; §11 docs → Task 11.
No spec section is unimplemented.

**Type consistency:** `AutonomyGate.decide` returns `AutonomyDecision`
everywhere; `WrikeSync.sync` returns `SyncReport` in Tasks 7 and 11;
`ProjectMatcher.match` returns `ProjectMatch | None` in Tasks 4 and 5;
`Transcriber.transcribe` returns `Transcript` in Task 9. `MemoryStore` helpers
added in Task 7 (`get_task`, `link_wrike`, `set_task_status`) are referenced
only after that task.

**Ordering constraint:** Task 1 must land first (columns + fixtures); Task 2
before 3, 5, 7, and 9 (the gate is consumed by all of them); Task 4 before 5;
Task 6 before 7.
