# Implementation status — resumable checkpoint

**Read this file, `IMPLEMENTATION_PLAN.md`, and `ACCEPTANCE_MATRIX.md` to resume without the
original conversation.** Verify each claim against the worktree before trusting it.

**Branch:** `vnext-implementation` · **Last updated:** 2026-09-06 · **Stage:** A, B, agent stack complete; **all stages complete**; **196 of 196 scenarios verified**

---

## Honest summary

Specification 1.3 has nine normative documents and 196 scenarios. **All 196
acceptance scenarios are recorded as verified** with named tests. Stage A, the
ModelGateway, Stage B, the C foundation and WP5's generic execution path are
complete. The archived Phase 4 system remains green and is the migration source,
not the target.

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
| WP3·B1 synthetic vault fixtures | **done** | `tests/vnext/vault_fixtures.py` (minimal + 500-source perf) |
| WP3·B5 gateway: paths, journal, recovery | **done** | `test_vault_gateway.py` 27 passed — V04, V05, V12, V13, V30 |
| WP3·B4 capture + ledger | **done** | `test_capture_ledger.py` 31 passed — V02, V03, V19, V20 |
| WP3·B2/B3 onboarding + policy | **done** | `test_onboarding_policy.py` 37 passed — V01, V26, V27, V28 |
| WP3·B6 read receipts + provenance | **done** | `test_receipts.py` 32 passed — V06, V07, V21, V23, V24, V29 |
| WP3·B7 compiler rules 1–9 | **done** | `test_compiler.py` 56 passed — V08–V11, V14–V18, V22, V32 |
| WP3·B8 lexical retrieval + meetings | **done** | `test_search.py` 20 passed — V25, V31 |
| **Stage B** | **complete** | all 32 V scenarios verified |
| WP4·C1 typed plans + DAG | **done** | `test_planner_authority.py` 48 passed — A01–A04 |
| WP4·C2 authority + privacy propagation | **done** | same file — A06, A08–A13, A23, **LG04** |
| WP4·C3 shared budget through contexts | **done** | same file — A07 |
| WP4·C4 operation ledger + approvals | **done** | `test_operations.py` 25 passed — A14–A17, LG07 (replay) |
| WP4·C5 capability registry | **done** | `test_registry.py` 37 passed — EX01–EX05, EX08–EX10 |
| WP5 coordinator + generic runners | **done** | `test_capability_runners.py` — LG01, LG02 |
| WP6 run durability + backups | **done** | `test_run_durability.py` — LG06, LG08–LG11 |
| WP7·D1/D3 connector health + observations | **done** | `test_connector_health.py`, `test_observations.py` — P03–P05, P08, P10, P11, P19 |
| WP7·D2 routine documents + activation | **done** | `test_routines_notify.py` 50 passed — P01, P02, P20, P21 |
| WP7·D4 notification policy | **done** | same file — P06, P07, P09 |
| WP7·D5/D6 weather pack | **done** | `test_weather.py` 123 passed — **WF01–WF24** |
| **Section 8 (weather)** | **complete** | all 24 WF scenarios verified |
| E1 learning + weekly review | **done** | `test_learning.py` 48 passed — P12–P18 |
| **Section 6 (proactivity/learning)** | **complete** | all 21 P scenarios verified |
| E2 research evidence + control gates | **done** | `test_research_and_gates.py` 50 passed — A05, A10, A18–A22 |
| **Section 5 (team/privacy/control)** | **complete** | all 23 A scenarios verified |
| E3 travel pack | **done** | `test_travel.py` 150 passed — **TR01–TR24** |
| **Section 7 (travel)** | **complete** | all 24 TR scenarios verified |
| E4–E8 operations, packaging, ops | **done** | `test_operations.py` 76 passed, `test_packaging.py` 9 passed (build checks) — **O01–O14** |
| **Section 10 (build/ops/release)** | **complete** | all 14 O scenarios verified |
| E9 final block | **done** | `test_remaining.py` 84 passed — T04–T07, D12–D14, EX06/07/11–14 |
| **All eleven sections** | **complete** | 196/196 verified |
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

# current, WP5 complete (2026-09-06)
pytest              879 passed (288 legacy + 591 vnext), 1 warning
ruff check .        All checks passed!
mypy .              Success: no issues found in 127 source files
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

## WP3 progress — B1, B4, B5 delivered

| File | Contract | Scenarios verified |
|---|---|---|
| `tests/vnext/vault_fixtures.py` | acceptance §1 fixtures | (harness) |
| `loop/vault/gateway.py` | vault §5, runtime §9 | **V04**, **V05**, **V12**, **V13**, **V30** |
| `loop/vault/ledger.py` | vault §5 six-column ledger | **V19**, **V20** |
| `loop/vault/capture.py` | vault §5 exact capture | **V02**, **V03** |

**Three real bugs found by tests.**
1. *Append recovery was structurally broken.* `desired_hash` held the hash of the appended
   fragment, but recovery compares it against the whole file — so every resumed append looked
   like a third-party conflict. `make_operation` now computes the resulting-file hash.
2. *A derived title inherited the body's pipe.* Bodies legitimately contain `|` (V02); a
   machine-derived title must not, or it corrupts the ledger row. Derived titles are sanitised;
   an explicitly supplied title with a pipe is still rejected as a user error.
