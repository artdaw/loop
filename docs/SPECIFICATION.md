# Loop — Technical & Product Specification

**Status:** Living document · reflects the codebase through Phase 3
**Audience:** Engineers (human or LLM) rebuilding or extending Loop
**Author's stance:** Written as an internal design doc — the kind a staff
engineer would hand to a team (or an LLM) and expect them to reconstruct the
system faithfully from it, without access to the original source.

---

## 0. How to read this document

This spec is deliberately *reproducible*: every architectural decision, data
model, algorithm, configuration key, and module contract needed to rebuild Loop
from an empty repository is written down here. If you are an LLM tasked with
recreating Loop, you should be able to generate a functionally equivalent system
from Sections 4–13 alone. Sections 1–3 give you the "why" so your
implementation choices match the product intent.

Conventions:
- **MUST / SHOULD / MAY** carry their RFC-2119 meaning.
- Code identifiers are in `monospace`.
- "Local" always means *on the user's own machine*; "cloud" means a third-party
  hosted API (only Anthropic today).

---

## 1. Product overview

**Loop** is a terminal-first, **local-first** personal assistant for a busy
knowledge worker. It watches your email, calendar, notes, and chat, and proactively
keeps you on top of them: it drafts email follow-ups, warns you about
double-booked meetings, delivers a morning briefing, captures tasks from chat,
and answers free-form questions grounded in your own data.

The defining constraint is **privacy**: Loop runs a local LLM (via Ollama) and a
local vector store (ChromaDB) so that, by default, *nothing leaves the machine*.
A cloud model (Anthropic Claude) is used only as a fallback, and **never** for
data the user has marked private.

### 1.1 Elevator pitch

> A personal chief-of-staff that lives on your laptop. It reads your inboxes and
> calendars, remembers your notes, and nudges you at the right moments — all
> without shipping your private life to someone else's servers.

### 1.2 Primary user & personas

- **The operator (primary):** a technical individual contributor or manager who
  lives in the terminal, keeps notes in Obsidian, juggles Gmail + Outlook, and
  wants automation without surrendering privacy.
- **Peers (secondary):** colleagues of the operator who each run their **own
  isolated instance**. Loop is explicitly *not* multi-tenant; every peer gets a
  separate checkout, database, vector store, and containers.

### 1.3 Core value propositions

1. **Proactive, not reactive** — Loop initiates (briefings, reminders,
   follow-up drafts, conflict warnings) instead of waiting to be asked.
2. **Local-first privacy** — private data is answered only by the local model;
   the cloud is a fallback that the privacy gate can hard-block.
3. **Terminal-first, web-second** — the CLI is the power surface; the web
   dashboard is a lightweight companion.
4. **One brain over many sources** — email, calendar, notes, and tasks are
   unified behind one orchestrator and one semantic memory.

---

## 2. Design principles

1. **Privacy is a gate, not a preference.** The privacy decision happens in one
   place (`LLMRouter`) and fails *closed*: if a private request cannot be served
   locally, it errors rather than silently escalating to the cloud.
2. **Local-first, cloud-as-fallback.** Always try Ollama first. Escalate to
   Anthropic only on low-confidence local output (too short, refusal phrase,
   over latency budget) or when Ollama is unavailable — and only for non-private
   work.
3. **Everything configurable, nothing hardcoded.** All configuration flows
   through a single Pydantic `Settings` model sourced from environment/`.env`.
   No secrets in code.
4. **Async where it counts.** All LLM and I/O-bound integration calls are
   `async`. Background periodic work runs on APScheduler.
5. **Specialists over a monolith.** Each domain (email, calendar, tasks,
   knowledge) is a focused sub-agent with a narrow interface; the orchestrator
   composes them.
6. **Local durable state.** Structured state is SQLite (via SQLAlchemy 2.0);
   semantic memory is ChromaDB. No remote databases.
7. **Graceful degradation.** Missing connectors/credentials must not crash the
   system; features light up as integrations are configured.
8. **Auditability.** Every LLM call is logged (backend, prompt *hash*, latency,
   privacy flag) — prompt content is never persisted.

---

## 3. System context (C4 level 1)

