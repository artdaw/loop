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

Preserve existing user changes and verified Stage A work. Verify the recorded
checkpoint before resuming; do not restart completed milestones. Use LangChain
and LangGraph as required, with generic capability runners and the shared policy
gateway. Follow the ordered migration work packages in CLAUDE_EXECUTION.md.

Continue through the remaining A–E milestones, including weather, travel and
GlebOS knowledge workflows. Use synthetic vaults, fake models and transports.
Keep IMPLEMENTATION_PLAN.md, IMPLEMENTATION_STATUS.md and ACCEPTANCE_MATRIX.md
accurate after each milestone. A dependency import, scaffold or mocked graph is
not proof of implementation. Do not mark the target complete while mandatory
acceptance scenarios remain unverified. Do not activate live external actions.

Start by inspecting the worktree, reproducing relevant baseline checks and
implementing the next unfinished dependency. Make ordinary implementation
decisions independently; record consequential decisions and continue useful
work when an optional provider is unconfigured.
```

## Starting checkpoint

The recorded checkpoint is A1–A6 complete, A7 next, with 15 of 196 acceptance
scenarios recorded as verified. These are historical claims to check, not results
rerun while preparing this brief. The worktree includes uncommitted documentation
and unrelated implementation changes: preserve both. Use the existing implementation
branch if appropriate; creating a fresh branch must not discard the working tree.

Inspect the current pyproject.toml and uv.lock before dependency changes. The
manifest still declares legacy LangChain/LangGraph ranges and direct provider SDKs.
Do not remove a dependency while a retained legacy module imports it. Upgrade
through compatible provider packages and test legacy compatibility explicitly.

## Ordered migration work packages

These packages refine the existing plan, rather than replacing its A–E milestones.

| Order | Work package and likely files | Completion evidence |
|---|---|---|
| 1 | Finish A7–A9: loop/runtime notifications, outbox, service and end-to-end task flow | Task survives restart; done/snooze cancel stale delivery; unknown send remains unknown; required A scenarios pass |
| 2 | Before model-driven B: loop/ai/model_gateway.py, provider construction, middleware, pyproject.toml and uv.lock | Actual LangChain fake-model path; LG03 privacy denial and LG05 shared budget tests; install works with optional cloud unconfigured |
| 3 | Complete B: vault gateway, policy, capture, receipts, compiler and retrieval | Synthetic GlebOS capture → compile → cited answer; model calls use gateway; V scenarios pass |
| 4 | C foundation: typed plans, operation ledger/approvals, registry snapshots and tool wrappers | C1–C5 contracts; forged tool arguments cannot override authority; input/output and dependencies validated |
| 5 | C execution: loop/agents/coordinator.py and generic agent/workflow/adapter runners | LG01, LG02, LG04 and EX scenarios through real create_agent/StateGraph; two unrelated packs added without core edits |
| 6 | C durability: run mapping migration, AsyncSqliteSaver, resume dispatch and lifecycle | LG05–LG10 and LG12; restart, replay, parallel isolation, cancellation and bound approval resume demonstrated |
| 7 | D/E: weather, travel, learning, remaining interfaces and operations | Domain acceptance scenarios; LG11 paired backup/restore and retention; full reconstruction workflow |

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
3. Interrupt for approval, terminate the process, restart, and resume the same run.
   Assert that wrong actors, changed payloads, expired approvals and cancellation
   cannot execute, and that accepted execution uses the original operation key.
4. Inject crashes around domain/checkpoint commits and remote sends. Assert no
   duplicate committed effect and no invented success for uncertain delivery.
5. Fail the local model with private input, including derived summaries and child
   output. Assert zero cloud calls and no content in remote tracing or audit logs.
6. Exercise concurrent children and repair attempts against one root budget;
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

To resume in a later Claude session, reuse the copy-paste task above. The updated
status and matrix determine the next step; do not rely on conversation memory.
