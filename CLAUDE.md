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
pip install -e ".[dev]"          # the `loop` CLI + pytest/ruff/mypy
pip install -e ".[voice]"        # optional: faster-whisper for voice-to-note
loop --help                      # or: python -m cli.main --help
uvicorn web.main:app --reload --port 8000

pytest                           # 288 tests; hermetic, no network/model/keys/.env
pytest tests/test_autonomy.py -v # a single file
pytest -k "ceiling"              # a single test by name
ruff check .                     # rule set pinned in [tool.ruff.lint]
mypy .                           # ignore_missing_imports = true

# Docker stack (ollama + chromadb + app)
docker compose up -d
docker compose run --rm app loop briefing
docker compose logs -f app
./scripts/setup.sh               # one-command per-user setup (idempotent)
./scripts/new_user.sh <name>     # provision an isolated peer instance
```

The suite is **hermetic by design**: no test needs Ollama, a network, a vault, or a Wrike key —
and an autouse fixture in `conftest.py` blanks `Settings.model_config["env_file"]` so a developer's
real `.env` can never change what the suite asserts. Keep that fixture; without it the tests pass on
a fresh checkout and fail once someone runs `setup.sh`.
Keep it that way — use the collaborator-injection seams (`Orchestrator`, `LLMRouter`, `WrikeSync`,
`AutonomyGate`, and every specialist take their dependencies as constructor kwargs), the
`memory_store`/`settings`/`web_client` fixtures in `tests/conftest.py`, and `httpx.MockTransport`
for HTTP. `get_settings()` is `lru_cache`d — the `settings` fixture calls `get_settings.cache_clear()`
for you. Shell scripts must pass `shellcheck`.

## Architecture

Layering rule (enforce it in reviews): `integrations` and `delivery` know nothing about
`specialists`; `specialists` depend on `core`; `core` depends on nothing above it. `core/orchestrator.py`
is the only place specialists are wired together.

- **Two gates, deliberately orthogonal.** `LLMRouter` asks *which model may see this data* and
  fails **closed** (`PrivacyError`). `AutonomyGate` (`core/autonomy.py`) asks *may Loop act without
  asking* and fails to **asking** (`ApprovalRequiredError`). Never use one to answer the other's
  question: autonomy must not influence backend choice, and privacy metadata must not decide
  whether an action runs. Levels are an `IntEnum` so ceilings work as `min(level, ceiling)`;
  `EMAIL_SEND` is capped by `max_autonomy_email_send` so full send autonomy needs an `.env` edit,
  not a dashboard click.
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
  pattern when adding methods. **Adding a column requires nothing extra, but never assume
  `create_all` applies it** — `bootstrap()` runs an additive `_migrate()` that `ALTER TABLE`s
  columns missing from existing tables. It is additive only (no drops, renames, or retypes), which
  is what makes it safe on every boot; added columns must therefore be nullable. `ConversationMemory` is the async, session-scoped conversation store
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
- Loop never auto-sends email — the `EMAIL_SEND` ceiling enforces this in code, not just in policy.
- Anything that writes to an external system or the vault goes through `AutonomyGate`; anything that
  only *reads* does not (a Wrike pull runs at any autonomy level).
- Audio is unconditionally `local_only` — there is no cloud transcriber behind the `Transcriber`
  protocol and the design forbids adding one.
- Project matching is deterministic and LLM-free on purpose; it runs on every inbound item.
- **Connectors: parse separately from transport.** Every provider module keeps normalisation in
  module-level pure functions (`_to_message`, `_to_event`) and injects the transport
  (`service=` for Google, `graph=`/`transport=` for Microsoft), so the fiddly parts — MIME headers,
  RFC-2822 dates, all-day events, Graph's nested recipients — are tested with no SDK or network.
- **Gmail and Google Calendar share one OAuth token** with a unioned scope set (`google_auth.py`).
  Per-connector scopes would mean whichever ran second invalidated the first's access.
- **Outlook is delegated device-code auth** (`/me` endpoints, `msal`), not app-only, and needs only
  a client id — no secret. Calendar reads go through `/me/calendarView`, which expands recurring
  series; `/me/events` would return the master once and miss every occurrence.
- **Poll providers independently.** `CalendarSpecialist.get_all_events_today` and
  `EmailSpecialist.scan_inboxes` catch per connector and record `last_errors`; one broken provider
  must never discard another's results.
- `loop status`/`briefing` live in `core/health.py` and `specialists/briefing.py`, split
  collect-then-render like `review.py`. Neither may raise: a missing connector or unreachable
  service is a *state to report*. `_calendar()` in the CLI must pass real connectors — a
  `CalendarSpecialist` built without them returns `[]`, which would render as "nothing in the diary"
  and be indistinguishable from a free day.
- Work lands on `phase-N` branches merged to `main` via PR; commit subjects read
  `Phase N: <what changed>`.