```
                 ┌─────────────────────────────────────────────┐
                 │                   Loop (one user)            │
                 │                                              │
  Gmail  ───────▶│  integrations ─▶ specialists ─▶ orchestrator │─▶ Telegram
  Outlook ──────▶│        │                │            │       │─▶ Teams
  Google Cal ───▶│        │                │            ▼       │
  Outlook Cal ──▶│        │           core: llm_router ─┼─▶ Ollama (local)
  Obsidian ─────▶│        │                │            └─▶ Anthropic (cloud)
  Wrike ────────▶│        ▼                ▼                    │
                 │   vector_store        memory (SQLite)        │
                 │   (ChromaDB)                                 │
                 │        ▲                ▲                    │
                 │   CLI (typer) ───┐  Web (FastAPI+HTMX) ──────│─▶ Browser
                 └──────────────────┴───────────────────────────┘
```

- **Inbound:** email/calendar/notes/task integrations feed events in.
- **Brain:** specialists + orchestrator decide what to do; the LLM router picks
  the model under the privacy gate.
- **Outbound:** delivery channels (Telegram, Teams) push messages; the CLI and
  web dashboard are interactive surfaces.

---

## 4. Repository layout

```
loop/
├── cli/                     # Typer CLI (power-user surface)
│   └── main.py
├── config/
│   ├── settings.py          # single Pydantic Settings model
│   └── .env.example         # configuration template (copy to .env)
├── core/                    # the engine
│   ├── orchestrator.py      # routes events to specialists; free-form `ask`
│   ├── llm_router.py        # local-first routing + privacy gate + fallback
│   ├── memory.py            # SQLite state (ORM models + stores)
│   ├── vector_store.py      # ChromaDB semantic memory
│   ├── scheduler.py         # APScheduler periodic jobs
│   └── exceptions.py        # PrivacyError, BackendUnavailableError, …
├── specialists/             # domain sub-agents
│   ├── email.py             # inbox monitoring + triage scoring
│   ├── calendar.py          # reminders + conflict detection
│   ├── tasks.py             # task capture + summaries
│   └── knowledge.py         # Obsidian semantic Q&A
├── integrations/            # external service clients
│   ├── gmail.py  outlook.py
│   ├── google_calendar.py  outlook_calendar.py
│   ├── obsidian.py  wrike.py
│   ├── telegram_bot.py  teams_bot.py
├── delivery/                # outbound messaging
│   ├── telegram.py  teams.py
├── web/                     # FastAPI + Jinja2 + HTMX dashboard
│   ├── main.py
│   └── templates/*.html
├── scripts/                 # packaging / provisioning
│   ├── setup.sh             # one-command per-user setup
│   └── new_user.sh          # provision an isolated peer instance
├── docs/
│   ├── SPECIFICATION.md     # this document
│   └── onboarding.md        # peer onboarding guide
├── data/                    # local state (SQLite db, chroma dir) — gitignored
├── docker-compose.yml       # ollama + chromadb + app
├── Dockerfile
└── pyproject.toml
```

**Layering rule:** `integrations` and `delivery` know nothing about
`specialists`; `specialists` depend on `core`; `core` depends on nothing above
it. The `orchestrator` is the only place that wires specialists together.

---

## 5. Technology stack

| Concern            | Choice                          | Notes                                   |
|--------------------|---------------------------------|-----------------------------------------|
| Language           | Python ≥ 3.12 (runs on 3.11)    | `from __future__ import annotations`    |
| Config             | pydantic-settings (`BaseSettings`) | single `Settings` model              |
| Local LLM          | Ollama (`llama3.1:8b`)          | via `ollama` async client               |
| Cloud LLM          | Anthropic (`claude-3-5-sonnet-20241022`) | via `anthropic` async client   |
| Embeddings         | Ollama (`nomic-embed-text`)     | local embeddings for ChromaDB           |
| Vector store       | ChromaDB (persistent, local)    | knowledge + email collections           |
| Structured state   | SQLite via SQLAlchemy 2.0 ORM   | `data/loop.db`                          |
| Scheduler          | APScheduler (`BackgroundScheduler`) | interval + cron jobs                 |
| CLI                | Typer                           | entry point `loop`                      |
| Web                | FastAPI + Jinja2 + HTMX + Tailwind (CDN) | no JS framework                |
| HTTP               | httpx                           | async                                   |
| Packaging          | Docker + Docker Compose         | per-user isolation via project name     |

