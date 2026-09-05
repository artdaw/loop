# Implementation status — resumable checkpoint

**Read this file, `IMPLEMENTATION_PLAN.md`, and `ACCEPTANCE_MATRIX.md` to resume without the
original conversation.** Verify each claim against the worktree before trusting it.

**Branch:** `vnext-implementation` · **Last updated:** 2026-09-05 · **Stage:** A (in progress)

---

## Honest summary

Planning artefacts exist and the baseline is recorded. **No vNext runtime code has been written
yet.** 0 of 184 acceptance scenarios are verified. The repository still contains the archived
Phase 4 system, which remains fully green and is the migration source, not the target.

Do not read the Phase 4 README/spec completion claims as evidence for this target.

## Milestone ledger

| Milestone | State | Evidence |
|---|---|---|
| Baseline recorded | done | `pytest` 288 passed; `ruff`/`mypy` clean; no `uv.lock` |
| Branch created preserving WIP | done | `git branch --show-current` → `vnext-implementation`; user WIP still unstaged |
| Specs read (8 documents) | done | main, vault, runtime, interfaces, capabilities, weather, travel, acceptance |
| 184 scenarios enumerated | done | `docs/ACCEPTANCE_MATRIX.md`, generated from `acceptance.md`, 184 rows |
| `IMPLEMENTATION_PLAN.md` | done | Stages A–E with milestones, files, dependency order |
| `uv.lock` generated | **next** | interfaces §8 requires it before `--locked` workflows |
| A1 foundations | pending | — |
| A2 schema + migrations | pending | — |
| A3–A9 | pending | — |
| Stages B–E | pending | — |

## Baseline checks (pre-existing, not regressions)

```
pytest              288 passed, 1 warning
ruff check .        All checks passed!
mypy .              Success: no issues found in 61 source files
uv.lock             ABSENT
python              3.12.11
```

Any future failure not in this list is a regression introduced by this work.

## Worktree state

Pre-existing uncommitted user work — **preserved, do not revert**:

```
 M CLAUDE.md            M Dockerfile           M README.md
 M delivery/telegram.py M integrations/telegram_bot.py
 M pyproject.toml       M scripts/setup.sh     M docs/SPECIFICATION.md
?? LICENSE            ?? docs/archive/       ?? docs/specification/
```

`integrations/telegram_bot.py` and `delivery/telegram.py` were being actively edited by the user
in a prior session. Leave them alone unless a scenario requires the change.

Added by this work: `docs/IMPLEMENTATION_PLAN.md`, `docs/IMPLEMENTATION_STATUS.md`,
`docs/ACCEPTANCE_MATRIX.md`.

## Consequential decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | New code lands in a `loop/` package rather than editing legacy modules in place | Legacy modules stay importable and green during migration; removal happens per-scenario, not wholesale |
| 2 | Legacy `due_date` values stay dates | interfaces §9 forbids coercing them into midnight instants |
| 3 | Existing 288 tests must keep passing | They are the regression baseline; migrate a test only when the new code verifies its scenario |
| 4 | Matrix generated from `acceptance.md`, not hand-transcribed | Guarantees exactly the 184 specified IDs with no invention or omission |

## Blockers

None. No live credentials are required for Stage A; all adapters are injected/faked.

## Exact next step

1. Generate and validate `uv.lock` (`uv lock`, then `uv sync --locked --extra dev`).
2. Implement **A1 foundations** — `loop/core/{settings,clock,ids,privacy,errors}.py`:
   - `Clock` protocol with `FrozenClock` for tests (runtime §3, acceptance §1).
   - `PrivacyLabel` with the merge algebra: `local_only` OR, `sensitive` OR, origins ∪,
     destinations ∩; unlabelled import defaults to `local_only`, no destinations (runtime §3).
   - Structured error taxonomy from runtime §10 (`invalid_input` … `internal_error`).
3. Then **A2**: the 30-table schema and versioned migrations (runtime §4), tested against a
   populated snapshot of the legacy schema (acceptance §1, interfaces §9).

Relevant spec sections for the next step: runtime §3 (types/storage), §4 (schema), §10 (failures);
interfaces §7 (settings), §9 (migration); acceptance §1 (harness).
