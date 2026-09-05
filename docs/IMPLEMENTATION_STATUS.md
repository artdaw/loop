# Implementation status — resumable checkpoint

**Read this file, `IMPLEMENTATION_PLAN.md`, and `ACCEPTANCE_MATRIX.md` to resume without the
original conversation.** Verify each claim against the worktree before trusting it.

**Branch:** `vnext-implementation` · **Last updated:** 2026-09-05 · **Stage:** A (A1–A5 done, A6 next)

---

## Honest summary

Planning artefacts exist, the baseline is recorded, and **milestone A1 (foundations) is complete
and verified**, and **A2 (schema, versioned migrations, legacy upgrade)** is complete and
verified, and **A3 (intake), A4 (task service) and A5 (triggers/DST/catch-up)** are complete and
verified. **11 of 184 acceptance scenarios are now verified** with named tests: T02, T03, T11–T15,
D07–D10. The rest remain pending. The archived Phase 4 system remains green and is the
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
| A2 schema + migrations | **done** | `pytest tests/vnext/test_schema_and_migration.py` 25 passed; full suite 372 |
| A3 event intake | **done** | `tests/vnext/test_intake.py` 23 passed — T02, T15 |
| A4 task service | **done** | `tests/vnext/test_tasks.py` 28 passed — T03, T11–T14 |
| A5 triggers + DST + catch-up | **done** | `tests/vnext/test_triggers.py` 24 passed — D07–D10 |
| A6 job queue, leases, fencing | **next** | — |
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

# current, after A5
pytest              449 passed (288 legacy + 161 vnext), 1 warning
ruff check .        All checks passed!
mypy .              Success: no issues found in 87 source files
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
| 5 | Colliding legacy tables are renamed to `legacy_*`, not dropped | `create_all` skips existing names; renaming frees the name while keeping the original data recoverable |
| 6 | Follow-ups are counted and warned about, not silently copied | vNext models them as tasks with `waiting_on`, which needs the A4 task service; a partial copy now would create rows the service cannot yet manage |

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

## A2 delivered

| File | Contract | Tests |
|---|---|---|
| `loop/db/models.py` | runtime §4 Stage A tables, indexes, constraints | `tests/vnext/test_schema_and_migration.py` |
| `loop/db/migrations.py` | ordered revisions, transactional, backup, dry-run | same (25 total) |
| `loop/db/session.py` | `foreign_keys=ON`, WAL, `synchronous=FULL`, busy timeout | same |
| `loop/db/legacy.py` | interfaces §9 Phase 4 → vNext data migration | same |

**Bug found by testing against a populated snapshot** (which is why acceptance §1 demands it):
`create_all` only creates *missing* tables, so on a real Phase 4 database the vNext `tasks` table
was silently skipped and migrated rows were written against the legacy columns. Revision
`0000_rename_legacy` now moves colliding legacy tables to `legacy_*` before the schema is created.
The old data is renamed, never dropped, so a failed upgrade loses nothing.

Enforced and asserted: replayed events cannot be stored twice (the DB guarantee behind T02);
duplicate job dedupe keys are rejected; foreign keys are genuinely enforced; legacy `due_date`
stays a date and never becomes a midnight `due_at`; migrated rows default to local-only privacy;
`autonomy.email_send=act` is retained at the more restrictive `approve`; **no triggers are created
from legacy rows**, so no old reminder reactivates.

## A3–A5 delivered

| File | Contract | Scenarios verified |
|---|---|---|
| `loop/runtime/intake.py` | runtime §2/§5, interfaces §2 | **T02** replay, **T15** key-reuse conflict |
| `loop/services/tasks.py` | runtime §6 lifecycle | **T03**, **T11**, **T12**, **T13**, **T14** |
| `loop/runtime/triggers.py` | runtime §6–§7 time | **D07**, **D08**, **D09**, **D10** |

**Two real bugs found by the DST tests.** First, a spring *gap* and an autumn *fold* both make
`fold=0`/`fold=1` report different UTC offsets, so the offset comparison alone cannot tell them
apart — the round-tripped wall clock can, and must be checked first. Second, `claim_occurrence`
swallowed every `IntegrityError` as "already claimed", which would have silently hidden a firing
whose foreign key was wrong; it now confirms the row actually exists before reporting a duplicate.

## Exact next step

Implement **A6 — durable job queue with leases and fencing** (runtime §7):

1. `loop/runtime/jobs.py`: claim due work in a short `BEGIN IMMEDIATE` transaction using a
   compare-and-set lease (default 60s, renew every 20s); `fencing_token` increments per claim.
2. **D02**: two workers claim the same due job; only the current lease/fencing token may commit.
3. **D03**: a worker that loses its lease during a model call cannot commit or start a new effect
   when it returns.
4. **D11**: cancellation is checked before each effect.
5. Retry policy: ≤5 attempts, 5s/30s/120s/600s with ≤20% jitter; schema, permission, unsupported
   and auth failures do not retry (auth pauses that connector).

Then **A7** (notifications + outbox, D04–D06, T08–T10), **A8** (loop run, D15–D16), **A9**
(end-to-end: task → restart → reminder → snooze/done).

Relevant spec sections: runtime §7 (leases, retries, triggers), §8 (delivery truth);
acceptance §3 (D-series). (runtime §4, interfaces §9, acceptance §1):