---

## 6. Configuration (`config/settings.py`)

All configuration is a single `Settings(BaseSettings)` with
`SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
case_sensitive=False)`. Access it through a cached `get_settings()`
(`functools.lru_cache`). Every field has a safe default; **no secrets** live in
code. Env var names match field names case-insensitively (no prefix).

### 6.1 Full field reference

| Field | Default | Purpose |
|-------|---------|---------|
| `environment` | `development` | `development` \| `production` |
| `timezone` | `Europe/Berlin` | IANA timezone for scheduling |
| `briefing_time` | `08:00` | Local HH:MM for the morning briefing |
| `follow_up_window_hours` | `48` | Flag emails with no reply after this |
| `database_url` | `sqlite:///data/loop.db` | SQLAlchemy URL |
| `chroma_persist_dir` | `data/chroma` | ChromaDB persistence directory |
| `telegram_bot_token` | `""` | BotFather token |
| `telegram_chat_id` | `""` | Default outbound chat id |
| `gmail_credentials_path` | `credentials.json` | Google OAuth client secrets |
| `gmail_token_path` | `token.json` | Cached Google user token |
| `outlook_client_id` | `""` | Azure app client id |
| `outlook_client_secret` | `""` | Azure app secret |
| `outlook_tenant_id` | `common` | Azure tenant |
| `teams_app_id` | `""` | Bot Framework app id |
| `teams_app_password` | `""` | Bot Framework secret |
| `wrike_api_key` | `""` | Wrike permanent token |
| `ollama_base_url` | `http://localhost:11434` | Ollama endpoint |
| `ollama_default_model` | `llama3.1:8b` | Local completion model |
| `ollama_embed_model` | `nomic-embed-text` | Local embedding model |
| `anthropic_api_key` | `""` | Cloud fallback key (blank ⇒ fully local) |
| `anthropic_model` | `claude-3-5-sonnet-20241022` | Cloud model |
| `llm_fallback_latency_seconds` | `10.0` | Local latency above which to escalate |
| `llm_fallback_min_chars` | `50` | Local response shorter than this ⇒ escalate |
| `obsidian_vault_path` | `~/Obsidian/Vault` | Main knowledge vault |
| `obsidian_private_vault_path` | `""` | Local-only vault (never cloud) |
| `web_host` | `0.0.0.0` | Dashboard bind host |
| `web_port` | `8000` | Dashboard port |
| `local_min_response_chars` | `40` | Legacy low-confidence threshold |
| `local_latency_budget_seconds` | `10.0` | Legacy latency budget |
| `private_vaults` | `""` | CSV of vault names/paths that must stay local |
| `private_personal_calendar` | `true` | Treat personal cal events as local-only |
| `private_telegram_personal` | `true` | Treat 1:1 Telegram chats as local-only |
| `vip_senders` | `""` | CSV of VIP sender addresses (triage boost) |
| `session_ttl_hours` | `24` | Conversation session expiry |

Derived helper: `vip_sender_list -> list[str]` splits `vip_senders` on commas
and lowercases/strips each.

---

## 7. Core engine (`core/`)

### 7.1 Exceptions (`core/exceptions.py`)

- `PrivacyError` — raised when a `local_only` request cannot be served locally.
  The router MUST never fall back to the cloud after raising this.
- `BackendUnavailableError` — raised when neither backend can serve a
  non-private request.

### 7.2 LLM Router (`core/llm_router.py`) — the privacy gate

The single choke point for every LLM call. Its job is to enforce privacy and to
implement local-first routing with cloud fallback.

**Key symbols:**
- `Backend(str, enum.Enum)`: `LOCAL = "local"`, `CLOUD = "cloud"`.
- `PRIVATE_SOURCES = frozenset({"obsidian_private", "personal_calendar",
  "telegram_private"})`.
- `_REFUSAL_MARKERS`: phrases signalling low confidence ("i don't know",
  "i'm not sure", "cannot answer", "no information", …).
- `RoutingDecision(backend, reason, sanitised=False, local_only=False)`.
- `LLMResult(text, backend, reason, latency_seconds=0.0, fell_back=False,
  metadata={})`.
- `_hash_prompt(prompt) -> str`: SHA-256 hex digest (prompt content is never
  persisted).

