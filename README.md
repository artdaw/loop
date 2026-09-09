# Loop

> A terminal-first, AI-driven **personal command centre** for daily work and life.
> Local-first, cloud-optional. One isolated instance per person.

The **[Loop vNext specification](docs/SPECIFICATION.md)** defines the proposed
persistent agent team, durable tasks and reminders, the vault knowledge workflows,
proactive routines, and learning. Its contracts cover
[the vault](docs/specification/vault.md), [runtime](docs/specification/runtime.md),
[interfaces and deployment](docs/specification/interfaces.md), and
[acceptance scenarios](docs/specification/acceptance.md).
The [travel itinerary pack](docs/specification/capabilities/travel.md) adds
personalized trip research, feasible daily plans, comparison, and optional monitoring.
The [weather pack](docs/specification/capabilities/weather.md) compares sources
and prefers suitable local meteorological services. Both use the
[shared extension contract](docs/specification/capabilities/README.md) and
[starter template](docs/specification/capabilities/template/README.md).
To implement the full target with Claude Code or another coding agent, use the
[single Claude implementation document](docs/CLAUDE_IMPLEMENTATION.md).
It includes the approved architecture review, complete contracts, remaining work
and progress ledger. Superseded execution documents are preserved in
[the dated archive](docs/archive/pre-consolidation-2026-09-06/README.md).
Specification 1.3 standardizes the [agent stack](docs/specification/agent-stack.md)
on LangChain and LangGraph, with generic capability runners and durable recovery.