3. *`reserve_path` could not recognise its own retry.* The second attempt saw the base path
   occupied and allocated a suffix, creating a sibling file for a note written once. It now reuses
   the base path when the existing bytes match what it is about to write.

**Mutations verified:** removing path confinement fails 2 V30 tests; removing the expected-hash
check fails the V12 concurrent-edit test and the replace test.

GlebOS at `/Users/gleb/Claude_Cowork/GlebOS` was never read or written; all tests use temporary
synthetic vaults.

## B2/B3 delivered

| File | Contract | Scenarios verified |
|---|---|---|
| `loop/vault/onboarding.py` | vault §1, §4 read-only onboarding | **V01**, **V26** |
| `loop/vault/policy.py` | vault §3 loading, precedence, conflicts | **V27**, **V28** |

Dry run is the **default** for apply: pointing a new tool at years of notes and having it
reorganise them is the failure users fear, so writing has to be earned with an explicit flag.
Precedence never consults modification time — recency is not authority — and a conflict between
equal-authority documents is reported with both paths rather than resolved by picking one.
A prepared routine's `enabled: true` is the author's intent, not permission: activation needs an
authenticated event, or anyone able to write to the vault could schedule background work.

*Fixture correction:* layout detection failed because the fixture's `CLAUDE.md` was thinner than
the observed vault it reproduces. The fixture was fixed to name the numbered folders, rather than
weakening the detector to match an under-specified fixture.

**Mutations verified:** removing last-valid policy retention fails the V27 retention test;
removing the dry-run guard fails the V01 no-writes test.

## B6–B8 delivered — Stage B complete

| File | Contract | Scenarios verified |
|---|---|---|
| `loop/vault/receipts.py` | vault §6 evidence | **V06**, **V07**, **V21**, **V23**, **V24**, **V29** |
| `loop/vault/compiler.py` | vault §2/§6 rules 1–9 | **V08**–**V11**, **V14**–**V18**, **V22**, **V32** |
| `loop/vault/search.py` | vault §6 retrieval | **V25**, **V31** |

Coverage is tracked as **byte ranges**, not a boolean: a long source read as a preview would
otherwise "have a receipt" while the cited claim sits in the unread remainder. Classification from
a preview is refused outright.

The compiler's hardest rule is 8 — *extract distinct concepts and connect them*. An implementation
eager to satisfy it will invent a second page and a plausible link. A genuinely isolated one-fact
source therefore yields `needs_context` and stays **pending**: filed, not compiled. Contradictions
are never resolved by preferring the newer or more confident claim; both sides are retained,
confidence becomes `contested`, and an open question is recorded. Rule 5 survives a rule 9 merge:
a human-owned page cannot be absorbed by rewriting it.

Retrieval is FTS5, not embeddings — a knowledge base that stops being searchable when Ollama is
down is not a knowledge base. Privacy filtering happens **in the SQL**, so a caller preparing
content for a cloud model never loads private rows at all.

**Mutations verified (Stage B):** path confinement, expected-hash check, last-valid policy
retention, dry-run guard, receipt range coverage, receipt hash check, contradiction retention,
human-ownership guard, search privacy filter. Each was disabled and confirmed to fail its tests.

*Also fixed:* one V15 assertion ended in `or True`, making it vacuous. Replaced with two real
assertions.

## WP4 C1–C4 delivered

| File | Contract | Scenarios verified |
|---|---|---|
| `loop/runtime/planner.py` | runtime §5 typed plans | **A01**, **A02**, **A03**, **A04** |
| `loop/runtime/authority.py` | runtime §10, agent-stack §2 | **A06**, **A08**–**A13**, **A23**, **LG04** |
| `loop/runtime/operations.py` | runtime §10 ledger | **A14**–**A17**, LG07 replay |

A plan is a proposal, and unknown fields are a **rejection** rather than something to ignore: a
model emitting `authority: owner` is usually pattern-matching, but an executor that accepts the
field has handed plan authorship the power to grant permissions. Runtime context is injected by
code and reserved argument names are stripped and logged.

Effect slots key on the **root event**, so two unrelated requests worded similarly get different
slots — merging them would silently drop one of the user's actual requests.

The ledger separates `authorized` from `committed` deliberately. A gate allowing an action is not
evidence it happened; recording "executed" because the check passed is how an audit log starts
lying. Approvals bind to operation + payload hash + actor + expiry, all four revalidated in one
place so a call site cannot forget one.

*Design note recorded:* the dependency-depth limit is unreachable within a single plan, because
depth can never exceed the step count and the step limit is equal. It binds across **nested**
plans, and is tested against `validate_dag` directly rather than through a plan that can never
trigger it.

**Mutations verified:** approval payload binding, reserved-argument stripping. Both fail their
tests when removed.

## C5 delivered — WP4 complete

`loop/capabilities/registry.py` + `tests/vnext/pack_fixtures.py`. Verifies **EX01–EX05**,
**EX08**, **EX09**, **EX10**.

Validation runs without importing pack code and without any network — a pack is data until
explicitly enabled. Refused before anything executes: schema `$ref`s leaving the package, remote
`$ref`s (a validator that fetches gives a pack an egress channel *before* enablement), reserved
builtin namespaces, two bindings for one mode, undeclared tools, and `default_arguments` carrying
authority. **Effects are declarations, not grants**; a manifest states what it would need and the
executor still requires real scoped authority.