**Constructor:** `LLMRouter(settings=None, memory=None)`. `memory` is an
optional store used to log usage.

**Async API (Phase 3, primary):**
- `async ask_local(prompt, system=None, *, model=None) -> str` — Ollama
  `AsyncClient` chat completion against `ollama_default_model`.
- `async ask_anthropic(prompt, system=None, *, model=None) -> str` — Anthropic
  `AsyncAnthropic` completion against `anthropic_model`.
- `async route_async(prompt, context_metadata=None, *, model=None,
  system=None) -> LLMResult` — the main entry point.

**`route_async` algorithm:**
1. Read `context_metadata`: `local_only: bool` and `source: str`.
2. `decision = decide_route(local_only=…, source=…)`. A request is local-only if
   `local_only` is true **or** `source ∈ PRIVATE_SOURCES`.
3. **Local-only path (fail closed):** call `ask_local`. On *any* exception,
   raise `PrivacyError` — do **not** touch the cloud. Log `backend=local,
   local_only=True`. Return the `LLMResult`.
4. **Non-private path (local-first):** call `ask_local`; measure latency. If the
   result is *not* low-confidence, return it (`reason="local-first
   (confident)"`). Otherwise fall back to `ask_anthropic`, set `fell_back=True`,
   and return that. If Ollama itself is unavailable, go straight to Anthropic.
   If Anthropic also fails, raise `BackendUnavailableError`.
5. Log every request via the memory store (`log_llm_usage`) with backend, prompt
   hash, latency (ms), and the `local_only` flag.

**Low-confidence test** `_is_low_confidence(text, latency)` returns true when:
- `len(text.strip()) < llm_fallback_min_chars`, OR
- any `_REFUSAL_MARKERS` phrase appears (case-insensitive), OR
- `latency > llm_fallback_latency_seconds`.

**Sync/legacy API (kept for earlier specialists):** `route`, `route_detailed`,
`decide`, `complete`, `_complete_local`, `_complete_cloud`. These mirror the
async behaviour for callers that are not yet async.

> **Invariant:** there is exactly one place cloud calls can originate
> (`ask_anthropic`), and it is unreachable on the local-only path. Preserve this
> when refactoring.

### 7.3 Memory — structured state (`core/memory.py`)

SQLite via SQLAlchemy 2.0 declarative models. `Base(DeclarativeBase)`.
`MemoryStore(settings=None)` wraps an engine + session factory; `bootstrap()`
creates all tables; `session()` yields a `Session`. Store methods return
*detached* (expunged) ORM copies so callers can read fields after the session
closes.

**ORM models & tables:**

- `FollowUp` → `follow_ups`: `id`, `thread_id` (idx), `subject`, `sender`,
  `last_sent_at`, `status` (`waiting|drafted|sent|ignored`), `draft_text`,
  `triage_score` (int, idx, default 0), `snooze_until` (nullable).
- `Reminder` → `reminders`: `id`, `kind` (`meeting|task|custom`), `fire_at`
  (idx), `payload` (JSON text), `delivered` (bool).
- `Task` → `tasks`: `id`, `description`, `due_date` (nullable, idx), `priority`
  (`low|normal|high`), `status` (`open|done`), `source` (`chat|wrike|cli|web`),
  `created_at`, `completed_at` (nullable).
- `Preference` → `preferences`: `key` (pk), `value`.
- `LLMUsageLog` → `llm_usage_log`: `id`, `timestamp` (idx), `backend`,
  `prompt_hash` (SHA-256), `latency_ms`, `local_only`.
- `ConversationTurn` → `conversation_history`: `id`, `session_id` (idx), `role`
  (`user|assistant|system`), `content`, `timestamp` (idx).
- `SessionSummary` → `session_summaries`: `session_id` (pk), `summary`,
  `updated_at`.

**`MemoryStore` methods:**
- Tasks: `add_task(description, *, due_date=None, priority="normal",
  source="chat")`, `list_open_tasks()`, `get_due_today(today=None)`,
  `get_overdue(today=None)`, `get_completed_today(today=None)`,
  `complete_task(task_id) -> bool`.
