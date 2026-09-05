# Phase 4 — Polish & Automation: Design

**Date:** 2026-09-05
**Status:** Approved, ready for implementation planning
**Baseline:** `docs/SPECIFICATION.md` (Phase 3)

Phase 4 is the "act more autonomously" phase. It adds six subsystems:
autonomy levels, weekly review, project-context awareness, Wrike bidirectional
sync, voice-to-note, and a metrics dashboard.

---

## 1. Guiding principle: two orthogonal gates

Loop already has one gate. Phase 4 adds a second, and keeping them **separate**
is the central design decision of this phase.

| Gate | Question it answers | Failure mode | Lives in |
|---|---|---|---|
| `LLMRouter` (Phase 1–3) | *Which model may see this data?* | Fails **closed** — raises `PrivacyError` | `core/llm_router.py` |
| `AutonomyGate` (Phase 4) | *May Loop do this without asking?* | Fails to **asking** — requires approval | `core/autonomy.py` |

They are independent axes. A request can be private *and* fully autonomous (a
local-only voice note written to the private vault), or public *and* requiring
approval (an outbound email to a colleague). Conflating "private" with "unsafe
to automate" would make both gates harder to reason about and would tempt future
code into using one as a proxy for the other.

**Invariant (new):** `AutonomyGate` MUST NOT weaken any `LLMRouter` decision.
Autonomy governs *actions*; privacy governs *inference*. No code path may
consult the autonomy level to decide a backend, and none may consult privacy
metadata to decide whether to act.

---

## 2. Autonomy levels (`core/autonomy.py`)

### 2.1 Model

```python
class AutonomyLevel(enum.IntEnum):
    OBSERVE = 0   # record only; never surface proactively
    SUGGEST = 1   # surface a suggestion; prepare nothing
    APPROVE = 2   # prepare the action, wait for explicit approval (today's behaviour)
    ACT     = 3   # execute autonomously, then log it
```

`IntEnum` so levels compare with `<`/`>=` and a ceiling is expressible as
`min(level, ceiling)`.

```python
class ActionType(str, enum.Enum):
    EMAIL_SEND    = "email_send"
    TASK_CREATE   = "task_create"
    WRIKE_WRITE   = "wrike_write"
    NOTE_WRITE    = "note_write"
    CALENDAR_WRITE = "calendar_write"
    NOTIFY        = "notify"
```

```python
@dataclass(frozen=True)
class AutonomyDecision:
    action: ActionType
    level: AutonomyLevel
    allowed: bool            # may proceed at all (level >= SUGGEST)
    requires_approval: bool  # must not execute without an explicit approval
    reason: str
    capped: bool = False     # True when a ceiling lowered the configured level
```

### 2.2 Resolution order

`AutonomyGate.level_for(action)` resolves in this order, first hit wins:

1. `Preference["autonomy.<action>"]` — per-action override, set at runtime.
2. `Preference["autonomy.default"]` — global runtime default.
3. `settings.default_autonomy_level` — config default (`approve`).

The resolved level is then **capped** by any ceiling for that action
(§2.3). Unparseable stored values fall back to the next tier and log a warning
rather than raising — a corrupt preference must not brick the assistant.

### 2.3 The email ceiling

`settings.max_autonomy_email_send` defaults to `approve`. `EMAIL_SEND` is capped
at this value regardless of the preference, so reaching `ACT` for outbound email
requires editing `.env`, not clicking a slider in the dashboard.

