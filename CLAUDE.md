# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Loop is

A terminal-first, **local-first** personal assistant for one user per instance (explicitly *not*
multi-tenant). It watches Gmail/Outlook, Google/Outlook Calendar, Obsidian and Wrike, and pushes
reminders, briefings and follow-up drafts to Telegram and MS Teams. A local LLM (Ollama) answers
first; Anthropic is a fallback that the privacy gate can hard-block.

`docs/SPECIFICATION.md` is the authoritative design document (§ numbering, RFC-2119 MUST/SHOULD) —
it is written to be sufficient to rebuild the system from scratch. Read the relevant section before
changing core behaviour, and **update it in the same change** when architecture, data model,
settings keys or module contracts change. `docs/onboarding.md` is the end-user setup guide.

## Commands

```bash
# Local dev (needs Python 3.12+ and a reachable Ollama)
pip install -e ".[dev]"          # installs the `loop` CLI + pytest/ruff/mypy
loop --help                      # or: python -m cli.main --help
uvicorn web.main:app --reload --port 8000

ruff check .                     # line-length 100, target py312
mypy .                           # ignore_missing_imports = true

# Docker stack (ollama + chromadb + app)
docker compose up -d
docker compose run --rm app loop briefing
docker compose logs -f app
./scripts/setup.sh               # one-command per-user setup (idempotent)
./scripts/new_user.sh <name>     # provision an isolated peer instance
```

**There is no test suite yet** — `pytest` is declared in the dev extra but no `tests/` directory
exists. When adding tests, follow the collaborator-injection seams described below (`Orchestrator`,
`LLMRouter`, and every specialist take their dependencies as constructor kwargs) and use a temp
SQLite file for `MemoryStore`. `get_settings()` is `lru_cache`d — call `get_settings.cache_clear()`
between tests that change the environment. Shell scripts must pass `shellcheck`.

## Architecture

Layering rule (enforce it in reviews): `integrations` and `delivery` know nothing about
`specialists`; `specialists` depend on `core`; `core` depends on nothing above it. `core/orchestrator.py`
is the only place specialists are wired together.

- **`core/llm_router.py` — the privacy gate.** The single choke point for every LLM call, and the
  most important invariant in the repo. A request is local-only if `context_metadata["local_only"]`
  is true or `source ∈ PRIVATE_SOURCES` (`obsidian_private`, `personal_calendar`, `telegram_private`).
  On that path it **fails closed**: any Ollama failure raises `PrivacyError` and must never reach
  `ask_anthropic`. Non-private requests try Ollama first and escalate to Anthropic only on
  low-confidence output (too short / refusal marker / over latency budget). Cloud calls may
  originate from exactly one method (`ask_anthropic`) — preserve that when refactoring.
  The router carries **two parallel APIs**: async (`route_async`, `ask_local`, `ask_anthropic`) is
  the current one; sync `route`/`route_detailed`/`decide`/`complete` are legacy shims kept for
  specialists scaffolded in Phase 1. Prefer async for new code.
- **`core/memory.py`** — SQLAlchemy 2.0 ORM over SQLite (`data/loop.db`). Store methods `expunge()`
  rows before returning, so callers get detached copies readable after the session closes; keep that
  pattern when adding methods. `ConversationMemory` is the async, session-scoped conversation store
  in the same database and summarises sessions with the **local-only** model.
- **`core/vector_store.py`** — persistent ChromaDB with two collections (`knowledge`, `emails`).
  Embeddings are always local (`nomic-embed-text` via Ollama), so **Ollama must be running for
  indexing and search**, not just chat. Notes from the private vault must carry metadata marking
  them local-only so downstream Q&A forces local inference.
- **`core/orchestrator.py`** — `ask()` is the RAG pipeline behind `loop ask`: semantic search →
  privacy escalation (any private hit forces local-only unless the caller overrode it) →
  conversation history → grounded prompt → `route_async` → persist both turns.
- **`core/scheduler.py`** — APScheduler `BackgroundScheduler` bound to `settings.timezone`. Jobs are
  sync, so async specialist coroutines are driven with `asyncio.run` inside the job function.
- **Specialists** are pure logic over injected collaborators (email triage scoring, calendar conflict
  detection, task capture/summaries, Obsidian Q&A). Integrations/delivery are thin async clients;
  a missing credential or one failing channel must degrade gracefully, never crash the process.

Auditability: every LLM call is logged to `llm_usage_log` with backend, latency, `local_only` and a
**SHA-256 hash** of the prompt — prompt content is never persisted. Do not add content logging.

Heavy/optional SDKs (`anthropic`, `ollama`, chromadb clients) are imported lazily inside the methods
that use them so unconfigured deployments still start. Keep new integrations doing the same.

## Conventions

- `from __future__ import annotations` at the top of every module; dataclasses for value objects.
- Unfinished work is marked `# TODO(phaseN):` — a large part of `integrations/` and `delivery/` is
  still Phase 1/2 scaffolding. Check for these markers before assuming a connector works.
- All configuration flows through the single `Settings` model in `config/settings.py` (env var names
  = field names, case-insensitive, no prefix). Every field needs a safe default; nothing hardcoded,
  no secrets in code. Adding a setting means updating `config/.env.example` and the §6 table in
  `docs/SPECIFICATION.md`.
- Store UTC timestamps in SQLite; all scheduling honours `settings.timezone`.
- Web dashboard is FastAPI + Jinja2 + HTMX + Tailwind CDN — no JS framework. Write endpoints act
  then redirect (303) back to the listing so it works without JavaScript; search returns the
  `search_results.html` fragment when the `HX-Request` header is present.
- Loop never auto-sends email — sending always requires explicit user approval.
- Work lands on `phase-N` branches merged to `main` via PR; commit subjects read
  `Phase N: <what changed>`.