- Follow-ups: `list_open_follow_ups(*, include_snoozed=False)` (sorts by
  `triage_score` desc, hides snoozed unless asked), `add_follow_up(*, thread_id,
  subject="", sender="", status="waiting", triage_score=0)`,
  `get_follow_up(id)`, `set_follow_up_status(id, status) -> bool`,
  `snooze_follow_up(id, hours) -> bool`, `set_follow_up_triage(id, score) ->
  bool`.
- Preferences: `get_preference(key, default="")`, `set_preference(key, value)`.
- Usage: `log_llm_usage(*, backend, prompt_hash, latency_ms, local_only)`.

**`ConversationMemory(settings=None, ...)`** — async persistent conversation
context, sharing the same SQLite database:
- `async save_turn(role, content, session_id)`.
- `async get_recent(session_id, n=10) -> list[dict]` — returns `[]` if the
  session is expired; otherwise the last `n` turns as `{"role", "content"}`.
- `async summarise_session(session_id) -> str` — produces a one-paragraph
  summary using the **local-only** LLM (session content is private) and upserts
  a `SessionSummary`.
- `is_expired(session_id) -> bool` — true when the last turn is older than
  `session_ttl_hours`.
- `purge_expired() -> int` — deletes turns for expired sessions.

### 7.4 Vector store (`core/vector_store.py`) — semantic memory

Wraps a persistent ChromaDB client at `chroma_persist_dir`. Two collections:
`knowledge` (Obsidian notes) and `emails` (email summaries). Embeddings come
from Ollama's `nomic-embed-text` (local).

**`VectorStore(settings=None)` methods:**
- `embed(text) -> list[float]` — local embedding.
- `embed_and_store(text, metadata=None, *, collection=…)`.
- `index_note(text, *, path, title, date=…)` — index an Obsidian note.
- `index_email(subject, summary, sender, date) -> str` — index an email summary.
- `semantic_search(query, n_results=5, *, collections=None) ->
  list[SearchResult]` — embeds the query, queries each target collection,
  merges, sorts by ascending distance, returns top-n. `SearchResult` exposes
  `id`, `document`, `metadata`, `distance`, and a `kind` property (`note` /
  `email`).

Privacy note: notes from the private vault MUST carry metadata that marks them
local-only so downstream Q&A forces local inference (see §9.4/§8.1).

### 7.5 Scheduler (`core/scheduler.py`)

APScheduler `BackgroundScheduler` bound to `settings.timezone`.

- `add_interval_job(func, *, minutes, job_id)`.
- `add_daily_job(func, *, hour, minute, job_id)`.
- `schedule_end_of_day(task_specialist, deliveries, *, hour=17, minute=30)` —
  builds and sends the end-of-day task summary; failures in one channel never
  block others.
- `schedule_calendar_conflicts(calendar_specialist, *, minutes=15,
  run_immediately=True)` — runs `check_and_warn_conflicts()` (an async
  coroutine, driven with `asyncio.run` inside the sync job) at startup and every
  15 minutes.
- `start()` / `shutdown()`.

Periodic responsibilities (wired by the orchestrator): poll calendars, poll
inboxes, fire 30-min + 5-min meeting reminders, the 08:00 morning briefing, the
17:30 end-of-day summary, and the 15-min calendar conflict sweep.

### 7.6 Orchestrator (`core/orchestrator.py`)

The brain. Dataclasses:
- `AgentEvent(source, kind, payload={}, is_private=False)`.
- `AgentResponse(text, channel="auto", actions=[])`.

`Orchestrator(settings=None, *, router=None, vectors=None, conversation=None)` —
collaborators are injectable (for tests) and otherwise created lazily via
`_get_router` / `_get_vectors` / `_get_conversation`. `register_specialist(name,
specialist)` fills a dispatch table.

**Free-form Q&A — `async ask(question, *, session_id="cli", local_only=None,
n_context=5) -> AgentResponse`** (the RAG pipeline behind `loop ask`):
1. `semantic_search(question, n_results=n_context)` over the vector store
   (best-effort; empty on failure).
2. Determine privacy: if any retrieved hit is private (`metadata.is_personal`
   truthy or `metadata.collection == "personal"`), force local-only — unless the
   caller explicitly set `local_only`.
3. Load recent conversation turns via `ConversationMemory.get_recent`.
4. Compose a grounded prompt: retrieved context block + conversation history +
   the question, with a concise system prompt that forbids inventing facts.