Versions are immutable: changed bytes claiming the same version are rejected rather than
hot-swapped, because a job pinned to a version must be able to trust the bytes behind it.

The two fixture packs are deliberately **unrelated domains** (plant care, bike service) — two packs
from one domain could share a special case without anyone noticing, which would defeat EX02.

**Two test-quality problems found and fixed.** The cross-pack cycle test patched fixtures by string
replacement, which also matched inside the `tools:` list at a different indent; both packs became
invalid, so no cycle could be found and the test passed for the wrong reason. It now builds the
manifests explicitly and asserts both packs are valid first. Separately, mutation-testing the `..`
path check did *not* fail any test — the resolve-based containment check catches it anyway. With
**both** disabled the test fails, so the behaviour is protected; the `..` check is documented as
defence-in-depth rather than the load-bearing mechanism.

## WP5 delivered — generic coordinator and runners

| File | Contract | Scenarios verified |
|---|---|---|
| `loop/agents/coordinator.py` | agent-stack §2 stable generic stages | **LG02** |
| `loop/capabilities/runners.py` | agent/workflow/adapter dispatch, typed wrappers, artifact persistence | **LG01**, **LG02** |
| `loop/ai/model_gateway.py` | native LangChain-message policy adapter | **LG01** |

Agent packs execute through LangChain's real `create_agent`; workflow packs compile their
declarative DAG to LangGraph `StateGraph`; adapter mode resolves only explicitly registered trusted
handlers. The same coordinator graph runs load, discovery, typed planning, ready invocation,
validation, approval resolution and grounded composition for every domain.

LG01 asserts the stored artifact payload and evidence. Evidence comes from the authorized tool
result rather than from the model's final prose, and the final object must pass the pack's JSON
Schema before it is persisted. LG02 runs the unrelated plant-care agent and bike-service workflow
through the same coordinator and records hashes for coordinator, channel/service and core-schema
files before and after; none changes. Declared remote-write/spend effects are refused before a
handler can run until WP6 supplies a bound durable approval.

## WP6 delivered — the agent stack is complete

`loop/runtime/runs.py` + `tests/vnext/test_run_durability.py`. Verifies **LG06**–**LG11**.

Tests use a **real** `AsyncSqliteSaver` and real LangGraph `interrupt`/`Command(resume=...)`.
The restart tests open a brand-new saver against the same file, so only what reached disk
survives — verified by swapping in an in-memory saver, which fails the test.

Run mappings are the join between two databases that cannot share a transaction: domain SQLite
holds the authoritative business records, the checkpointer holds execution position. A thread
belongs to **one root run**, not a conversation — sharing it would let one request's cancellation
or approval leak into another's execution.

Three invariants worth restating:
* **A checkpoint is never evidence of an effect.** A graph that resumed past a send node proves
  only that the node ran; delivery truth stays in the outbox and the operation ledger.
* **Versions pin per run.** An upgraded pack pauses a paused run with an actionable reason rather
  than silently resuming it into different code.
* **Backups are paired.** Backing up one database without the other yields a restore where
  execution position and business records disagree; the manifest records both hashes so a
  mismatched pair is refused before it is restored.

**Mutations verified:** in-memory saver fails the restart test; removing version pinning fails 2
LG10 tests; removing backup hash verification fails the tamper test.

## WP7 progress — D1, D3 delivered

| File | Scenarios verified |
|---|---|
| `loop/connectors/health.py` | **P10**, **P11** |
| `loop/services/observations.py` | **P03**, **P04**, **P05**, **P08**, **P19** |

**Unavailable is not empty.** A calendar that could not be read has unknown contents; reporting
"no meetings" because the request failed is a confident false statement, and it is the one users
act on. Merged reads keep successful providers *and* name the failing ones.

Three-valued logic throughout: collapsing `unknown` into `false` turns "we could not check" into
"we checked and it is fine". A stale observation resolves `unknown`, so stale weather cannot
satisfy a rain predicate.

Edge triggering means a persistently true condition fires once and rearms only on false/unknown or
an explicit occurrence boundary — otherwise a condition that stays true becomes an unlimited
notification source. Outage notices are per *episode*: a provider failing every minute must not
notify every minute.

**Mutations verified:** removing the staleness filter fails 2 P04 tests; removing edge state fails
the P05 re-fire test; removing episode tracking fails the P11 re-notify test.

## WP7 progress — D2, D4 delivered

| File | Scenarios verified |
|---|---|
| `loop/runtime/routines.py` | **P01**, **P02**, **P20**, **P21** |
| `loop/runtime/notify_policy.py` | **P06**, **P07**, **P09** |

**A routine document is a proposal, not a permission.** `enabled: true` inside a file is the
author's stated intent; activation requires an authenticated event, because anything that can
write to the vault could otherwise schedule background work that sends messages.

**A missing location is a question, not a guess.** A timezone is not a location — `Europe/Berlin`
covers hundreds of places with different weather — so `_question_for()` never derives one from the
trigger. A capability Loop does not have is *not* a question: no answer from the user supplies a
missing adapter, so it stays a visible inactive proposal naming what is absent.

**Broadening scope is a permission change wearing the clothes of a content edit.** Retiming or
rewording applies after validation; adding a destination, tool or effect stays a proposal, and the
previously approved scope remains in force meanwhile.

