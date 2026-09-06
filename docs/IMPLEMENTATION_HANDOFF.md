# Implementation handoff for Claude Code or another coding agent

This is an implementation brief for **Loop vNext specification 1.3**, not a claim
that the target has been built. Use the existing local repository so the current,
possibly uncommitted specifications and user changes are available.

In Claude Code, open this repository and ask:
“Read @docs/IMPLEMENTATION_HANDOFF.md and execute the implementation brief.”

For the concrete 1.3 migration sequence and complete starting prompt, use
[CLAUDE_EXECUTION.md](CLAUDE_EXECUTION.md). Its work packages refine the plan below.

For a planning-only review, explicitly ask for that instead; leave Plan Mode before
expecting code changes. Claude Code's documented workflow supports repository
exploration, planning, implementation and verification.
[Claude Code best practices](https://code.claude.com/docs/en/best-practices).

## Current resumable checkpoint — 2026-09-06

WP5 is complete. The repository records **87 of 196 acceptance scenarios verified**;
the last locked run passed **879 tests** with Ruff and mypy clean. Completed work
includes Stage A, ModelGateway/shared budget policy, all V scenarios in Stage B,
typed plans/authority/operation ledger, capability registry validation, and the
generic coordinator plus agent/workflow/adapter runners. LG01 and LG02 have real
framework evidence in `tests/vnext/test_capability_runners.py`.

Resume at **WP6 checkpoint durability**. Do not rebuild the coordinator or agent
runner. Extend the existing seams with a versioned run-mapping migration, local
`AsyncSqliteSaver`, one checkpoint thread per root run, durable resume jobs, and
approval interrupts tied to the existing operation ledger. The exact order and
test obligations are in `CLAUDE_EXECUTION.md` and `IMPLEMENTATION_STATUS.md`.

## Implementation brief

Implement the complete Loop vNext system described by docs/SPECIFICATION.md and
all eight contracts in its reading-order table. The objective includes stages A–E,
weather.local, travel.itinerary, and the shared capability extension mechanism.
Work on the existing implementation, retaining useful components and migrating
persistent data safely. Continue beyond planning into implementation and verification.

### Establish the baseline

1. Read repository instructions, including CLAUDE.md and any applicable AGENTS.md.
   Read the nine normative specification documents and the capability template.
   New target contracts supersede conflicting historical phase notes; archived
   specification/old README completion claims are not proof of implementation.
2. Inspect git status and existing code/tests. Preserve all pre-existing changes.
   Use an implementation branch without losing uncommitted specifications or work.
   Do not reset, delete or silently replace unrelated work.
3. Run the available baseline checks using the project environment; record existing
   failures separately from regressions. Follow the repository's RTK command rule.
   Use uv and Python >=3.12. A missing lockfile must be generated and validated
   before commands requiring --locked can succeed.
4. Update the existing docs/IMPLEMENTATION_PLAN.md with concrete milestones/files and dependency
   order, docs/IMPLEMENTATION_STATUS.md for resumable progress, and
   docs/ACCEPTANCE_MATRIX.md mapping every scenario ID to implementation and tests.
   There are currently 196 specified scenarios; enumerate them from acceptance.md.
   They are requirements to implement and test, not already-passing tests.
   Preserve existing plan/status/matrix files and verified evidence when resuming.

### Implement in dependency order

- **A: durable commitments.** DB schemas/migrations, authenticated intake, shared
  task services, persisted triggers/jobs, leases, idempotency, delivery outbox,
  lifecycle and loop run. Demonstrate task → restart → reminder → snooze/done.
- **B: GlebOS fidelity.** Synthetic vault fixtures, read-only onboarding, rule
  loading/conflicts, exact capture/ledger, safe gateway and crash recovery,
  provenance/read receipts, Compiler rules and lexical retrieval. Demonstrate
  capture → compile → cited retrieval.
- **C: coordination and extensions.** Typed plans and work assignments, privacy/
  authority propagation, budgets, cancellation, dependency validation, registry,
  generic invocation, capability scaffold/validate/test/enable and version pinning.
  Demonstrate two unrelated added packs without changing core/channel handlers.
- **D: proactive support.** Independent connector health, routines, expiring context,
  notification policy, calendar/mail and multiple-source local-preferred weather.
  Demonstrate disagreement, correlated sources, fallback, stale data and warnings.
- **E: full operation.** Preferences/hypotheses/feedback, research/save/output,
  travel comparison/revision/selection/monitoring, remaining required interfaces,
  optional-provider degradation, migration, locked packaging, backup/restore and
  end-to-end operation.

Complete and verify each stage's working behavior before broadening dependent work.
Keep progressing through the remaining stages; completing A alone does not complete
this objective. Useful preparation for later stages may happen earlier, but do
not replace end-to-end implementation with empty classes or mocked success responses.

### Execution and verification rules

- Follow [agent-stack.md](specification/agent-stack.md): LangChain chat adapters
  behind ModelGateway, create_agent for agent packs, LangGraph coordinator/workflow
  runners, persistent checkpointer and bound interrupt/resume. No custom agent
  loop, domain-specific coordinator branches or direct provider SDK bypass.
- ModelGateway and the WP5 framework path are complete. Remaining Stage C work must
  cover LG06–LG10 through real framework execution with fake models; keep Stage A's
  deterministic persistence and scheduling. Graph checkpoints do not replace
  the operation ledger, approvals, task state or delivery receipts.

- Models interpret and propose; deterministic runtime code owns effects, state,
  timing, privacy and evidence checks. All entry points share those services.
- Treat GlebOS as read-only during development. Use synthetic temporary vaults and
  databases. No live vault writes, purchases, bookings, publishing or messages to
  real recipients as automatic testing. Prepare real integration setup separately.
- Use injected clocks, models and transports. Default tests use no real .env,
  credentials, network or paid model. Optional real smoke tests are distinct,
  explicitly configured and do not turn fake-based tests into live verification.
- Implement meaningful failure/replay/restart tests for the contracts. Each
  acceptance-matrix row names its test and result; labels are pending, implemented,
  verified, blocked or optional-unconfigured. A count of green tests alone is not
  acceptance coverage.
- Run relevant pytest, ruff and mypy checks, plus package installation outside the
  source tree, Docker build, process-restart and backup/restore checks as required.
  Run shellcheck when changing shell scripts. Fix introduced failures.
- Verify actual results: a scheduled trigger exists, a saved capture has file and
  ledger, an executed operation has evidence, and a sent message has a receipt.
  Test missing/uncertain results as well as successful outcomes.
- Handle ordinary implementation choices independently and record consequential
  decisions. Resolve specification contradictions before dependent implementation;
  record the minimal clarification and rationale. Do not silently reduce scope
  or weaken an invariant to make tests pass.
- Ask only for genuinely blocking information/authority and continue independent
  work while waiting. Missing live credentials must not block implementing and
  testing the rest of the adapter/runtime.
- Keep changes reviewable and preserve unrelated work. A future publish/deploy/live
  activation remains a separate action; the implementation brief authorizes building
  and verifying the system, not automatic external operation.

### Checkpoint and completion requirements

After each milestone, update IMPLEMENTATION_STATUS.md with:
- Completed behavior and acceptance IDs, with exact test/command evidence.
- Current branch/worktree state and relevant modified files.
- Remaining work, known failures, blockers and consequential decisions.
- The exact next step and relevant specification sections.

If a session ends or context resets, read these files and resume from evidence.
Do not restart the architecture or mark unfinished items complete to end a session.
A later session should be able to continue without the original conversation.

Final delivery must include working setup/run instructions, migrations, uv.lock,
CLI/API/bot interfaces, tested capability template, configured/unconfigured feature
matrix, acceptance coverage and reproducible workflow transcripts. Separate
implemented/tested, configured, and verified-live status. Report any outstanding
mandatory scenario as incomplete; optional missing providers must degrade as specified.

## Resume prompt

“Read CLAUDE.md, docs/CLAUDE_EXECUTION.md, docs/IMPLEMENTATION_HANDOFF.md,
docs/IMPLEMENTATION_PLAN.md, docs/IMPLEMENTATION_STATUS.md and
docs/ACCEPTANCE_MATRIX.md. Preserve the uncommitted work. Verify the focused WP5
tests and the recorded 87/196 total, then implement WP6 checkpoint durability from
the existing coordinator and runners. Start with the run-mapping migration and
restricted local AsyncSqliteSaver factory; continue through durable resume jobs,
bound approval interrupts, replay/cancellation/version checks, and LG06–LG10 tests.
Update the matrix and status after each verified slice.”
