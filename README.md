# Loop

> A terminal-first, AI-driven **personal command centre** for daily work and life.
> Local-first, cloud-optional. One isolated instance per person.

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
```bash
# install Ollama, then pull the default models:
ollama pull llama3.1:8b          # local chat/reasoning model
ollama pull nomic-embed-text     # local embedding model (ChromaDB index)

pip install -e .        # installs the `loop` CLI + all deps (chromadb, watchdog, botbuilder…)
loop status
loop briefing

# optional web dashboard (read-only):
uvicorn web.main:app --reload --port 8000
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
| Command | Description | Phase |
|---|---|---|
| `loop status` | Health: integrations, scheduler, pending items | 1 |
| `loop briefing` | Today's meetings + flagged emails | 1 |
| `loop snooze <item> --hours N` | Snooze a reminder/follow-up | 1 |
| `loop find "<query>"` | Semantic search over Obsidian | 2 |
| `loop ask "<question>"` | Free-form query across all sources | 3 |

> **Note:** Phase 1 (foundation) and Phase 2 (knowledge & tasks) are implemented.
> `loop find` performs real semantic search once your vault is indexed and Ollama
> is running. Phases 3–4 remain scaffolded with `TODO(phaseN)` markers.

---

## Roadmap

| Phase | Focus | Status | Key deliverables |
|---|---|---|---|
| **1 — Foundation** | Stop forgetting meetings & emails | ✅ complete | Scaffold, Ollama setup, Telegram bot, Gmail/Outlook + both calendars, meeting reminder engine, morning briefing, email follow-up tracker, SQLite state, CLI (status/briefing/snooze) |
| **2 — Knowledge & Tasks** | Capture knowledge, never lose a task | ✅ complete | Obsidian watcher (watchdog), PARA + Zettelkasten formatter, ChromaDB + `nomic-embed-text` embeddings, `find` semantic search, chat task capture, end-of-day summary (17:30), Teams bot (Bot Framework), hard privacy gate, read-only web dashboard |
| **3 — Depth & Peers** | Roll out to peers, get smarter | 🔜 planned | Per-user packaging, Anthropic fallback tuning, conflict detection, email triage scoring, conversation memory, `ask` command, interactive web UI |
| **4 — Polish & Automation** | Act more autonomously | 🔜 planned | Autonomy levels, weekly review, project-context awareness, Wrike bidirectional sync, voice-to-note, metrics dashboard |

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

## License

MIT