**Notification categories come from authority, not urgency.** A model may argue relevance but
cannot promote a discretionary suggestion into a reminder — only the executor assigns a category,
from the authority the candidate arrived with. Otherwise "this is important" is the bypass. An
activated routine's *own* scheduled output is non-discretionary; other suggestions it happens to
produce are not, or activating one routine would exempt everything downstream of it from the cap.

A rescheduled meeting's old reminder is not merely late — it is wrong, so `supersede()` cancels it
rather than delivering advice about something that is no longer happening.

**Mutations verified (7, all killed):** dropping the activation-event check fails the P01 refusal
test; deriving the location from the timezone fails the P02 no-guess test; treating a widened
destination as a content edit fails the P21 old-scope test; letting quiet hours silence
non-discretionary categories fails the P07 tests; removing the daily cap fails the P06 cap test;
making `supersede()` cancel nothing fails P09; making every activated-routine output
non-discretionary fails the category-assignment test.

## WP7 D5/D6 delivered — the weather pack

| File | Scenarios verified |
|---|---|
| `loop/capabilities/weather/sources.py` | **WF01**, **WF02**, **WF09**, **WF10**, **WF22** |
| `loop/capabilities/weather/normalize.py` | **WF06**, **WF07**, **WF08** |
| `loop/capabilities/weather/compare.py` | **WF03**, **WF04**, **WF05**, **WF15**, **WF17** |
| `loop/capabilities/weather/warnings.py` | **WF11**–**WF14**, **WF20** |
| `loop/capabilities/weather/bundle.py` | **WF16**, **WF18**, **WF19**, **WF21**, **WF23**, **WF24** |

Sources are synthetic (`tests/vnext/weather_fixtures.py`). No test touches a network, a
credential or a paid model.

**Independence is lineage, not hostnames.** `open_meteo_dwd` re-serves the same DWD ICON run as
`dwd`; counting their agreement as corroboration manufactures confidence out of a copy. Both are
one evidence family, and the comparison is labelled `correlated_sources`.

**Locality is a preference with three preconditions** — freshness, product suitability and actual
coverage. A stale local forecast loses to a fresh fallback, and a valley station is excluded from a
mountain point by its declared elevation band rather than winning on proximity.

**Two functions are deliberately unimplemented.** `split_probability` and `combine_probabilities`
raise `NotImplementedError`, as does `consensus`: dividing a 3-hour probability into hourly ones,
averaging hourly ones into a trip risk, and averaging sources into a consensus number all produce
values no source issued and nobody can attribute. Leaving them absent means they cannot be reached
by accident.

**Silence is never an all-clear.** `WarningState` is three-valued; only a feed that succeeded, was
complete and was fresh reaches `none_in_checked_feed`, and even that is a statement about the feed.
An alert is cleared only by explicit cancellation, its own expiry, or absence from a feed the
publisher *documents* as a complete snapshot.

**WF20 forced a distinction the notification policy did not have.** Activating a warning
subscription grants delivery authority, not timing authority: the user chose the trigger, not the
hour. `Candidate.timing_chosen_by_user` now separates "you set this time" (a reminder at 23:00)
from "something happened at this time" (an alert), so an official label alone cannot buy a
quiet-hours exemption.

**Mutations verified (12, all killed):** ignoring lineage; skipping the staleness filter in
selection and again in the bundle; measuring age from `fetched_at`; dropping statistic/interval
from the comparability key; reporting a failed warning feed as no warnings; treating any successful
feed as a complete snapshot; ignoring elevation suitability; summing across an interval gap;
unbounding the fetch budget; nudging the primary probability to match a comparator; restoring the
blanket quiet-hours bypass.

Two of those twelve initially **survived**, which is the point of running them: no test covered a
healthy non-snapshot feed omitting a known alert, and the "missing interval" test used a null value
rather than a gap between intervals. Both gaps are now closed by named tests.

## E1 delivered — learning, preferences and the weekly review

| File | Scenarios verified |
|---|---|
| `loop/services/learning.py` | **P12**, **P13**, **P14**, **P15**, **P16**, **P17** |
| `loop/services/review.py` | **P18** |

**Nonresponse is not feedback.** There is no `FeedbackKind` for "ignored" and none for "seen", so
silence cannot be recorded, cannot become evidence, and cannot reach the learner. Ten unanswered
messages are ten things Loop does not know — not dislike, not consent, not completion. This is
enforced structurally rather than by a check, because silence is the most abundant signal available
and the least informative one.

**Every timing threshold blocks one specific false positive.** Distinct *days* rather than events,
because five taps in one frustrated minute is one bad morning. A minimum median shift, because a
four-minute median is noise wearing a signal's clothes. A maximum IQR, because a wide spread means
the user snoozes to whenever they are free and no single new time would have helped.

**Three record kinds stay apart.** An explicit statement governs; a hypothesis never does, is
stored separately, and expires after 30 days without fresh evidence. Only the user's acceptance
promotes an inference into something that decides behaviour. An explicit statement does not merely
outrank a conflicting hypothesis — it supersedes it, so the inference cannot resurface as a
proposal the user has already answered.

**Forgetting removes, it does not archive.** The canonical record, the derivatives quoting it, and
any queued proposal that would reintroduce it all go; the key is remembered as forgotten so the
learner cannot re-propose it. Archiving a private copy would leave the next digest saying it again.

