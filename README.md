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
ollama pull llama3.1:8b
ollama pull nomic-embed-text

pip install -e .        # installs the `loop` CLI
loop status
loop briefing

# optional web dashboard:
uvicorn web.main:app --reload --port 8000
```

### CLI commands
| Command | Description | Phase |
|---|---|---|
| `loop status` | Health: integrations, scheduler, pending items | 1 |
| `loop briefing` | Today's meetings + flagged emails | 1 |
| `loop snooze <item> --hours N` | Snooze a reminder/follow-up | 1 |
| `loop find "<query>"` | Semantic search over Obsidian | 2 |
| `loop ask "<question>"` | Free-form query across all sources | 3 |

> **Note:** this is the Phase 1 scaffold. Every module is a runnable stub with
> clearly marked `TODO(phaseN)` comments describing what each phase implements.

---

## Roadmap

| Phase | Focus | Key deliverables |
|---|---|---|
| **1 — Foundation** | Stop forgetting meetings & emails | Scaffold, Ollama setup, Telegram bot, Gmail/Outlook + both calendars, meeting reminder engine, morning briefing, email follow-up tracker, SQLite state, CLI (status/briefing/snooze) |
| **2 — Knowledge & Tasks** | Capture knowledge, never lose a task | Obsidian watcher, PARA + Zettelkasten formatter, ChromaDB + embeddings, `find` search, chat task capture, Wrike connector, Teams bot, privacy gate, read-only web dashboard |
| **3 — Depth & Peers** | Roll out to peers, get smarter | Per-user packaging, Anthropic fallback, conflict detection, email triage scoring, conversation memory, `ask` command, interactive web UI |
| **4 — Polish & Automation** | Act more autonomously | Autonomy levels, weekly review, project-context awareness, Wrike bidirectional sync, voice-to-note, metrics dashboard |

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

## License

MIT