**vNext is now implemented** and ships as `loop-next`, alongside the legacy
`loop` command described further down. Milestones M0–M7 are complete: the
composition root, durable coordinator and workers, persistent stores, the three
interfaces, the vault and weather workflows, real adapters, the learning loop
and the release gate. See [what works today](#what-works-today-loop-next) for
the honest state, including what is deliberately not built.

Loop watches the tools you already use — Gmail, Outlook, Google/Outlook Calendar,
Wrike, Obsidian — and delivers reminders and follow-ups where you already live:
**Telegram and Microsoft Teams**. It runs a local LLM (Ollama) first and only
falls back to Anthropic when needed. Data you mark private never leaves your machine.

The two pain points that drive every design decision:

| Pain point | How Loop helps |
|---|---|
| Forgotten meetings | 30-min + 5-min reminders and an 08:00 morning briefing |
| Un-followed-up emails | Flags stalled threads and drafts a follow-up for your approval |

---

## What works today (`loop-next`)

`loop-next` is the vNext stack. It is a **separate entry point** rather than a
replacement for `loop`: the legacy CLI's autonomy, sync, voice and metrics
commands have no vNext equivalent yet, and interfaces are kept until their
replacements match their behaviour.

```bash
uv sync
loop-next status          # works with nothing configured at all
```

| Command | What it does |
|---|---|
| `loop-next status` | Pending triggers, jobs and messages; enabled capabilities; configuration limitations, stated plainly |
| `loop-next say "<text>"` | An ordinary sentence — captures a fact, saves a task, or asks the one question it needs |
| `loop-next remind <text>` | Persist a task and its reminder before confirming it |
| `loop-next task add/list/complete` | Durable commitments with optimistic versioning |
| `loop-next vault capture/compile/ask/reindex` | Exact capture → sourced wiki page → cited answer |
| `loop-next routine add/list/activate/pause` | Recurring routines; saving is not activating |
| `loop-next learning review/confirm/forget/snooze` | What Loop has inferred, and what it proposes — never applied on its own |
| `loop-next capability init/validate/test/list/enable/disable` | Scaffold, test, and manage capability packs |
| `loop-next do <operation> <json>` | Invoke any enabled capability; adding a pack needs no new command |
| `loop-next run once [--sweep-only]` | Sweep, run queued work, deliver |
| `loop-next run daemon` | The durable scheduler and workers until SIGTERM |
| `loop-next backup --output <dir>` / `restore --from <dir> --target <dir>` | Domain state, graph checkpoints and vault as one verified pair |
| `loop-next-api` | HTTP API (bearer token required, even on localhost) |
| `loop-next-telegram` | The Telegram bot |

Three interfaces, **one service**. The CLI, the HTTP API and the bot construct
their services from a single `build_application()`, so they cannot drift into
separate ideas of what completing a task means. `tests/vnext/test_interface_parity.py`
does the same operation three ways and compares the results against each other.

### Try it

```bash
scripts/demo.sh          # 30 checks through the installed commands, on a throwaway vault
scripts/acceptance_run.sh # the full release gate
```

`demo.sh` drives `loop-next` itself, so it fails when a shipped command drifts
from what the walkthrough claims — which a transcript in a document cannot do.
Steps needing a network or an account are named and skipped rather than omitted.

### What is deliberately not built

An unconfigured provider is a fact about your machine; a missing one is a fact
about the software. These are the second kind, and they are named rather than
stubbed:

- **No fare, availability or transport-schedule provider.** Every source worth
  trusting needs an account, and an adapter that incurs charges requires
  configured budget authority first. A missing price is reported as a missing
  *provider*, never as a search that found nothing.
- **No calendar or mail transport on the vNext stack yet** — those still live
  in the legacy commands.
- **Telegram**: `/remind`, `/travel`, `/trips`, `/briefing`, `/cancel`, voice
  notes and inline-button delivery are not wired. `/help` names them as
  unavailable rather than leaving you to discover it.
- **HTTP**: search, research, weather, trips, approvals, memory, activity and
  policy routes are absent.

Acceptance status is tracked row by row in
[the acceptance matrix](docs/ACCEPTANCE_MATRIX.md): **179 of 196 scenarios
verified**, 17 `implemented` (code exists; the scenario is not proven at the
level its required result demands). `scripts/audit_acceptance.py` enforces that
distinction on every release run, so a `verified` label cannot quietly outrun
its evidence.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     DELIVERY LAYER                        │
│            Telegram Bot        MS Teams Bot               │
└────────────────────┬─────────────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────────────┐
│                 ORCHESTRATOR (Agent Core)                 │
│   Local LLM (Ollama) ─▶ LLM Router ─▶ Anthropic (cloud)   │
│                       (privacy gate)                       │
│                                                           │
│   Email     Calendar     Tasks      Knowledge             │
│   Specialist Specialist  Specialist Specialist            │
└────────────────────┬─────────────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────────────┐
│                  INTEGRATION LAYER                        │
│  Gmail  Outlook  G-Cal  O-Cal  Teams  Telegram  Wrike     │
│                      Obsidian                              │
└────────────────────┬─────────────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────────────┐
│                LOCAL PERSISTENCE LAYER                    │
│   SQLite (state)   ChromaDB (vectors)   Obsidian (md)     │
└─────────────────────────────────────────────────────────┘
```

**Privacy gate:** any item flagged local-only (private Obsidian vaults, personal
calendar events, personal Telegram chats) is processed by the local Ollama model
only — it is never sent to a cloud LLM.

---

## Project layout

```
loop/
├── core/           # orchestrator, llm_router, memory, scheduler
├── specialists/    # email, calendar, tasks, knowledge sub-agents
├── integrations/   # gmail, outlook, calendars, telegram, teams, wrike, obsidian
├── delivery/       # outbound telegram + teams formatting/sending
├── cli/            # Typer CLI (loop status | briefing | snooze | find | ask)
├── web/            # optional FastAPI + HTMX dashboard
├── config/         # Pydantic settings + .env.example
├── data/           # local SQLite + ChromaDB (gitignored)
├── docker-compose.yml
├── Dockerfile
└── pyproject.toml
```

---

## Quick start

### 1. Prerequisites
- Python 3.12+
- [Ollama](https://ollama.com) for the local LLM
- Docker + Docker Compose (recommended for the full stack)

### 2. Configure
```bash
git clone https://github.com/artdaw/loop.git
cd loop
cp config/.env.example .env
# edit .env and fill in your tokens / paths
```

### 3a. Run with Docker (recommended)
Pulls the local models, starts ChromaDB, and runs the app. The image's default
command is the **vNext scheduler** (`loop-next run daemon`), which sweeps
triggers, drains routine and coordinator jobs, and releases its leader lease on
SIGTERM:
```bash
docker compose up -d
docker compose run --rm app loop-next status
docker compose run --rm app loop briefing      # the legacy CLI, still available
```

### 3b. Run locally (without Docker)
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first.

```bash
# install Ollama, then pull the default models:
ollama pull llama3.1:8b          # local chat/reasoning model
ollama pull nomic-embed-text     # local embedding model (ChromaDB index)

uv venv --python 3.12
source .venv/bin/activate
uv pip install -e .        # installs the `loop` CLI + all deps (chromadb, watchdog, botbuilder…)
uv pip install -e ".[voice]"   # optional: local voice-to-note (faster-whisper)
uv pip install -e ".[dev]"     # optional: pytest, ruff, mypy
loop status
loop briefing

# optional web dashboard (read-only):
.venv/bin/uvicorn web.main:app --reload --port 8000
```

### Phase 2 setup notes

**ChromaDB (semantic search).** Vectors are stored locally under
`CHROMA_PERSIST_DIR` (default `data/chroma`). No server is required for the CLI —
`loop find "<query>"` opens the persistent store directly. Embeddings are
produced by the local `nomic-embed-text` model via Ollama, so **Ollama must be
running** for indexing and search. Index your vault once with a running watcher
(new/modified notes are indexed automatically) or programmatically via
`KnowledgeSpecialist().index_vault()`.

**Obsidian watcher (watchdog).** Set `OBSIDIAN_VAULT_PATH` to your vault. Notes
that must never reach a cloud model live in a separate vault at
`OBSIDIAN_PRIVATE_VAULT_PATH` — every change there is flagged `local_only=True`.
The watcher (`integrations.obsidian.ObsidianWatcher`) emits an event to the
Knowledge Specialist on every new/modified `.md` file.

**Teams bot registration.** The Teams bot is a Bot Framework webhook:
1. Create an **Azure Bot** resource and an **App Registration**; copy the app id
   and client secret into `TEAMS_APP_ID` / `TEAMS_APP_PASSWORD`.
2. Set the bot's messaging endpoint to `https://<your-host>/api/messages`.
3. Run the aiohttp app that `integrations.teams_bot.TeamsBot.build_app()`
   returns (e.g. behind a tunnel/reverse proxy during development).
Proactive reminders and follow-up prompts are sent via
`TeamsBot.send_proactive(conversation_ref, message)`.

### Legacy CLI commands (`loop`)

These are the Phase 1–4 commands. They are preserved because their vNext
replacements do not yet match them — see
[what works today](#what-works-today-loop-next) for the `loop-next` surface.

Start the legacy Telegram bot locally with `loop telegram`. It accepts messages only
from `TELEGRAM_CHAT_ID` and supports `/status`, `/briefing`, `/task <text>`,
`/tasks`, and `/done <id>`. Plain text is sent to Loop's assistant; private-chat
questions are forced through the local model. In Docker, the `app` service runs
the bot automatically.

| Command | Description | Phase |
|---|---|---|
| `loop status` | What is wired up, what is not, what is waiting | 1 |
| `loop briefing` | Today's meetings, flagged threads, tasks due | 1 |
| `loop telegram` | Run the private Telegram bot with long polling | 1 |
| `loop snooze <id> --hours N` | Hide a follow-up from the briefing | 1 |
| `loop find "<query>"` | Semantic search over Obsidian | 2 |
| `loop ask "<question>"` | Free-form query across all sources | 3 |
| `loop autonomy` | Show how autonomously Loop may act, per action | 4 |
| `loop autonomy-set <action> <level>` | Change one action's autonomy level | 4 |
| `loop review` | Weekly review: what you finished, what slipped | 4 |
| `loop sync [--dry-run]` | Bidirectional Wrike task sync | 4 |
| `loop metrics [--days N]` | Local-vs-cloud, task and follow-up metrics | 4 |
| `loop note-from-audio <path>` | Transcribe a voice memo into a vault note | 4 |

> **Note:** Phases 1–4 are implemented, and everything that reads local state
> works today — `status`, `briefing`, `telegram`, `snooze`, `find`, `ask`,
> `review`, `sync`, `metrics`, and the dashboard.
>
> What remains stubbed is the **external transport**: the Gmail, Outlook,
> and Google/Outlook Calendar clients are still
> `TODO(phase1)`. So Loop can rank, schedule, and draft around your mail and
> meetings, but cannot yet fetch or send them. `loop briefing` says so plainly
> rather than rendering an empty diary — an assistant that quietly omits your
> meetings is worse than one that admits it cannot see them.

---

## Roadmap

| Phase | Focus | Status | Key deliverables |
|---|---|---|---|
| **1 — Foundation** | Stop forgetting meetings & emails | ✅ complete | Scaffold, Ollama setup, Telegram bot, Gmail/Outlook + both calendars, meeting reminder engine, morning briefing, email follow-up tracker, SQLite state, CLI (status/briefing/snooze) |
| **2 — Knowledge & Tasks** | Capture knowledge, never lose a task | ✅ complete | Obsidian watcher (watchdog), PARA + Zettelkasten formatter, ChromaDB + `nomic-embed-text` embeddings, `find` semantic search, chat task capture, end-of-day summary (17:30), Teams bot (Bot Framework), hard privacy gate, read-only web dashboard |
| **3 — Depth & Peers** | Roll out to peers, get smarter | ✅ complete | Per-user packaging, Anthropic fallback tuning, conflict detection, email triage scoring, conversation memory, `ask` command, interactive web UI |
| **4 — Polish & Automation** | Act more autonomously | ✅ complete | Autonomy levels (a second gate, orthogonal to privacy), weekly review, project-context awareness, Wrike bidirectional sync (remote-wins), local voice-to-note, metrics dashboard, first test suite |

### vNext milestones (`loop-next`)

| Milestone | Focus | Status |
|---|---|---|
| **M0–M1** | Role-scoped coordinator, typed plans, structured output, Reviewer repair | ✅ done |
| **M2** | Async graph lifecycle, secure checkpoints, durable worker dispatch, child runs | ✅ done |
| **M3** | Eight in-memory stores replaced by durable repositories | ✅ done |
| **M4** | Composition root; CLI, HTTP API and Telegram over shared services; shipped packs | ✅ done |
| **M5** | Capture → ledger → sourced wiki → cited answer; routine → weather → advice → outbox, across a real process restart | ✅ done |
| **M6** | CAP warning adapters, travel research providers, the learning loop, scoped trip monitoring | ✅ done |
| **M7** | Acceptance re-audit, release gate, reproducible demo | ✅ done |

Remaining work is tracked as named gaps rather than phases — see
[what is deliberately not built](#what-is-deliberately-not-built) and the
`implemented` rows in [the acceptance matrix](docs/ACCEPTANCE_MATRIX.md).

---

## Configuration reference

All settings live in `.env` (see `config/.env.example`). Highlights:

- **Telegram:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- **Gmail/Google:** `GMAIL_CREDENTIALS_PATH`, `GMAIL_TOKEN_PATH`
- **Outlook/Graph:** `OUTLOOK_CLIENT_ID`, `OUTLOOK_CLIENT_SECRET`, `OUTLOOK_TENANT_ID`
- **Teams:** `TEAMS_APP_ID`, `TEAMS_APP_PASSWORD`
- **Wrike (Phase 2):** `WRIKE_API_KEY`
- **Local LLM:** `OLLAMA_BASE_URL`, `OLLAMA_DEFAULT_MODEL`, `OLLAMA_EMBED_MODEL`
- **Cloud fallback:** `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`
- **Obsidian:** `OBSIDIAN_VAULT_PATH`
- **Privacy:** `PRIVATE_VAULTS`, `PRIVATE_PERSONAL_CALENDAR`, `PRIVATE_TELEGRAM_PERSONAL`
- **Autonomy (Phase 4):** `DEFAULT_AUTONOMY_LEVEL`, `MAX_AUTONOMY_EMAIL_SEND`
- **Projects (Phase 4):** `PROJECTS`
- **Weekly review (Phase 4):** `WEEKLY_REVIEW_DAY`, `WEEKLY_REVIEW_TIME`
- **Wrike sync (Phase 4):** `WRIKE_SYNC_MINUTES`, `WRIKE_FOLDER_ID`
- **Voice-to-note (Phase 4):** `WHISPER_MODEL_SIZE`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`
- **vNext HTTP API:** `HTTP_BIND`, `HTTP_PORT`, `API_BEARER_TOKEN` — blank by
  default, so a fresh install refuses every request rather than opening itself
- **vNext capability discovery:** `CAPABILITY_PATHS` (defaults to
  `packs`, `capabilities`, `data/capabilities`)

### Behaviour lives in the vault, not in `.env`

Deployment configuration is environment; *personal behaviour* is the owner's to
edit without a deploy, and it is personal data besides. So named locations and
official warning feeds are read from the vault manifest
(`_ctx/loop/manifest.yaml`), not from environment variables:

```yaml
locations:
  home:
    latitude: 52.52
    longitude: 13.405
    timezone: Europe/Berlin
warning_feeds:
  - country: de          # MeteoAlarm, per ISO 3166-1 alpha-2 code
    language: en
```

Unconfigured means the warning state stays `unknown` — never an all-clear from
a feed nobody chose.

Secrets are never committed — `.env`, `credentials.json`, and `token.json` are gitignored.

---

## Privacy

Loop is **local-first**. Every LLM request passes through the privacy gate in
`core/llm_router.py` (`LLMRouter.route(prompt, context_metadata)`):

- **Local-only, fail-closed.** If `context_metadata["local_only"]` is `True`, or
  `source` is one of `obsidian_private`, `personal_calendar`, or
  `telegram_private`, the request is served by the **local Ollama model only**.
  If Ollama is unavailable, the router raises `PrivacyError` (from
  `core/exceptions.py`) — it **never** falls back to a cloud model. Private data
  cannot leak, even during an outage.
- **Local-first everywhere else.** For non-private work data, Loop tries Ollama
  first and only escalates to Anthropic when the local answer looks weak
  (response shorter than `LOCAL_MIN_RESPONSE_CHARS`, contains a refusal phrase,
  or exceeds `LOCAL_LATENCY_BUDGET_SECONDS`).
- **Local embeddings.** All vector embeddings use `nomic-embed-text` via Ollama
  and are stored in a local ChromaDB (`data/chroma`). Nothing is embedded in the
  cloud, so even indexing private notes stays on your machine.
- **Private vault.** Notes under `OBSIDIAN_PRIVATE_VAULT_PATH` are flagged
  `local_only=True` end-to-end: watcher → Knowledge Specialist → LLM router.

The result: **your private notes, personal calendar, and personal chats are
processed locally and never sent to a third-party AI provider.**

---

## Autonomy

Loop has a **second gate**, deliberately separate from the privacy gate:

| Gate | Question | Failure mode |
|---|---|---|
| Privacy (`core/llm_router.py`) | *Which model may see this data?* | fails **closed** — raises `PrivacyError` |
| Autonomy (`core/autonomy.py`) | *May Loop do this without asking?* | fails to **asking** |

They are independent. A voice memo is private *and* safe to file automatically;
an email to a colleague is not private but must never go out unattended. Using
one as a proxy for the other would make both harder to reason about.

Four levels, set per action (`email_send`, `task_create`, `wrike_write`,
`note_write`, `calendar_write`, `notify`):

| Level | Behaviour |
|---|---|
| `observe` | record only; never surface proactively |
| `suggest` | surface a suggestion, prepare nothing |
| `approve` | prepare the action, wait for your approval **(default)** |
| `act` | execute autonomously, then log it |

```bash
loop autonomy                          # show the current table
loop autonomy-set note_write act       # file voice notes without asking
loop autonomy-set default suggest      # change the global default
```

**Outbound email has a hard ceiling.** `MAX_AUTONOMY_EMAIL_SEND` (default
`approve`) is applied *on top of* whatever level is set, so Loop cannot be
talked into sending mail unattended from the dashboard — lifting it is a
deliberate edit to `.env`. Every gated action is recorded in `autonomy_audit`
with the action, level, and outcome — never the content.

---

## Development

```bash
uv sync                     # or: uv venv --python 3.12 && uv pip install -e ".[dev]"
pytest          # 2,152 passing tests, hermetic: no network, model, keys, or .env
ruff check .
mypy .
```

Tests use temp SQLite databases and fakes throughout — nothing in the suite
needs a running model, a vault, or an account.

### The release gate

```bash
scripts/acceptance_run.sh
```

Runs, failing cheapest first: lint, types, the hermetic suite, the acceptance
matrix totals, the **matrix audit**, packaging and wheel checks, restart
durability, and the demo through the installed commands.

`scripts/audit_acceptance.py` is the part worth knowing about. A green suite is
not coverage, so it asks two things of every acceptance row: does the test it
names actually exist and get collected, and does that test run at the level the
row's required result demands? A scenario needing a restart, a delivered
message or a shipped command cannot be `verified` by a unit test. Both answers
come from pytest and the test files themselves, never from the label.

Docker is the final mandatory release check. The release script fails when the
daemon is unavailable; ordinary hermetic test runs still skip Docker-only tests:

```bash
open -a Docker && LOOP_PACKAGING_TESTS=1 pytest tests/vnext/test_packaging.py -k o03
```

### Conventions in this codebase

- **Durable by default.** Anything a restart must not forget — idempotency
  records, condition edges, pending clarifications, callback buttons — lives in
  SQLite, not in a process. The failures that motivate this are in the commit
  messages, which are worth reading.
- **Never claim what did not happen.** A capture says "saved" only when the
  file *and* the ledger row exist; an unconfigured provider is reported, not
  omitted; an uncertain send stays uncertain rather than being resent.
- **Mutation-tested where it matters.** New behaviour is checked by breaking it
  deliberately and confirming a test fails. Survivors are either fixed with a
  real test or recorded in a comment as a known redundancy.

---

## License

MIT