**A stale dashboard is evidence of staleness, never a progress source (P18).** The review reports
what the canonical goal and project records say, and says "progress not recorded" where they say
nothing. The tempting alternative — filling the gap from a dashboard computed six days ago —
produces a number that looks right, carries no visible age, and misstates a week of work. The 14-
and 30-day inactivity thresholds are kept as separate constants: merging them would silently
rescope a routine already in the user's vault.

**Mutations verified (11, all killed):** counting same-day clicks separately; removing the variance
limit, the minimum shift, and the 28-day window; letting a proposal govern without confirmation;
removing the rejection cooldown; dropping supersession on an explicit statement; making `forget`
archive rather than remove; leaving queued proposals alive after forgetting; removing the
hypothesis TTL; falling back to the dashboard number in the review.

## E2 delivered — research evidence and the remaining control gates

| File | Scenarios verified |
|---|---|
| `loop/agents/seeker.py` | **A05** |
| `loop/ai/model_gateway.py` (tests only) | **A10** |
| `loop/services/export_map.py` | **A18** |
| `loop/ai/spend.py` | **A19**, **A20** |
| `loop/services/diagnostics.py` | **A21** |
| `loop/runtime/intake.py` (tests only) | **A22** |

**A researcher and a reviewer are not independent failures.** A model that invents a citation and
a model that approves it are reading the same fabricated text, so two opinions do not add up to
evidence. `compile_draft` accepts a `ReviewVerdict`, records it, and deliberately does not consult
it: the gate is a deterministic check that every claim cites a source Loop actually read, at a span
the read receipt actually covers. A preview read cannot support a claim about page twelve.

**Money needs its own ledger.** `RootBudget` bounds one request; `DailySpendLedger` bounds the day
across all of them. Reserve the estimated maximum *before* the call and settle after — charging
afterwards lets N concurrent calls each observe the same headroom and each proceed. Unknown pricing
refuses rather than guesses, and an unsettled reservation keeps holding its estimate, because "we
do not know whether we spent this" is not "we did not spend it" (A20). None of these refusals
touches local inference, which costs nothing and stays available.

**Diagnostics allow named fields rather than stripping known-bad ones.** A denylist fails open, and
the first field someone adds without thinking ships message bodies to a bug tracker. `prompt_hash`
is allowed although it contains "prompt": a hash identifies a call without revealing it.

**Export is opt-in per object.** "Push everything to Wrike" is a statement about a project, not
about every row in the same table. An object with no mapping and no covering scope stays local —
an unwanted push is visible to other people and cannot be taken back.

**Mutations verified (10, all killed):** letting reviewer approval unblock compilation; passing
uncited claims; skipping span coverage; removing the spend lock; releasing an unknown reservation;
guessing an unpinned price; switching diagnostics to a denylist; removing secret masking; pushing
unmapped objects by default; ignoring object type in an export scope.

The spend-lock mutation initially **survived**: the threaded test detected it about one run in
four, and a test that usually passes on broken code is not a test. The check-then-act window is now
widened deterministically — `Reservation.__init__` is constructed between reading the committed
total and writing it back, so a monkeypatched sleep there is exactly the interval the lock must
cover. The mutation now fails on every run.

## E3 delivered — the travel pack

| File | Scenarios verified |
|---|---|
| `loop/capabilities/travel/brief.py` | **TR01**–**TR03**, **TR21**, **TR22** |
| `loop/capabilities/travel/money.py` | **TR08**, **TR09** |
| `loop/capabilities/travel/schedule.py` | **TR05**–**TR07**, **TR14**, **TR18** |
| `loop/capabilities/travel/options.py` | **TR01**, **TR04**, **TR12**, **TR23** |
| `loop/capabilities/travel/evidence.py` | **TR10**, **TR11** |
| `loop/capabilities/travel/trip.py` | **TR13**, **TR19**, **TR20**, **TR24** |
| `loop/capabilities/travel/monitor.py` | **TR15**–**TR17**, **TR19**, **TR23** |

All fixtures are synthetic (`tests/vnext/travel_fixtures.py`). Nothing in this pack books, pays,
messages a host, contacts a provider, or writes a calendar event.

**Money is integer minor units and `Decimal`, never binary floating point.** A total of
€1,199.9999999 passes or fails a €1,200 hard limit depending on rounding nobody chose. Cross-currency
comparison without a dated sourced rate returns `uncomparable` and separate totals — declining is
the real answer, because "probably about €1,150" invites a booking decision that separate totals do
not. Unknown mandatory fees make compliance tentative: a total omitting a compulsory city tax is not
under budget, it is a total of the wrong thing.

**Timezones live on segments, not on the itinerary.** A flight's `end_timezone` differs from its
start, so `end_utc` converts using the arrival zone. The Tokyo→Berlin fixture reads as six hours on
the clock and is thirteen in fact; a single-zone reading gets both the duration and the arrival date
wrong.

**Ranking is deterministic and its order is fixed by the spec**, because "which trip is better" is a
question a model answers enthusiastically and differently each time. Feasible before tentative
before infeasible, then the user's ordered priorities, then fewer unresolved facts, then the option
ID so the same inputs always produce the same order. When nothing is feasible the answer is
`no_feasible_plan` with the conflicting constraints named — dropping the budget to produce three
options answers a question nobody asked.

**Selection is the user's act alone.** A new proposal marks older ones stale and flags the selected
plan for review; it never moves the selection. The plan someone is travelling on cannot change
because a background recheck preferred something else.