This preserves the standing rule from `docs/SPECIFICATION.md` §8.1 ("Sending
always requires explicit approval — Loop never auto-sends") while making Phase
4's approval-gated autonomous sending reachable for a user who deliberately
opts in. When a ceiling applies, `AutonomyDecision.capped` is `True` and the
reason names the ceiling.

### 2.4 API

```python
class AutonomyGate:
    def __init__(self, settings=None, memory=None) -> None: ...
    def level_for(self, action: ActionType) -> AutonomyLevel: ...
    def set_level(self, action: ActionType | None, level: AutonomyLevel) -> None: ...
        # action=None writes autonomy.default; an ActionType writes that override
    def decide(self, action: ActionType, *, detail: str = "") -> AutonomyDecision: ...
    def guard(self, action: ActionType, *, approved: bool = False,
              detail: str = "") -> AutonomyDecision: ...
    def record(self, action: ActionType, *, level, executed: bool,
               approved: bool, detail: str = "") -> None: ...
```

`guard()` raises `ApprovalRequiredError` (new, in `core/exceptions.py`) when the
decision requires approval and `approved` is `False`. Callers that can present
an approval UI should use `decide()` and branch; callers that cannot should use
`guard()`.

### 2.5 Audit

New table `autonomy_audit` — `id`, `timestamp` (idx), `action` (idx), `level`,
`executed` (bool), `approved` (bool), `detail`. Mirrors the auditability rule
already applied to LLM calls: record that an action happened and under what
authority, never the full content.

### 2.6 Surfaces

- CLI: `loop autonomy` (show the table), `loop autonomy set <action> <level>`.
- Web: `/autonomy` page listing each action with its level, ceiling, and a POST
  form to change it; follows the existing act-then-303-redirect pattern.

---

## 3. Weekly review (`specialists/review.py`)

### 3.1 Split: stats vs narrative

```python
@dataclass
class ReviewStats:
    week_start: date
    week_end: date
    tasks_created: int
    tasks_completed: int
    tasks_overdue: int
    completion_rate: float          # completed / created, 0.0 when created == 0
    follow_ups_resolved: int
    follow_ups_ignored: int
    follow_ups_pending: int
    llm_local: int
    llm_cloud: int
    local_share: float              # local / total, 0.0 when total == 0
    autonomy_actions: int
    top_projects: list[tuple[str, int]]
```

`WeeklyReview.collect(week_start=None) -> ReviewStats` is **pure aggregation
over SQLite** — no LLM, fully unit-testable with a seeded temp database.

`WeeklyReview.compose(stats=None) -> str` renders a deterministic stats block
and appends a short narrative paragraph produced by the **local** model
(`source="work"`, non-private). If the router fails, the narrative is omitted
and the stats block is still returned — a model outage must not cost the user
their review.

### 3.2 Scheduling

`Scheduler.schedule_weekly_review(review, deliveries, *, day_of_week="sun",
hour=18, minute=0)` — a cron job following `schedule_end_of_day` exactly,
including its per-channel exception isolation.

Settings: `weekly_review_day` (`sun`), `weekly_review_time` (`18:00`).

CLI: `loop review [--weeks-ago N]`.

---

## 4. Project-context awareness (`core/projects.py`)

### 4.1 Where projects come from

The knowledge specialist already commits to **PARA**, so `Projects/` in the
Obsidian vault is the natural registry: one subdirectory or note per project.
A `projects` setting (CSV) supplements or replaces it when there is no vault.

```python
@dataclass(frozen=True)
class Project:
    slug: str            # normalised identifier, e.g. "atlas-migration"
    name: str            # display name, e.g. "Atlas Migration"
    keywords: frozenset[str]  # includes aliases; the name and slug are seeded in
    source: str          # "vault" | "settings"
```

`ProjectRegistry.discover() -> list[Project]` reads the vault's `Projects/`
directory (missing directory ⇒ empty list, never an error) plus the setting, and
caches the result. `refresh()` clears the cache.

### 4.2 Matching is deterministic

`ProjectMatcher.match(text, *, threshold=0.35) -> ProjectMatch | None` scores
each project by:

- keyword/alias hits in the text (weighted by keyword length, so "atlas" beats "api"),
- `difflib.SequenceMatcher` similarity between the project name and the text's
  strongest candidate span,
- a small bonus for an exact slug or `#tag` mention.

**No LLM.** Matching runs on every inbound email, task, and note, so it must be
cheap, deterministic, and testable. An LLM tiebreak is explicitly deferred.

### 4.3 Wiring in

- Nullable `project` column (indexed) on `Task` and `FollowUp`.
- `project` in the metadata written to the vector store for notes and emails.
- `EmailSpecialist` importance gets `+1` when the mail matches a known project,
  clamped as today.
- Dashboard: a project filter on `/tasks` and `/follow-ups`.

---

## 5. Wrike bidirectional sync

### 5.1 Client (`integrations/wrike.py`)

Replace the stub with a real async client against `https://www.wrike.com/api/v4`,
Bearer-authenticated from `settings.wrike_api_key`, built lazily like every
other SDK in the repo:

- `async list_tasks(*, updated_since=None) -> list[WrikeTask]`
- `async create_task(*, title, due=None, folder_id=None) -> WrikeTask`
- `async update_task(task_id, *, title=None, due=None) -> WrikeTask`
- `async complete_task(task_id) -> WrikeTask`

`WrikeTask` gains `updated_at` and `permalink`. A blank API key makes
`configured` `False`; every method raises `WrikeNotConfiguredError` in that
state, which the sync layer catches and converts into a report.

### 5.2 Conflict policy: Wrike wins, Loop pushes new

Decided deliberately: Wrike is a *team* tool. A colleague's edit there must not
be silently reverted by one peer's laptop.

| Situation | Resolution |
|---|---|
| Task exists in both, differs | **Remote wins** — local row is updated from Wrike |
| Task exists only in Wrike | Created locally, `source="wrike"` |
| Task exists only locally, no `wrike_id` | **Pushed** to Wrike (gated by `WRIKE_WRITE`), returned id stored |
| Completed locally, open in Wrike | Completion **pushed** — a deliberate local action is not a stale value |
| Completed in Wrike, open locally | Completed locally (remote wins) |
| Deleted in Wrike | Local row kept, marked `status="orphaned"` — never destroy user data on a remote 404 |

Schema: `Task` gains `wrike_id` (nullable, indexed), `last_synced_at`,
`remote_updated_at`.

**`Task.status` gains a third value.** It is currently `open|done`, and every
existing query filters on `status == "open"`. Adding `orphaned` therefore
removes those tasks from `list_open_tasks`, `get_due_today`, and `get_overdue`
automatically — which is the intent: a task whose Wrike parent was deleted
should stop nagging, but must not be destroyed. It stays visible on `/tasks`
under an explicit "orphaned" filter so it can be restored or deleted by hand.

### 5.3 Sync engine (`core/wrike_sync.py`)

```python
@dataclass
class SyncReport:
    configured: bool
    pulled_new: int
    pulled_updated: int
    pushed_new: int
    pushed_completed: int
    orphaned: int
    errors: list[str]
    dry_run: bool = False
```

`WrikeSync.sync(*, dry_run=False) -> SyncReport` runs `pull()` then `push()`.
An unconfigured key returns `SyncReport(configured=False)` — **never raises**,
per the graceful-degradation rule. Per-task errors are collected into `errors`
and do not abort the run.

Scheduler: `schedule_wrike_sync(sync, *, minutes=30)`. CLI: `loop sync [--dry-run]`.

---

## 6. Voice-to-note (`integrations/transcribe.py`)

### 6.1 Engine

`Transcriber` is a `typing.Protocol` with
`transcribe(audio_path: Path) -> Transcript`. The shipped implementation is
`FasterWhisperTranscriber`, importing `faster_whisper` lazily inside the method
so an unconfigured install still boots.

Optional dependency: `pyproject` extra `voice = ["faster-whisper>=1.0"]`, kept
out of the base install so the Docker image and the default `pip install -e .`
stay light. When the package is absent, `available()` returns `False` and the
CLI/bot report the feature as unavailable with the install command — never a
traceback.

Settings: `whisper_model_size` (`base`), `whisper_device` (`cpu`),
`whisper_compute_type` (`int8`).

### 6.2 The privacy invariant

**Audio is unconditionally `local_only=True`.** Voice memos are the most
personal content Loop touches, and the user cannot realistically audit each one
before it is processed.

- Transcription is local by construction (no cloud STT is implemented, and the
  design forbids adding one behind this interface).
- The note-formatting LLM call passes `{"local_only": True,
  "source": "obsidian_private"}`, so the privacy gate hard-blocks the cloud and
  fails closed if Ollama is down.
- The resulting note is written to `obsidian_private_vault_path` when
  configured, falling back to the main vault, and is indexed with
  `local_only=True` metadata.

### 6.3 Flow

`KnowledgeSpecialist.note_from_audio(path, *, source="telegram_voice") -> NoteDraft`:
transcribe → reuse the existing Zettelkasten formatting → `write_to_vault()` →
index. `NOTE_WRITE` autonomy gates the vault write.

Surfaces: `loop note-from-audio <path>`; a Telegram voice handler that downloads
the OGG to a temp file and calls the same method. The Telegram polling loop
itself remains a Phase 1 stub — the handler is written and tested against a fake
bot object.

---

## 7. Metrics dashboard (`core/metrics.py`, `/metrics`)

Aggregates data Loop already records; adds no new collection.

```python
class MetricsCollector:
    def llm_usage(self, days: int = 30) -> LLMUsageMetrics: ...
    def task_metrics(self, days: int = 30) -> TaskMetrics: ...
    def follow_up_metrics(self, days: int = 30) -> FollowUpMetrics: ...
    def autonomy_metrics(self, days: int = 30) -> AutonomyMetrics: ...
    def summary(self, days: int = 30) -> DashboardMetrics: ...
```

`LLMUsageMetrics` carries `local_count`, `cloud_count`, `local_share`,
`avg_latency_ms`, `p95_latency_ms`, `fallback_rate`, and a daily series.

The headline figure is the privacy one — **"N% of requests served locally"** —
because that is the product's central claim and the number the user most needs
to be able to check.

Rendering: Tailwind-styled bars sized by inline percentage width. **No chart
library**, honouring the existing no-JS-framework constraint. Empty data renders
as an explicit "no data yet" state, not a division-by-zero.

CLI: `loop metrics [--days N]`.

---

## 8. Schema migration

Phase 4 adds columns to **existing** tables (`tasks.project`, `tasks.wrike_id`,
`tasks.last_synced_at`, `tasks.remote_updated_at`, `follow_ups.project`).
`Base.metadata.create_all()` creates missing *tables* but never alters existing
ones, so an installed `data/loop.db` would break on upgrade.

`MemoryStore.bootstrap()` therefore runs an additive migration after
`create_all()`:

1. For each mapped table present in the database, read `PRAGMA table_info`.
2. For each mapped column absent from the table, execute
   `ALTER TABLE <t> ADD COLUMN <c> <type>` (nullable or with a default).
3. Never drop, rename, or retype a column.

Idempotent, dependency-free (no Alembic for a single-user SQLite file), and
safe to run on every boot. Additive-only is the constraint that makes it safe.

---

## 9. Testing

This repo has no test suite. Phase 4 introduces exactly the kind of logic that
needs one — decision tables, scoring, conflict resolution, aggregation — so it
ships with `tests/`, written test-first:

| Area | Cases |
|---|---|
| `autonomy` | resolution order; email ceiling caps `ACT`→`APPROVE`; `guard()` raises without approval; corrupt preference falls back; audit row written |
| `projects` | keyword and slug matching; no match under threshold; missing vault dir ⇒ empty registry |
| `wrike_sync` | each row of the §5.2 conflict table; unconfigured ⇒ `configured=False`, no raise; per-task error collected, run continues; `dry_run` writes nothing |
| `review` | stats aggregation over a seeded DB; zero-division guards; narrative failure still returns stats |
| `metrics` | local share; empty-data states; `p95` on small samples |
| `migration` | old schema gains new columns; running twice is a no-op; existing rows preserved |
| `transcribe` | missing `faster_whisper` ⇒ `available()` is `False`, no traceback; note routed with `local_only=True` |

Pure logic is tested against fakes, per the existing collaborator-injection
seams. No test requires Ollama, a network, or a Wrike key.

---

## 10. Explicitly out of scope

Named so they are not mistaken for oversights:

- **Phase 1 email connectors.** `scan_inboxes`, `draft_follow_up`, and
  `send_follow_up` stay `NotImplementedError`. `EMAIL_SEND` autonomy gates a
  transport that does not exist yet: the gate is real and tested, the send is
  not.
- **Telegram polling loop.** Still a stub; the voice handler is written and
  tested against a fake.
- **PII sanitisation before cloud calls** (`SPECIFICATION.md` §14 "Future") —
  a separate concern from autonomy, deferred.
- **Multi-step planning** — deferred.
- **LLM-assisted project matching** — deterministic matcher first; revisit only
  if accuracy proves insufficient in use.

---

## 11. Documentation to update in the same change

- `docs/SPECIFICATION.md` — new sections for each subsystem, the settings table,
  the two-gate principle, and the Phase 4 acceptance checks.
- `README.md` — roadmap Phase 4 → ✅, new CLI commands, the `[voice]` extra.
- `config/.env.example` — every new setting.
- `CLAUDE.md` — the autonomy gate, the migration rule, and the now-existing test
  suite.
