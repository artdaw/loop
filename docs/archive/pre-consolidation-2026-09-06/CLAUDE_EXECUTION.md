# Execute the Loop 1.3 approach

This is the execution entry point for Claude Code. The normative requirements
remain in [SPECIFICATION.md](SPECIFICATION.md) and its eight contracts; this file
turns the agent-stack change into ordered implementation work.

## Copy-paste task

```text
Implement Loop specification 1.3 in this repository. Read CLAUDE.md and
docs/CLAUDE_EXECUTION.md, then follow docs/IMPLEMENTATION_HANDOFF.md and all
normative specification contracts. This is an implementation request: proceed
from inspection through code, migrations, tests and documented verification.

Preserve existing user changes and all verified work through WP5. Verify the
recorded checkpoint before resuming; do not restart completed milestones. Use LangChain
and LangGraph as required, with generic capability runners and the shared policy
gateway. Follow the ordered migration work packages in CLAUDE_EXECUTION.md.

Continue through the remaining A–E milestones, including weather, travel and
GlebOS knowledge workflows. Use synthetic vaults, fake models and transports.
Keep IMPLEMENTATION_PLAN.md, IMPLEMENTATION_STATUS.md and ACCEPTANCE_MATRIX.md
accurate after each milestone. A dependency import, scaffold or mocked graph is
not proof of implementation. Do not mark the target complete while mandatory
acceptance scenarios remain unverified. Do not activate live external actions.

Start by inspecting the worktree, reproducing the focused WP5 checks, then begin
WP6 with the run-mapping migration and checkpointer factory. Make ordinary implementation
decisions independently; record consequential decisions and continue useful
work when an optional provider is unconfigured.
```

## Starting checkpoint

The recorded checkpoint is **WP5 complete**, with **87 of 196 acceptance scenarios
verified**. The exact verified path is in `IMPLEMENTATION_STATUS.md` and
`ACCEPTANCE_MATRIX.md`; check those claims against the worktree rather than rerunning
earlier milestones from scratch. The last full verification was **879 tests passed,
Ruff clean, mypy clean across 127 source files, and locked sync resolving 176
packages**.

WP5 added `loop/agents/coordinator.py`, `loop/capabilities/runners.py`, and the
native-message ModelGateway adapter. LG01 and LG02 run through real LangChain
`create_agent` and LangGraph `StateGraph`, persist validated output/evidence, and
prove the plant-care and bike-service fixtures need no coordinator/channel/schema
edit. Preserve those files and tests as the execution seam for WP6.

The worktree contains uncommitted implementation, documentation and older user
changes. Preserve all of it. Inspect `pyproject.toml` and `uv.lock` before dependency
changes. Direct provider SDKs remain because retained legacy modules import them;
do not remove them until those modules are migrated.

## Ordered migration work packages

These packages refine the existing plan, rather than replacing its A–E milestones.

| Order | Work package and likely files | Completion evidence |
|---|---|---|
| 1–5 | **Complete:** Stage A, ModelGateway, Stage B, C foundation, coordinator and generic runners | 87 scenarios verified; LG01–LG05, LG12 and the recorded A/V/EX coverage stay green |
| 6 | **Next — C durability:** run mapping migration, `AsyncSqliteSaver`, checkpointed coordinator, resume jobs and approval interrupts | LG06–LG10; crash boundaries use the operation ledger; paused runs retain pinned graph/schema/pack versions |
| 7 | C extension lifecycle: typed workflow references, capability objects, offline conformance, init/test/enable/upgrade/rollback, CLI/API/bot invocation | EX06, EX07, EX11–EX14 and remaining A18–A22 with shared authentication/error envelopes |
| 8 | D/E: weather, travel, learning, remaining interfaces and operations | Domain acceptance scenarios; LG11 paired backup/restore/forgetting; full reconstruction workflow |

File names identify intended seams; adapt them to existing modules without creating
parallel implementations. Implement privacy and budget middleware before any new
model-driven workflow. Add migrations for run mappings and graph versions using
the project's versioned migration mechanism. Preserve domain storage ownership.

The coordinator selects operations from registry metadata. Domain-specific graphs
belong inside capability packages. A new provider can require adapter code, but
ordinary agent/workflow packs must be installable through the same scaffold,
validate, offline test and enable workflow with no coordinator edits.

## Required proof for the new architecture

1. Run an agent pack through real create_agent with a fake chat model and typed
   tool wrappers. Assert persisted output and evidence, not just invocation counts.
2. Add an unrelated agent pack and workflow pack using the public template and CLI.
   Record the file diff proving no coordinator/channel/schema changes were needed.
3. **WP6 next:** interrupt for approval, terminate the process, restart, and resume the same run.
   Assert that wrong actors, changed payloads, expired approvals and cancellation
   cannot execute, and that accepted execution uses the original operation key.
4. **WP6 next:** inject crashes around domain/checkpoint commits and remote sends. Assert no
   duplicate committed effect and no invented success for uncertain delivery.
5. Fail the local model with private input, including derived summaries and child
   output. Assert zero cloud calls and no content in remote tracing or audit logs.
6. **WP6 next:** exercise concurrent children and repair attempts against one root budget;
   pin package/graph versions across an upgrade and verify isolated child state.
7. Back up and restore both databases, then exercise forgetting and retention.
   Record LG01–LG12 evidence in the acceptance matrix with real test names.

Use temporary storage, injected clocks and fake transports. Real framework
execution is required; provider network access is not. Consult official package
documentation for the locked release before using framework APIs. Do not silently
weaken a contract to accommodate a library limitation.

## Checkpoints and completion

After each package, record changed files, migrations, acceptance IDs, exact check
commands/results, remaining failures and the next step. Preserve historical
evidence with its date; new claims need new verification. Update the plan when
inspection changes a file choice or dependency order.

Completion requires all mandatory acceptance scenarios verified, reproducible uv
setup, package installation outside the source tree, Docker and restart checks,
paired backup/restore, and working CLI/API/bot paths. Report optional providers
as unconfigured until configured, and live verification separately from fake-based
tests. A session ending is a checkpoint, not completion of unfinished work.

To resume now, implement WP6 in this order: append the domain run-mapping migration;
add a local restricted-permission `AsyncSqliteSaver` factory; compile the existing
coordinator with it using one thread per root run; bridge interrupt/resume through
durable jobs; bind approvals before `interrupt`; then add restart, crash-order,
wrong-actor/payload/expiry, cancellation, version-pinning and child-isolation tests.
Do not start weather/travel or broad interface work until this path is green and
recorded. The updated status and matrix determine the next step after each slice.