5. `router.route_async(prompt, {"local_only": force_local, "source":
   "personal" if force_local else "work"}, system=…)`.
6. Persist both the user and assistant turns.
7. Return `AgentResponse(text=…, actions=[{backend, local_only, sources}])`.

`handle(event)` and `run_forever()` are the event-dispatch and daemon entry
points (scaffolded; wire specialists + scheduler + inbound listeners here).

---

## 8. Specialists (`specialists/`)

Each specialist is a focused sub-agent with a narrow, testable interface.

### 8.1 Email Specialist (`specialists/email.py`)

`EmailSpecialist(settings=None, vector_store=None, memory=None)`.

**Triage scoring (Phase 3):**
- `TriageScore(urgency: int, importance: int, action: str)` with
  `score = urgency * importance` (1..25).
- `TRIAGE_ACTIONS = (reply_now, reply_later, read_only, delegate, archive)`.
- `_URGENCY_KEYWORDS`: urgent, asap, deadline, today, eod, critical, …
- `triage_email(email) -> TriageScore`: reads `subject`, `sender`, `body`/
  `snippet`, `cc`, `thread_length`, `sender_in_contacts` from an object or dict.
  - **Urgency (1..5):** base 1 + up to +3 for urgency-keyword hits + 1 if the
    sender is a VIP (`vip_sender_list`), clamped to 5.
  - **Importance (1..5):** base 1 + 2 if VIP + 1 if in contacts + 1 if
    `thread_length ≥ 3` + 1 if `cc_count ≥ 3`, clamped to 5.
  - **Action:** `_recommend_action(urgency, importance, is_vip, cc_count)` —
    high/high ⇒ `reply_now`; important-but-not-urgent ⇒ `reply_later`; trivial &
    non-VIP ⇒ `archive`; broadly-CC'd low importance ⇒ `delegate`/`read_only`;
    else by composite.
- `triage_and_persist(follow_up_id, email)` — triage then
  `memory.set_follow_up_triage(follow_up_id, score.score)`.
- `briefing_order(follow_ups)` — sort by `triage_score` desc for the morning
  briefing.

Other (Phase 1 scaffolds): `scan_inboxes`, `find_follow_ups_due`,
`draft_follow_up`, `send_follow_up`, `index_email`. Sending always requires
explicit approval — Loop never auto-sends.

### 8.2 Calendar Specialist (`specialists/calendar.py`)

`CalendarSpecialist(settings=None, google_calendar=None, outlook_calendar=None,
delivery=None)`.

- `CalendarEvent(event_id, title, start, end, attendees=[], location="",
  is_personal=False)`.
- `ConflictWarning(event1, event2, overlap_minutes)` with `describe()` that
  masks personal event titles as `(private)`.

**Conflict detection (Phase 3):**
- `detect_conflicts(events) -> list[ConflictWarning]`: sort by start, pairwise
  sweep, report any positive overlap (`_overlap_minutes`). Early-break once a
  later event starts after the current one ends.
- `_same_event(a, b)`: dedup heuristic — same start (±60 s) and title similarity
  ≥ 0.85 (`difflib.SequenceMatcher`).
- `get_all_events_today()` (async): fetch from each configured connector's async
  `todays_events()`, merge, dedup.
- `check_and_warn_conflicts()` (async): detect conflicts across all calendars
  and, if a delivery layer is wired, push an immediate warning message. Returns
  the conflicts found.

Scaffolds: `todays_events`, `schedule_reminders`, `morning_briefing`.

### 8.3 Task Specialist (`specialists/tasks.py`)

Captures tasks from chat/CLI, persists via `MemoryStore`, and builds the
end-of-day summary (`end_of_day_summary() -> str`) consumed by the scheduler.
Later phases sync with Wrike.

### 8.4 Knowledge Specialist (`specialists/knowledge.py`)

Answers questions grounded in the Obsidian vault using `VectorStore` retrieval +
the LLM router. Private-vault notes force local-only inference.

---

## 9. Integrations (`integrations/`) & Delivery (`delivery/`)

Integrations are thin async clients; delivery channels push outbound messages.
All are configured via `Settings` and degrade gracefully when unconfigured.

