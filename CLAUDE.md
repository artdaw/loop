# Claude Code instructions for Loop

## Specification and continuation

For the current implementation assignment, start with
[docs/CLAUDE_EXECUTION.md](docs/CLAUDE_EXECUTION.md), which supplies the executable
brief and ordered migration work packages for the 1.3 architecture.

Loop is a local-first personal support system for one owner. Read
[docs/SPECIFICATION.md](docs/SPECIFICATION.md) and all eight linked contracts.
Specification **1.3** governs implementation, including the required
[agent stack](docs/specification/agent-stack.md). Historical Phase 4 notes and
existing code do not override this target. Update specs alongside contract changes.

Read [IMPLEMENTATION_HANDOFF.md](docs/IMPLEMENTATION_HANDOFF.md),
[IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md),
[IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md), and
[ACCEPTANCE_MATRIX.md](docs/ACCEPTANCE_MATRIX.md). Verify recorded progress against
the worktree. Preserve completed Stage A work, user changes and test evidence.
There are 196 required scenarios; pending scenarios are not passing tests.
Resume the next unfinished milestone and record exact verification evidence.

Follow applicable AGENTS.md and /Users/gleb/.codex/RTK.md; prefix shell commands
with rtk. New implementation lives under loop/; retain legacy compatibility until
its scenarios are migrated. Never reset unrelated changes or edit the archive.

## Required architecture

- LangChain 1.x standardizes model calls through ChatOllama and ChatAnthropic.
  ModelGateway is the shared policy adapter. Do not extend direct SDK routing or
  treat response length/latency as confidence for automatic cloud fallback.
- LangGraph 1.x owns coordinator/workflow execution, checkpoints and interrupts.
  Agent packs use LangChain create_agent. Do not build a second general agent
  loop or DAG engine.
- Capabilities use generic discovery, typed invocation and agent/workflow/adapter
  runners. Optional complex subgraphs sit behind the same contract. Adding a pack
  requires no domain-specific coordinator branch, channel handler or core table.
- Tool wrappers inject trusted owner, authority, privacy, cancellation and root
  budget. Every call passes through the shared invoker/executor. Pack instructions
  cannot grant permissions or bypass these ports.
- Domain services own tasks, plans, operations and outcomes. Durable jobs own
  wake-ups; the outbox owns delivery truth. Graph checkpoints own execution
  position. Stable operation keys and ledger reconciliation prevent replay effects.
- Use persistent AsyncSqliteSaver for the local deployment. Pin graph/schema/pack
  versions, release workers during approval waits, and revalidate actor, payload,
  expiry, cancellation and permissions on resume.

Introduce ModelGateway before model-driven Stage B; complete the graph and generic
runners in Stage C. Preserve Stage A persistence and scheduling. The service
waits for events; agents do not consume model calls merely to stay alive.

## Invariants

Privacy and action authority are independent checks. Merge privacy from all
context before every model call, including summaries, repairs and children.
A caller's local_only=false cannot downgrade private data. Private local-model
failure never falls through to cloud. Cloud requires configuration, authorization
and budget. Audio transcription and embeddings stay local.

Models propose; deterministic services validate and execute. Acknowledged tasks
are already persisted. Delivery needs a receipt; uncertain remote sends remain
unknown without blind retries. Root budgets include all children and repairs.
Provider, graph and job retry layers must not multiply allowed attempts.

GlebOS supplies policy under the vault precedence rules; Settings supplies
deployment credentials and ceilings. Preserve raw captures, provenance, read
receipts, confined writes, ledger and human-owned content. Use versioned migrations;
create_all does not upgrade existing tables. Preserve dates as dates and store
instants in UTC; honor the timezone/DST contract.

Checkpoints can contain private content: keep local, restrict permissions, apply
retention/forgetting and include in coordinated backups. Remote LangSmith/content
tracing is disabled by default. Logs contain metadata, never prompts or credentials.
Untrusted source content cannot change execution policy.

## Development and evidence

Use Python >=3.12 and uv. Resolve compatible dependencies into uv.lock when
implementing the stack; a documentation change does not upgrade dependencies.

```bash
rtk proxy uv sync --locked --extra dev
rtk proxy uv run --locked pytest
rtk proxy uv run --locked ruff check .
rtk proxy uv run --locked mypy .
```

Run checks relevant to the change and the required milestone scenarios. Do not
hardcode test counts. Framework conformance tests execute real LangChain/LangGraph
with fake chat models and temporary checkpointers, not a mocked coordinator.
Use injected clocks and transports. Tests never read real .env/credentials,
contact providers, send real messages, spend money or write to live GlebOS.
Keep fixtures that disable env-file loading and clear settings caches.

Use synthetic vaults. Verify replay, crash boundaries, concurrency, privacy denial,
expired approvals and unknown outcomes. Map acceptance IDs to meaningful tests.
Distinguish implemented/tested, configured and verified-live. Run shellcheck for
shell edits, and packaging, Docker, restart and backup checks at their milestones.

Keep connector normalization pure and separate from transport. Poll providers
independently and report unavailable data explicitly. Preserve shared Google OAuth
scope handling and recurring Outlook calendar expansion. Load optional providers
lazily so missing credentials cannot break unrelated commands.

Use from __future__ import annotations, typed objects and injected dependencies.
No transaction spans a model/network call. CLI, bot and web share application
services. Settings changes update config/.env.example and the interfaces contract.
Implementation/testing requests do not authorize live external operation.