**A search snippet is not a source** — it is a search engine's summary of a page nobody opened. And
"price changed" requires a quote for the same dates, party and inclusions: €200 for two people is
not an increase over €120 for one, and reporting it as one teaches the user to ignore alerts.

**Mutations verified (21 run, 20 killed, 1 retired):** guessed exchange rate; per-person costs not
multiplied; shared costs multiplied; unknown mandatory fees ignored; arrival timezone ignored;
operator connection minimums ignored; unknown accessibility reported as fine; last admission
unchecked; infeasible options ranked as equals; `no_feasible_plan` padded; snippets accepted as
evidence; one global freshness ceiling; `expected_version` unenforced; a new proposal moving the
selection; a human file overwritten; unsupported checks silently dropped; past checkpoints firing
immediately; quote-basis comparability ignored; repeated events notifying every time; private
context forwarded externally.

The 21st mutation **survived**, and the finding was in the code rather than the test:
`arrival_local_date` routed `end_local` through UTC and back into the same zone, which returns the
same value. It looked like a mechanism and was a no-op. It is now a plain read with a comment naming
`end_utc` as the check that actually carries timezone correctness — the same situation as the
`..` path check recorded under Stage B.

## E4–E8 delivered — interfaces, packaging and operations

| File | Scenarios verified |
|---|---|
| `loop/api/service.py` | **O07**, **O08** |
| `loop/ops/doctor.py` | **O04**, **O05**, **O13** |
| `loop/ops/backup.py` | **O09** |
| `loop/ops/retention.py` | **O10**, **O11** |
| `loop/ops/perf.py` | **O12** |
| `pyproject.toml`, `Dockerfile` | **O01**, **O02**, **O03** |
| `scripts/acceptance_run.sh`, `tests/vnext/test_packaging.py` | **O14** |

### Two real defects found by running the builds rather than testing around them

**The wheel did not contain the `loop` package.** `pyproject.toml` carried a hand-written
`[tool.setuptools] packages = [...]` list from the legacy layout. It predated `loop/` and never
gained it, so `uv build` produced a wheel that imports perfectly from the source tree and raises
`ModuleNotFoundError: No module named 'loop'` anywhere else. Replaced with a `packages.find`
directive whose patterns end in `*`, plus `package-data` for the dashboard templates — a wheel
without those imports fine and fails at the first page request. Verified by installing the wheel
into a venv outside the checkout and importing 20 modules with the working directory set to `/`.

**The container ran as root.** No `USER` directive, so a bug writing outside `/app` writes as uid 0
on the host through any bind mount. Added a dedicated uid 10001 user, with `/app` chowned to it
before the switch.

Both are invisible to unit tests by construction: one needs an install outside the tree, the other
needs a container. That is why `tests/vnext/test_packaging.py` shells out to the real tools.

### Notes on the rest

**"Configured" and "working" are different answers.** `Readiness.UNCONFIGURED` is not a failure —
not setting up a calendar is a choice; a calendar that cannot authenticate is a fault. Collapsing
them into one green tick is how someone learns their reminders stopped by missing one.

**`loop status` never claims continuous availability.** A laptop that slept through the night did
not run the 07:00 routine, so `ServiceHealth` reports heartbeat age and discloses the catch-up
limit rather than reporting "running" because the process is alive now.

**Restore refuses a non-empty target.** A recovery attempt that cleans its destination first
destroys the thing it was recovering. The vault part is hashed path-sensitively, so a renamed file
inside a backup is detected — identical bytes under a different name is a different vault.

**Retention removes expiring operational state only**, never vault evidence or human content, and
never anything an active piece of work still pins. A task outliving the note explaining it is the
worse outcome.

**Index rebuilds derive from sources and write to neither** — `rebuild_index` has no route to a
source, because the source is not its output.

### Verification status, stated precisely

* **O01, O02, O14** — verified live this session: `uv sync --locked`, `uv lock --check`, a wheel
  built and installed into an isolated venv, lint, types, and the matrix summary. 9 packaging tests
  pass.
* **O03** — the image was built and inspected live this session: it builds, runs as uid 10001
  (`loop`), imports the `loop` package, contains both `core/` and `loop/`, carries no `.env` or
  credential files, and can write `/app/data` as the run user. The automated equivalents exist in
  `test_packaging.py` but were **not re-run afterwards**, because the user asked to skip further
  Docker builds. They are written and will run under `scripts/acceptance_run.sh`.
* **O04–O13** — verified by the hermetic suite.

`test_packaging.py` is opt-in (`LOOP_PACKAGING_TESTS=1`), so the default suite stays hermetic and
fast. `scripts/acceptance_run.sh` runs the full gate in cheapest-fails-first order.

## E9 delivered — the final block

| File | Scenarios verified |
|---|---|
| `loop/runtime/interpret.py` | **T04**, **T05**, **T06**, **T07** |
| `loop/runtime/triggers.py` (`retime_floating`) | **D12** |
| `loop/runtime/operations.py`, `loop/api/service.py` | **D13** |
| `loop/runtime/jobs.py`, `loop/ops/backup.py` | **D14** |
| `loop/runtime/planner.py` | **EX06** |
| `loop/api/service.py` | **EX07** |
| `loop/capabilities/objects.py` | **EX11**, **EX13**, **EX14** |
| `tests/vnext/conftest.py`, `loop/core/settings.py` | **EX12** |

