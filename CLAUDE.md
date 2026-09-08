# Claude Code — Loop

The single active implementation document is
[docs/CLAUDE_IMPLEMENTATION.md](docs/CLAUDE_IMPLEMENTATION.md).

Read and execute it. It contains the approved architecture, current implementation
assessment, remaining milestones, complete contracts and continuation ledger.
Update progress there; do not recreate the archived plans or handoffs.

Release instruction updated 2026-09-09: **M7 is reopened**. Start at the
current continuation checkpoint and execute its mandatory **R0–R6** sequence.
Historical “M0–M7 done” claims do not authorize declaring release completion.
Missing live provider credentials do not block adapter/worker implementation
with fake transports. Required Docker checks cannot be skipped for release.
Run Docker at the very end, after all other implementation and verification;
follow R6's instructions to make it the release runner's final gate.

Preserve the user's staged and uncommitted changes. Follow applicable AGENTS.md
and /Users/gleb/.codex/RTK.md; prefix shell commands with rtk in this workspace.
Do not treat archived completion claims or the supporting acceptance matrix as
proof of end-to-end readiness. Runtime implementation is a separate task from
the documentation consolidation that prepared this file.
