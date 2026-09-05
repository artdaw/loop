# Implementation status — resumable checkpoint

**Read this file, `IMPLEMENTATION_PLAN.md`, and `ACCEPTANCE_MATRIX.md` to resume without the
original conversation.** Verify each claim against the worktree before trusting it.

**Branch:** `vnext-implementation` · **Last updated:** 2026-09-05 · **Stage:** A (A1 done, A2 next)

---

## Honest summary

Planning artefacts exist, the baseline is recorded, and **milestone A1 (foundations) is complete
and verified**. 0 of 184 acceptance scenarios are verified — A1 builds the primitives the
scenarios rest on (clock, privacy algebra, identity, error taxonomy, settings) but does not by
itself satisfy any numbered scenario. The archived Phase 4 system remains green and is the
migration source, not the target.

Do not read the Phase 4 README/spec completion claims as evidence for this target.

## Milestone ledger

| Milestone | State | Evidence |
|---|---|---|
| Baseline recorded | done | `pytest` 288 passed; `ruff`/`mypy` clean; no `uv.lock` |
| Branch created preserving WIP | done | `git branch --show-current` → `vnext-implementation`; user WIP still unstaged |
| Specs read (8 documents) | done | main, vault, runtime, interfaces, capabilities, weather, travel, acceptance |
| 184 scenarios enumerated | done | `docs/ACCEPTANCE_MATRIX.md`, generated from `acceptance.md`, 184 rows |
| `IMPLEMENTATION_PLAN.md` | done | Stages A–E with milestones, files, dependency order |
| `uv.lock` generated | done | `uv lock` → 171 packages; `uv sync --locked --extra dev` succeeds |
| A1 foundations | **done** | `pytest tests/vnext` 59 passed; full suite 347 passed; ruff + mypy clean |
| A2 schema + migrations | **next** | — |
| A3–A9 | pending | — |
| Stages B–E | pending | — |

## Baseline checks (pre-existing, not regressions)

```
# baseline, before any vNext code
pytest              288 passed, 1 warning
ruff check .        All checks passed!
mypy .              Success: no issues found in 61 source files
uv.lock             ABSENT
python              3.12.11

# current, after A1
pytest              347 passed (288 legacy + 59 vnext), 1 warning
ruff check .        All checks passed!
mypy .              Success: no issues found in 75 source files
uv.lock             present, validated with --locked
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

## A1 delivered

| File | Contract | Tests |
|---|---|---|
| `loop/core/privacy.py` | runtime §3 label + merge algebra | `tests/vnext/test_privacy.py` (19) |
| `loop/core/clock.py` | runtime §3 injected time, integer µs, ISO dates | `tests/vnext/test_clock_ids_errors.py` |
| `loop/core/ids.py` | runtime §3 UUID4, SHA-256, stable input hash | same |
| `loop/core/errors.py` | runtime §10 taxonomy + CLI exit codes | same (21 total) |
| `loop/core/settings.py` | interfaces §7 deployment settings | `tests/vnext/test_settings.py` (19) |

Key invariants now enforced in code: merging labels can never widen permissions; a caller-supplied
`local_only: false` cannot downgrade a true one; naive datetimes cannot be persisted as instants;
local dates never become UTC-midnight instants; cloud stays unavailable without flag + key + model
+ positive budget.

## Exact next step

Implement **A2 — schema and migrations** (runtime §4, interfaces §9, acceptance §1):

1. `loop/db/models.py`: the 30 logical tables. Start with the Stage A subset that later
   milestones build on — `events`, `plans`, `work_items`, `artifacts`, `tasks`, `triggers`,
   `trigger_firings`, `jobs`, `notifications`, `outbox`, `operations`, `approvals`, `feedback`,
   `audit` — each with `id`, `version`, `created_at`, `updated_at` and a `privacy` column where it
   carries content. Add the five required indexes.
2. `loop/db/migrations/`: versioned, transactional migrations. `create_all` alone is explicitly
   not migration (runtime §4).
3. `loop/migrate.py` + `tests/vnext/test_migration.py`: upgrade a **populated snapshot of the
   legacy schema**, preserving IDs, completed tasks, follow-ups, preferences and privacy; legacy
   `due_date` stays a date; no old reminder reactivates.

Relevant spec sections: runtime §4 (schema, constraints, indexes), §3 (storage conventions);
interfaces §9 (migration mapping rules); acceptance §1 (populated-snapshot requirement).
