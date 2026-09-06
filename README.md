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
This is the target design; the legacy setup and feature descriptions below do
not imply that vNext is implemented.

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
Pulls the local models, starts ChromaDB, and runs the app:
```bash
docker compose up -d
docker compose run --rm app loop briefing
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

### CLI commands

Start the Telegram bot locally with `loop telegram`. It accepts messages only
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
uv venv --python 3.12       # if you have not created the environment yet
source .venv/bin/activate
uv pip install -e ".[dev]"
pytest          # 288 tests, hermetic: no network, model, keys, or .env
ruff check .
mypy .
```

Tests use temp SQLite databases and fakes throughout — nothing in the suite
needs a running model, a vault, or a Wrike key.

---

## License

MIT