- **gmail.py / outlook.py** — read recent/unread threads, detect user-sent
  threads without replies, send approved follow-ups.
- **google_calendar.py / outlook_calendar.py** — expose async `todays_events()`
  returning `CalendarEvent`s.
- **obsidian.py** — read/watch the vault; feed notes to the vector store, tagging
  private-vault notes local-only.
- **wrike.py** — task sync (API key provided later).
- **telegram_bot.py / teams_bot.py** — inbound chat listeners.
- **delivery/telegram.py / delivery/teams.py** — outbound `send(text)` (sync or
  async); one failing channel must not block others.

---

## 10. Interfaces

### 10.1 CLI (`cli/main.py`, Typer, entry point `loop`)

- `loop status` — health: integrations, scheduler, open items.
- `loop briefing` — print the morning briefing.
- `loop snooze <item> [--hours N]` — snooze a reminder/follow-up.
- `loop find <query> [--limit N]` — semantic search over notes + email
  summaries (wired to `VectorStore.semantic_search`).
- `loop ask <question> [--local/-l] [--session/-s <id>]` — free-form Q&A via
  `Orchestrator.ask`. `--local` forces local-only; prints the answer plus a
  dim `[backend | local_only | sources]` metadata line. Runs the async
  orchestrator with `asyncio.run` and surfaces a friendly error if Ollama is
  down.

### 10.2 Web dashboard (`web/main.py`, FastAPI + Jinja2 + HTMX + Tailwind CDN)

Read endpoints:
- `GET /` — today's briefing (meetings, flagged emails, tasks due/overdue).
- `GET /follow-ups` — flagged threads (ranked by triage score).
- `GET /tasks` — open tasks (overdue highlighted).
- `GET /search?q=` — semantic search; returns the `search_results.html` fragment
  for HTMX requests (detected via the `HX-Request` header), the full page
  otherwise.
- `GET /health` — liveness probe.

Write endpoints (Phase 3 — interactive v2):
- `POST /follow-ups/{id}/approve` — mark `sent`.
- `POST /follow-ups/{id}/snooze` (form `hours`, default 24) — snooze.
- `POST /follow-ups/{id}/ignore` — mark `ignored`.
- `POST /tasks` (form `description`, `priority`) — create a task.
- `POST /tasks/{id}/complete` — complete a task.

All write endpoints perform the action then redirect (HTTP 303) back to the
listing, so the dashboard works with or without JavaScript. Forms require
`python-multipart`. Templates extend `base.html`.

---

## 11. Deployment & packaging

### 11.1 Docker Compose (`docker-compose.yml`)

Services: `ollama` (LLM runtime + volume), `ollama-init` (one-shot model pull of
`llama3.1:8b` + `nomic-embed-text`), `chromadb` (vector store + volume), `app`
(the assistant; `env_file: .env`, mounts `./data`, points at in-network
`ollama`/`chromadb`). Named volumes `ollama` and `chroma` persist state.

**Per-user isolation:** `new_user.sh` writes
`COMPOSE_PROJECT_NAME=loop-<user>` into each instance's `.env`; Compose reads it
automatically so containers, network, and volumes never collide between peers.

### 11.2 Scripts (`scripts/`)

- `setup.sh` — verifies Docker + Compose are installed and running, creates
  `.env` from the template (prompting for Telegram/Anthropic/Obsidian values,
  backing up any existing `.env`), `docker compose up -d`, and pulls the Ollama
  models. Idempotent and re-runnable. Portable `sed`, passes `shellcheck`.
- `new_user.sh <username> [git-remote] [parent-dir]` — provisions an isolated
  peer instance: clones (or `git archive`-copies) the repo into
  `loop-<slug>`, sets a unique `COMPOSE_PROJECT_NAME`, then delegates to
  `setup.sh`.

### 11.3 Local (non-Docker) run

`python -m venv .venv && pip install -e .` then `loop --help`;
`uvicorn web.main:app --port 8000` for the dashboard. Requires a reachable
Ollama.

---

## 12. Cross-cutting concerns

- **Privacy gate** (see §7.2) — the single most important invariant. Private
  requests fail closed.
- **Auditability** — `llm_usage_log` records every call by backend + prompt hash
  (never content).