**Saved is not scheduled.** "Remind me later to call" has no time in it. The task is saved — losing
it would be worse — but the plan is `needs_input` and `may_claim_scheduled` is false, so no reply
can say a reminder is set. A cheerful "I'll remind you!" for a reminder that does not exist is the
failure this path exists to prevent.

**A thing to do and a thing that is true are different commitments.** An infinitive after
"remember" is an action; a "that"-clause is a fact. Filing the fact as a task produces a to-do
nobody can complete; filing the action as a note produces a reminder that never fires. A capture is
stored verbatim, because paraphrasing is how "six weeks" becomes "about a month".

**A clarification completes the waiting task rather than starting a new one.** Reading "tomorrow at
10" as a fresh request turns one dentist appointment into two entries, and the duplicate outlives
the correction.

**Floating and pinned schedules are not the same routine.** "07:00 wherever I am" should follow the
owner to Lisbon; "07:00 Berlin time" usually means something in Berlin happens then, and rewriting
it would silently move a real appointment. Past fires are never rewritten.

**Capability objects give a pack somewhere to keep state without a core migration.** One shared
store, a schema the pack registers, `expected_version` on writes, and a privacy label that travels
with the object — so storing private data and reading it back cannot launder the label. A breaking
schema change needs an explicit migration and authority; a rollback restores local state and says
plainly what already left the machine, because reporting "reverted" after an email was sent
describes a state that exists nowhere.

**Mutations verified (12, all killed):** treating a vague time as concrete; classifying "remember
that" as a task; making a clarification create a new task; claiming a reminder while `needs_input`;
rewriting an explicit timezone; allowing a breaking schema change without authority; treating a new
required field as compatible; skipping `expected_version`; dropping the privacy merge on read;
skipping payload validation; claiming a rollback fully reversed; accepting `bool` as an integer.

## Two corrections made during this milestone

**I overwrote an existing test file.** `tests/vnext/test_operations.py` already held the A14–A17
operation-ledger tests; the O-series file I wrote replaced it. The suite still passed, because the
new file was green and the matrix rows pointing at the old names were not re-checked. Recovered
from `HEAD`, the O-series moved to `tests/vnext/test_ops_and_packaging.py`, and the 13 affected
matrix references retargeted. Both files now pass. Nothing was lost, but the near miss is the
argument for `git status` before `cat >` on a path under `tests/`.

**The matrix summary under-reported section 11.** `scripts/acceptance_summary.py` matched the stage
cell with `[A-Z—]` — a single character — so the `C/E` row never matched and its counts stayed
frozen at whatever was last written by hand. Section 11 had in fact been complete since WP6. The
grand total was always right, because it sums the tally directly rather than the section rows,
which is why the discrepancy survived. Regex widened to `[A-Z/—]+`.

## Post-acceptance: real implementations (in progress)

Acceptance is green, but several paths were *contracts with synthetic inputs*. This section tracks
turning them into working code. Two real defects were found by doing so.

### Weather providers — working against live data

`loop/capabilities/weather/adapters/` and `service.py`. Open-Meteo serves DWD ICON, ECMWF IFS and
NOAA GFS through one API shape, so it gives genuine multi-source comparison with no credential.
Transport is separated from parsing: only `base.py` touches a network, so the parser is tested
against a recorded payload and the whole path is testable offline.

**Verified live**: three models fetched for Berlin, compared, rendered — the run produced a real
5.5 °C disagreement between ICON and the other two and correctly reported `disagreement` with the
range, rather than averaging them.

**A defect this exposed.** Open-Meteo publishes no model run time, so the adapter honestly leaves
`issued_at=None`. `build_bundle` was treating any non-fresh sample as stale and excluding it, which
would have discarded every real sample. Stale and unknown are different answers: a stale product
has a known issue time that is too old and is excluded; an unknown one is still the data the
provider is serving, so it is used, the bundle is `degraded`, and the brief says the currency of
the data cannot be confirmed. Collapsing them either throws away every source that does not publish
a run time, or lets one masquerade as fresh.

### Agent stack — the coordinator is now actually durable

`test_run_durability.py` proved the LangGraph mechanisms in isolation, but **none of them were
wired into `Coordinator`**: it compiled with no checkpointer, `_resolve_approval` returned `[]`
unconditionally, and there was no `interrupt()`, no `Command(resume=...)`, and no `RunStore` use.
Every LG scenario passed while the production coordinator could not pause.

Now: compiled with a checkpointer; operations needing approval persist a bound approval, call
`interrupt()` and pause before the effect; `resume()` revalidates version pinning, resumability,
cancellation and the deciding actor; a run mapping is recorded pinned to the graph, schema and pack
versions it started under; and cancellation plus pack availability are rechecked before *every*
effect. `RegisteredHandler` gained `needs_approval`, because adapter-mode capabilities are the ones
with outward effects and could previously bypass the gate entirely.

**A spec violation this exposed.** `AuthorityContext` was being placed in graph state, which
msgpack refused to serialise. Agent-stack §4 requires graph state to hold typed identifiers, "not
open clients or credentials" — and the context carries a live budget, granted scopes and a privacy
label. State now holds the plan *payload* and plain result summaries; the context is supplied per
invocation and must be supplied again after a restart, which is what "reload current domain state
on resume" asks for. Resuming under a snapshotted authority would execute with permissions the
owner may no longer have.

