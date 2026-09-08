# Loop — Peer Onboarding Guide

Welcome! This guide walks you through standing up **your own private instance**
of Loop, the terminal-first, local-first AI personal assistant. Every peer runs
a fully isolated instance — your email, calendar, notes, and chat data never
leave your machine or mix with anyone else's.

> **Time to first briefing:** ~15 minutes, most of it spent waiting for the
> local models to download.

---

> **Two stacks, one repository.** This guide sets up the original `loop`
> commands. The vNext stack ships alongside it as `loop-next` — durable tasks
> and reminders, the vault capture/compile/answer workflow, routines, weather
> and the learning loop. Once you have finished here, `loop-next status` works
> immediately and `scripts/demo.sh` walks the whole surface on a throwaway
> vault. See the README's [what works today](../README.md#what-works-today-loop-next).

## 1. What you're setting up

Loop is a small stack that runs entirely on your own hardware:

| Component        | What it does                                              |
|------------------|----------------------------------------------------------|
| **app**          | The assistant: CLI (`loop …`) + web dashboard            |
| **ollama**       | Local LLM runtime (llama3.1:8b) + embeddings             |
| **chromadb**     | Local vector store for semantic search over your notes   |
| **SQLite**       | Local database for tasks, follow-ups, and memory         |

Loop is **local-first**: it always tries the local model first and only falls
back to Anthropic (Claude) for hard, *non-private* work — and never for anything
you mark private.

---

## 2. Prerequisites

Install these before you start:

- **Docker** and **Docker Compose** — <https://docs.docker.com/get-docker/>
  (Docker Desktop includes Compose.)
