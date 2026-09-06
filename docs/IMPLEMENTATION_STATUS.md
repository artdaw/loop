# Implementation status — resumable checkpoint

**Read this file, `IMPLEMENTATION_PLAN.md`, and `ACCEPTANCE_MATRIX.md` to resume without the
original conversation.** Verify each claim against the worktree before trusting it.

**Branch:** `vnext-implementation` · **Last updated:** 2026-09-06 · **Stage:** A complete; **WP2 (ModelGateway) complete**; WP3 (Stage B vault) next

---

## Honest summary

**Specification update, 2026-09-06:** target is now 1.3 with nine normative
documents and 196 scenarios. The 12 new LG scenarios are pending. Previously
recorded evidence remains unchanged; this documentation update ran no runtime
tests. Read [agent-stack.md](specification/agent-stack.md) before further agent
implementation. Continue A7, then introduce ModelGateway before model-driven
Stage B and the required LangGraph/generic runners during C.

Planning artefacts exist, the baseline is recorded, and **milestone A1 (foundations) is complete
and verified**, and **A2 (schema, versioned migrations, legacy upgrade)** is complete and
verified, and **A3 (intake), A4 (task service) and A5 (triggers/DST/catch-up)** are complete and
verified. **15 of 196 acceptance scenarios are recorded as verified** with named tests: T02, T03, T11–T15,
D02, D03, D05, D07–D11. The rest remain pending. The archived Phase 4 system remains green and is the
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
| A6 job queue, leases, fencing | **done** | `tests/vnext/test_jobs.py` 37 passed — D02, D03, D05, D11 |
| A7 notifications + outbox | **done** | `tests/vnext/test_outbox.py` 26 passed — D04, D06, T08 |
| A8 loop run, single leader | **done** | `test_stage_a_e2e.py` — D15, D16, D17 |
| A9 Stage A end-to-end | **done** | `test_stage_a_e2e.py` 23 passed — T01, T09, T10, D01 |
| **Stage A** | **complete** | 535 tests pass; ruff + mypy clean |
| WP2 ModelGateway (LangChain) | **done** | `tests/vnext/test_model_gateway.py` 29 passed — LG03, LG05, LG12 |
| WP3 Stage B: vault gateway → compiler | **next** | — |
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

# current, after WP2 (2026-09-06)
pytest              564 passed (288 legacy + 276 vnext), 1 warning
ruff check .        All checks passed!
mypy .              Success: no issues found in 98 source files
uv sync --locked    reproducible; 176 packages resolved
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

## A6 delivered

`loop/runtime/jobs.py` — leases, fencing tokens, bounded retry, cancellation. Verifies
**D02**, **D03**, **D05**, **D11**, plus the restart halves of D01/D14.

**A race bug found by refusing to trust a green test.** The first concurrency test used ten
threads and passed — *and kept passing with the locking removed*, because thread scheduling rarely
produces the interleaving that matters. Investigating that revealed `claim()` never checked whether
its compare-and-set actually matched a row, so two workers could each be handed the same job.
`apply_claim` now returns `None` on a zero row count, and `claim` is split into
`select_candidate` / `apply_claim` so the interleaving can be reproduced deterministically. The
new test was confirmed to fail when the check is disabled. The thread test is retained but
relabelled: it catches crashes under contention and proves nothing about atomicity.

**Method note for future milestones:** for any test asserting a concurrency, crash or recovery
invariant, disable the mechanism and confirm the test fails. A test that passes either way is
worse than no test, because it reads as evidence.

## A7–A9 delivered — Stage A complete

| File | Contract | Scenarios verified |
|---|---|---|
| `loop/runtime/outbox.py` | runtime §8 delivery truth | **D04**, **D06**, **T08** |
| `loop/runtime/service.py` | runtime §2/§7 sweep + leader lease | **D15**, **D16**, **D17** |
| `tests/vnext/test_stage_a_e2e.py` | the required demo | **T01**, **T09**, **T10**, **D01** |

`queued`/`sent`/`unknown` are kept strictly apart. An uncertain send becomes `unknown` and is
never automatically resent, because a resend risks a duplicate message and a "sent" claim would be
false. Single-leader is a database lease, not a module flag — a web reload re-imports the module
and a second bot alias is a different process, so an in-process guard sees neither.

**Two tests that proved nothing, caught by mutation testing.** The D01 restart tests passed even
with occurrence-key suppression removed, because a fired one-shot trigger is disabled and never
seen again — they proved the disable, not exactly-once. The replacement reproduces the real crash
window (firing committed, disable not) and fails when suppression is removed. That test in turn was
*itself* vacuous at first: the replacement service was not the leader, so no sweep ran; it now
advances past the dead leader's lease and asserts it actually swept.

**Standing method:** every concurrency, crash or recovery test is verified by disabling its
mechanism and confirming the test fails. Mutations applied so far: job compare-and-set, outbox
no-resend, outbox preflight, leader check, occurrence suppression.

## WP2 delivered — ModelGateway

| File | Contract | Scenarios verified |
|---|---|---|
| `loop/ai/model_gateway.py` | agent-stack §3 policy adapter | **LG03**, **LG12** |
| `loop/ai/budget.py` | agent-stack §3 shared accounting | **LG05** |

Dependencies moved to LangChain 1.4 / LangGraph 1.2 with `langchain-ollama` 1.1,
`langchain-anthropic` 1.7 and `langgraph-checkpoint-sqlite` 3.1. The direct `ollama` and
`anthropic` SDKs are **retained deliberately** — legacy `core/llm_router.py` still imports them
lazily, so removing them would break a retained module. There is a comment in `pyproject.toml`
saying so, because they otherwise look unused.

Tests use LangChain's own `GenericFakeChatModel`, so the path under test is the real
`BaseChatModel.invoke` path rather than a stand-in that could drift from framework behaviour.

**Removed the legacy escalation heuristic.** The old router escalated to cloud when a local reply
looked "weak" by length, latency or refusal phrasing. Specification 1.3 forbids this: those are
not confidence measurements, and acting on them silently sends private-adjacent work to a third
party. Cloud is now reached only when the local model is genuinely *unavailable* and the label
permits it.

**Provider API mismatch caught by mypy.** `ChatAnthropic` in the locked 1.7.1 declares `model`
with alias `model_name` and an `api_key` of type `SecretStr`; two aliased optional fields also read
as required to the type checker. Verified the real signature against the installed package instead
of assuming the documented spelling.

**Mutations verified:** allowing the private path to fall back to cloud fails 5 LG03 tests;
removing the budget lock fails the parallel-children test.

## Exact next step

**Work package 3: complete Stage B** (vault gateway, policy, capture, receipts, compiler,
retrieval) — 32 V scenarios, `docs/specification/vault.md`.

Order within the package, following the plan's B1–B9:
1. **B1** synthetic GlebOS fixtures (minimal + a 500-source/200-page perf vault) per acceptance §1,
   including Unicode NFC/NFD filenames, pipes inside source bodies, and an old v1 template.
2. **B2–B3** read-only onboarding and policy loading with rule conflicts (V01–V09).
3. **B4** exact capture and the six-column ledger (V10–V16).
4. **B5** journaled gateway with crash injection at each boundary (V17–V22) — apply the standing
   mutation discipline here especially.
5. **B6–B8** read receipts, compiler rules 1–9, FTS5 retrieval (V23–V32).

GlebOS at `/Users/gleb/Claude_Cowork/GlebOS` stays **read-only**; all tests use temporary
synthetic vaults. — the shared LangChain
policy adapter, required *before* any model-driven Stage B work.



 (runtime §4, interfaces §9, acceptance §1):