**Mutations verified (10, all killed):** compiling without the checkpointer; removing the approval
gate; executing a declined decision; skipping the actor recheck; ignoring a handler's approval
flag; backfilling `issued_at` from the fetch time; turning null forecast values into zero;
collapsing the three models into one lineage; parsing a non-200 response.

The cancellation mutation initially **survived**: the resume-time check is redundant with
`RunStore.require_resumable`. The load-bearing one is the recheck before each effect, which catches
a cancellation arriving *mid-plan* when the run was never paused — now covered by a test that
cancels from inside a running step.

### Still contract-only

Honest list of what remains synthetic, for the build/data testing:

* **Travel research adapters.** The pack's normalization, comparison, scheduling and monitoring
  rules are real; `travel.routes`, `travel.stays` and `travel.places` have no live providers.
* **Warning feeds.** `WarningState` logic is real and tested; no DWD/GeoSphere CAP adapter exists,
  so `warning_state` is `unknown` on live runs — which is the correct answer, not a silent gap.
* **In-memory stores.** `RoutineService`, `PreferenceStore`, `TripStore`, `CapabilityObjectStore`,
  `DailySpendLedger`, `NotificationManager` counters and `ExportMap` hold state in process and lose
  it on restart. Their logic is verified; persistence is not yet wired.
* **No CLI or HTTP app in the new stack.** `pyproject` still points `loop` at the legacy
  `cli.main:main`; `loop/api/service.py` implements the mutation semantics but has no FastAPI app
  or DB binding.
* **No shipped capability pack manifests**, so `CapabilityRegistry` discovers nothing by default.

## Agent-stack audit against the full spec package

Read all nine specification documents and audited the implementation against them. Findings:

### Fixed this session

**The seven roles existed only as strings.** `ROLES` was a frozenset the planner validated against,
duplicated in two files. Runtime §1 says "role identity determines instructions and tool scope" —
neither existed. `loop/agents/roles.py` now gives each role a brief and a deny-by-default scope
allow-list, filtered *before* a catalogue is disclosed to a model (agent-stack §2), because an agent
that can see a tool will eventually call it. Scopes are derived from the operation verb, so a pack
cannot name its own scope and thereby choose its own permissions. The role list now has one
definition that the planner and capability registry both import.

**The vault's agents are a different system.** GlebOS `_ctx/agents/` holds four Claude Code
subagents. Three — `scribe`, `compiler`, `seeker` — cover the same domain conventions as Loop roles
of the same name and are consulted as *supplementary guidance*, clearly attributed and never
authoritative. Loop never writes them (runtime §1).

**`edward.md` is a data-visualisation critic, not a review agent.** Runtime §1 says "Edward's
synthesis/review work maps to Coordinator/Reviewer"; following that literally would have loaded
Tufte chart-critique instructions into the coordinator, which is what an earlier draft of this
module did. His expertise is real and Loop does render dashboards, weekly reviews and metrics, so he
is adopted as an **advisory document bound to one operation** — `presentation.critique`, consultable
by the Reviewer. An advisory contributes expertise to a single operation and grants nothing:
consulting him does not widen the Reviewer's scope. The conflict with runtime §1 is recorded rather
than silently resolved.

**A migration collision, same class as revision 0000.** Revision 0003 adds a `preferences` table,
but Phase 4 already shipped one with a different shape. `CREATE TABLE IF NOT EXISTS` silently kept
the old table and the index on `state` then failed. The old table is now moved to
`legacy_preferences`, and `migrate_legacy` reads whichever name holds the rows.

### Still open against the spec

Audited and confirmed missing:

* **`AsyncSqliteSaver` is not used in production.** Agent-stack §4 names it specifically, at
  `data/graph-checkpoints.sqlite`. The coordinator accepts any checkpointer and the tests use the
  sync `SqliteSaver`. Needs an async invoke path plus a factory that applies `secure_checkpoint_file`.
* **No child run IDs.** Agent-stack §2: dynamic graphs called through tools are invisible to static
  inspection, so child run IDs and checkpoint references must be recorded for status and cancellation.
* **No Pydantic structured output.** Agent-stack §3 requires structured output through supported
  LangChain interfaces; runners currently hand-parse JSON from the last AI message.
* **No Reviewer stage or bounded repair loop.** Runtime §1: "Failed checks return bounded repair
  work to the Coordinator", and §3: repair attempts count against the original budget. The
  coordinator's `_validate_results` is a step-count check.
* **Roles are defined but not yet dispatched.** The registry exists; the coordinator does not yet
  filter its shortlist through it or attach role instructions to an agent call.

Plus the pre-existing list: travel research adapters, warning feeds, persistence for the in-memory
stores (schema landed in revision 0003, repositories not yet written), no CLI or HTTP app over the
new stack, no shipped pack manifests.

## Exact next step

**Dispatch roles through the coordinator** (filter the shortlist by role scope, attach role
instructions to agent runs), then the **Reviewer stage with bounded repair**, then
**AsyncSqliteSaver + child run IDs + structured output**. Persistence repositories follow.

## Current check results

```
pytest tests/            1608 passed, 16 skipped
ruff check loop tests    All checks passed!
mypy loop                Success: no issues found in 80 source files
```

All work remains **staged but uncommitted** at the user's instruction.