- **Error handling** — background jobs and dashboard endpoints swallow and log
  exceptions so a single failure never crashes the process.
- **Time & timezones** — all scheduling honours `settings.timezone`; store UTC
  timestamps in SQLite.
- **Secrets** — only ever in `.env` (gitignored); never logged, never sent
  anywhere but the provider APIs.
- **Async discipline** — LLM and network I/O is async; APScheduler jobs bridge
  to async via `asyncio.run`.

---

## 13. Reproduction guide (build order for an LLM)

To recreate Loop from scratch, implement in this order — each step is
independently testable:

1. **Scaffold** the repo layout (§4), `pyproject.toml` with the deps in §5, and
   `config/.env.example` mirroring §6.
2. **`config/settings.py`** — the `Settings` model (§6) + cached
   `get_settings()`.
3. **`core/exceptions.py`** — `PrivacyError`, `BackendUnavailableError`.
4. **`core/memory.py`** — ORM models + `MemoryStore` + `ConversationMemory`
   (§7.3). Test with a temp SQLite file.
5. **`core/vector_store.py`** — ChromaDB wrapper (§7.4).
6. **`core/llm_router.py`** — implement `route_async` with the privacy gate and
   fallback (§7.2). Unit-test: local-only raises `PrivacyError` when Ollama is
   down; low-confidence output escalates; refusal markers trigger fallback.
7. **Specialists** — email triage (§8.1), calendar conflicts (§8.2), tasks,
   knowledge. Each is pure logic over injected collaborators; test with fakes.
8. **`core/scheduler.py`** — interval/cron helpers + conflict sweep (§7.5).
9. **`core/orchestrator.py`** — the `ask` RAG pipeline (§7.6). Test with fake
   router/vectors/conversation.
10. **Integrations & delivery** — async clients behind the specialists (§9).
11. **CLI** (§10.1) and **web dashboard** (§10.2). Test the web with FastAPI's
    `TestClient` (needs `python-multipart`).
12. **Packaging** — `docker-compose.yml`, `Dockerfile`, `scripts/setup.sh`,
    `scripts/new_user.sh` (§11). `shellcheck` the scripts.

**Acceptance checks (must all hold):**
- A `local_only` request never produces a cloud call, even when Ollama fails
  (it raises `PrivacyError`).
- A weak local answer on a non-private request escalates to Anthropic and sets
  `fell_back=True`.
- `EmailSpecialist.triage_email` scores a VIP "URGENT deadline today" thread at
  the top and a promo newsletter near the bottom.
- `CalendarSpecialist.detect_conflicts` finds overlapping pairs and masks
  private titles; `get_all_events_today` dedups cross-provider duplicates.
- `loop ask` returns an answer plus backend/privacy/source metadata and persists
  both conversation turns.
- The web dashboard can create/complete a task and approve/snooze/ignore a
  follow-up, each redirecting (303) back to its listing.
- Every LLM call appears in `llm_usage_log` with a prompt *hash* (no content).

---

## 14. Phased roadmap (historical context)

- **Phase 1** — scaffolding: settings, memory schema, specialist/integration
  stubs, CLI skeleton, privacy-gate design.
- **Phase 2** — semantic memory (ChromaDB), `loop find`, read-only web
  dashboard, Obsidian indexing, dependency wiring.
- **Phase 3** (this spec's baseline) — async Anthropic/Ollama router with usage
  logging; calendar conflict detection; email triage scoring; persistent
  conversation memory; free-form `loop ask`; interactive web dashboard v2;
  per-user packaging scripts; onboarding + this specification.
- **Future** — full autonomous (approval-gated) follow-up sending; Wrike task
  sync; multi-step planning; richer Teams/Telegram inbound handling; per-source
  PII sanitisation before any cloud call.

---

## 15. Glossary

- **Local-first** — always prefer the on-device model; cloud is a fallback.
- **Privacy gate** — the router logic that forbids cloud use for private data.
- **Specialist** — a domain sub-agent (email, calendar, tasks, knowledge).
- **Follow-up** — a tracked email thread awaiting a reply.
- **Triage score** — `urgency × importance` (1..25) ranking an email.
- **Session** — a conversation thread keyed by `session_id`, expiring after
  `session_ttl_hours`.
- **Instance** — one peer's fully isolated deployment of Loop.
```