- **Git** — to clone the repository.
- ~10 GB free disk for the local models and vector store.
- Optional, only if you want cloud fallback: an **Anthropic API key**
  (<https://console.anthropic.com/>).

Verify Docker is running:

```bash
docker info
```

---

## 3. Quick start (recommended)

Clone the repo and run the one-command setup:

```bash
git clone https://github.com/artdaw/loop.git
cd loop
./scripts/setup.sh
```

`setup.sh` will:

1. Check Docker + Compose are installed and running.
2. Create your personal `.env` from `config/.env.example`.
3. Prompt you for the handful of secrets Loop needs (you can skip any and add
   them later).
4. Start the stack (`ollama`, `chromadb`, `app`).
5. Pull the local models `llama3.1:8b` and `nomic-embed-text`.

When it finishes, open the dashboard at **<http://localhost:8000>**.

### Provisioning a brand-new peer

If you're setting Loop up for someone else (or want a second isolated instance),
use `new_user.sh`. It clones Loop into its own directory and gives it a private
Docker Compose project so nothing is shared:

```bash
./scripts/new_user.sh alice https://github.com/artdaw/loop.git
```

Each instance lives under `~/loop-instances/loop-<name>` with its own `.env`,
database, vectors, and containers.

---

## 4. Configuration (`.env`)

`setup.sh` creates `.env` for you, but you can edit it any time. After changing
it, apply the changes with `docker compose up -d`. Key settings:

### Telegram (chat delivery)

```dotenv
TELEGRAM_BOT_TOKEN=...      # create a bot via @BotFather
TELEGRAM_CHAT_ID=...        # your personal chat id
```

To find your chat id: message your new bot, then visit
`https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`.

### Gmail / Google Calendar

1. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project and enable the **Gmail API** and **Google Calendar API**.
2. Create **OAuth client credentials** of type *Desktop app* and download the
   JSON as `credentials.json` in the project root.
3. Point Loop at it:
   ```dotenv
   GMAIL_CREDENTIALS_PATH=credentials.json
   GMAIL_TOKEN_PATH=token.json
   ```
4. On first use, Loop opens a browser once to authorise; the token is cached in
   `token.json` (gitignored).

### Outlook / Microsoft Graph

1. Register an app in the [Azure Portal](https://portal.azure.com/) → *App
   registrations*.
2. Add the Microsoft Graph delegated permissions `Mail.Read`, `Mail.Send`,
   `Calendars.Read`.
3. Copy the values into `.env`:
   ```dotenv
   OUTLOOK_CLIENT_ID=...
   OUTLOOK_CLIENT_SECRET=...
   OUTLOOK_TENANT_ID=common
   ```

### Anthropic (cloud fallback — optional)

```dotenv
ANTHROPIC_API_KEY=...       # leave blank to stay 100% local
ANTHROPIC_MODEL=claude-3-5-sonnet-20241022
```

Leave the key blank and Loop never contacts the cloud at all.

### Obsidian knowledge base

```dotenv
OBSIDIAN_VAULT_PATH=/absolute/path/to/your/Vault
# A second, strictly local-only vault (never sent to any cloud LLM):
OBSIDIAN_PRIVATE_VAULT_PATH=/absolute/path/to/Private
```

---

## 5. Privacy configuration

Loop's privacy gate decides what may reach a cloud model. Defaults are
conservative — tighten or relax them in `.env`:

```dotenv
# Vault names/paths that must NEVER reach a cloud LLM (comma-separated).
PRIVATE_VAULTS=Personal,Journal
# Treat personal calendar events as local-only.
PRIVATE_PERSONAL_CALENDAR=true
# Treat 1:1 (non-group) Telegram chats as local-only.
PRIVATE_TELEGRAM_PERSONAL=true
```

Anything flagged private is answered **only** by the local model. If the local
model can't handle it, Loop tells you — it will **not** silently fall back to the
cloud for private content.

---

## 6. Using Loop

### CLI

Run commands through the container (or install locally, see below):

```bash
docker compose run --rm app loop status               # health + connectivity
docker compose run --rm app loop briefing             # today's morning briefing
docker compose run --rm app loop find "quarterly okrs" # semantic search
docker compose run --rm app loop ask "what meetings do I have today?"
docker compose run --rm app loop ask --local "summarise my journal note"
docker compose run --rm app loop snooze 42 --hours 4  # snooze a follow-up
```

- `loop ask` retrieves relevant notes/emails, blends in recent conversation
  history, and answers via the privacy-gated router.
- `--local` forces local-only inference regardless of the source.

### Web dashboard

Open **<http://localhost:8000>**:

- **Home** — today's briefing (meetings, flagged emails, tasks due).
- **Follow-ups** — approve, snooze, or ignore stalled email threads; items are
  ranked by their triage priority score (urgency × importance).
- **Tasks** — add tasks and mark them done.
- **Search** — semantic search across your notes and email summaries.

### Running the CLI outside Docker (optional)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
loop --help
```

You'll still need Ollama reachable (either the Docker one on
`http://localhost:11434` or a native install).

---

## 7. Day-to-day operations

```bash
docker compose logs -f app     # tail the assistant logs
docker compose ps              # what's running
docker compose restart app     # apply .env changes to the app
docker compose down            # stop everything (data is kept in volumes)
docker compose down -v         # stop AND delete all data volumes (careful!)
```

---

## 8. FAQ / Troubleshooting

**The dashboard won't load.**
Check the app is up: `docker compose ps`. Look at logs: `docker compose logs app`.

**`loop ask` says it can't reach the model.**
Ollama may still be pulling models. Check `docker compose logs ollama` and
confirm `docker compose exec ollama ollama list` shows `llama3.1:8b`.

**Nothing shows up in search.**
Your vault hasn't been indexed yet, or `OBSIDIAN_VAULT_PATH` is wrong. Confirm
the path in `.env`, then let the indexer run (or trigger a briefing).

**Can I run fully offline?**
Yes. Leave `ANTHROPIC_API_KEY` blank. Everything runs against the local model.

**How is my data isolated from other peers?**
Each instance has its own checkout, `.env`, SQLite DB, ChromaDB volume, and a
unique `COMPOSE_PROJECT_NAME`. There is no shared backend — nothing is
multi-tenant.

**How do I update to a new version?**
`git pull`, then `docker compose up -d --build`.

**Where are my secrets stored?**
Only in your local `.env` (gitignored). Loop never transmits credentials
anywhere except directly to the provider APIs you configured.

---

Happy looping. If something's unclear, check `docs/SPECIFICATION.md` for the full
technical design, or open an issue.
