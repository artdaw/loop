# Implementation status — resumable checkpoint

**Read this file, `IMPLEMENTATION_PLAN.md`, and `ACCEPTANCE_MATRIX.md` to resume without the
original conversation.** Verify each claim against the worktree before trusting it.

**Branch:** `vnext-implementation` · **Last updated:** 2026-09-05 · **Stage:** A (A1–A2 done, A3 next)

---

## Honest summary

Planning artefacts exist, the baseline is recorded, and **milestone A1 (foundations) is complete
and verified**, and **A2 (schema, versioned migrations, legacy upgrade)** is complete and
verified. 0 of 184 acceptance scenarios are verified — A1/A2 build the primitives and storage the
scenarios rest on, and enforce several of their preconditions at the database level, but a
scenario is only marked verified once its own behaviour is asserted end to end. The archived Phase 4 system remains green and is the
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
| A3 event intake | **next** | — |
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

# current, after A2
pytest              372 passed (288 legacy + 84 vnext), 1 warning
ruff check .        All checks passed!
mypy .              Success: no issues found in 80 source files
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

## Exact next step

Implement **A3 — authenticated event intake** (runtime §2, §5; interfaces §2):

1. `loop/runtime/intake.py`: accept → verify owner identity (both chat *and* sender ID for
   Telegram) → build the event envelope → commit **before** acknowledging.
2. Dedupe on `UNIQUE(origin, idempotency_key)`; a replayed update returns the same accepted
   result with one acknowledgement (**T02**).
3. Idempotency key reused with a *different* body returns HTTP 409 with no second effect
   (**T15**).
4. An unbound instance must not accept the first arbitrary `/start` as its owner (interfaces §2).

Then **A4** (task service: T01, T03, T06, T11, T12, T14) against the same events.

Relevant spec sections: runtime §2 (cycle), §5 (event envelope/kinds); interfaces §1–2
(interaction contract, Telegram identity); acceptance §2 (T-series). (runtime §4, interfaces §9, acceptance §1):


