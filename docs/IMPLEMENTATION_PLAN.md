# Loop vNext 1.2 — implementation plan

**Branch:** `vnext-implementation` · **Started:** 2026-09-05 · **Spec:** `docs/SPECIFICATION.md` + 7 contracts
**Companion documents:** [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) (resumable progress) ·
[`ACCEPTANCE_MATRIX.md`](ACCEPTANCE_MATRIX.md) (184 scenarios → code → tests)

## 0. Baseline recorded before any change

Captured on branch creation so regressions are distinguishable from pre-existing failures.

| Check | Result at baseline |
|---|---|
| `pytest` | 288 passed, 0 failed |
| `ruff check .` | clean |
| `mypy .` | clean, 61 source files |
| `uv.lock` | **absent** — must be generated (interfaces §8) |
| Python | 3.12.11 in `.venv` |

Pre-existing uncommitted user work preserved untouched: `CLAUDE.md`, `Dockerfile`, `README.md`,
`delivery/telegram.py`, `integrations/telegram_bot.py`, `pyproject.toml`, `scripts/setup.sh`,
`LICENSE`, and the vNext specification set itself.

## 1. What exists versus what the target requires

The current checkout is the archived Phase 4 system. It is **not** a subset of vNext; several
core invariants are absent, so most of it becomes a *migration source* rather than a foundation.

| Area | Today | vNext requirement | Disposition |
|---|---|---|---|
| Schema | 8 tables, `create_all` + additive `_migrate` | 30 logical tables, versioned migrations | **Replace**; migrate data (interfaces §9) |
| Concurrency | none | leases, fencing tokens, idempotency, outbox | **New** |
| Scheduling | APScheduler in-process | persisted triggers/firings/jobs with DST rules | **Replace** |
| Agents | 4 sync specialists | 7 roles, typed plans, work items, budgets | **Replace** |
| Vault | read + naive write | journaled gateway, receipts, ledger, crash recovery | **Replace** |
| Privacy | `local_only` bool on a router call | `PrivacyLabel` on every content-bearing row | **Extend everywhere** |
| Capabilities | hardcoded methods | registry, manifests, generic invocation | **New** |
| Connectors | Gmail/Graph/Wrike (working) | same + health/cursor state rows | **Keep, wrap** |
| Autonomy gate | working, ceilinged | maps to typed permissions + approvals | **Keep, extend** |
| CLI/API/bot | ~15 commands | ~60 commands, `/api/v1`, envelopes | **Extend** |

Reusable as-is: `integrations/{gmail,google_auth,google_calendar,ms_graph,outlook,outlook_calendar}.py`,
`core/autonomy.py` semantics, `core/projects.py`, `integrations/transcribe.py`, the 288 existing tests
(which must keep passing or be explicitly migrated).

## 2. Dependency order and milestones

Each milestone ends with working behavior, its acceptance IDs verified, and a status checkpoint.
A milestone is not complete while any of its scenarios is `pending`.

### Stage A — durable commitments (T01–T15, D01–D17, part of O)

| # | Milestone | Key files | Proves |
|---|---|---|---|
| A1 | Foundations: settings, clock, IDs, privacy label, error taxonomy | `loop/core/{settings,clock,ids,privacy,errors}.py` | Injected clock; label merge algebra |
| A2 | Schema + versioned migrations + legacy migration | `loop/db/{models,migrations/*}.py`, `loop/migrate.py` | Populated legacy snapshot upgrades |
| A3 | Event intake: auth, dedupe, idempotency, envelope | `loop/runtime/intake.py` | T02, T15 |
| A4 | Task service + lifecycle + versioning | `loop/services/tasks.py` | T01, T03, T06, T11, T12, T14 |
| A5 | Triggers, occurrences, DST, catch-up | `loop/runtime/triggers.py` | D07–D10, D12 |
| A6 | Job queue: leases, fencing, retry, cancellation | `loop/runtime/jobs.py` | D01–D03, D11, D14 |
| A7 | Notifications + outbox + delivery truth | `loop/runtime/{notifications,outbox}.py` | D04–D06, T08–T10 |
| A8 | Loop run / worker lifecycle, single-leader | `loop/runtime/service.py`, `loop/cli/run.py` | D15, D16 |
| A9 | Stage A demo: task → restart → reminder → snooze/done | `tests/e2e/test_stage_a.py` | End-to-end |

### Stage B — GlebOS fidelity (V01–V32)

| # | Milestone | Key files | Proves |
|---|---|---|---|
| B1 | Synthetic vault fixtures (minimal + 500-source perf) | `tests/fixtures/vault/` | Harness §1 |
| B2 | Read-only onboarding, manifest, layout detection | `loop/vault/onboarding.py` | V01–V04 |
| B3 | Policy loading, rule conflicts, precedence | `loop/vault/policy.py` | V05–V09 |
| B4 | Exact capture + ledger (pipes, Unicode, collisions) | `loop/vault/{capture,ledger}.py` | V10–V16 |
| B5 | Journaled gateway + crash recovery at each boundary | `loop/vault/gateway.py` | V17–V22 |
| B6 | Read receipts + provenance validation | `loop/vault/receipts.py` | V23–V26 |
| B7 | Compiler rules 1–9, contradictions, needs_context | `loop/agents/compiler.py` | V27–V30 |
| B8 | Lexical retrieval (FTS5) + cited answers | `loop/vault/search.py` | V31, V32 |
| B9 | Stage B demo: capture → compile → cited retrieval | `tests/e2e/test_stage_b.py` | End-to-end |

### Stage C — coordination and extensions (A01–A23, EX01–EX14)

| # | Milestone | Key files | Proves |
|---|---|---|---|
| C1 | Typed plans, work items, dependency DAG validation | `loop/runtime/planner.py` | A01–A06 |
| C2 | Privacy/authority propagation through assignments | `loop/runtime/authority.py` | A07–A12 |
| C3 | Budgets, cancellation, partial results | `loop/runtime/budget.py` | A13–A18 |
| C4 | Approvals, operations, effect slots | `loop/runtime/operations.py` | A19–A23 |
| C5 | Capability registry + manifest validation | `loop/capabilities/registry.py` | EX01–EX07 |
| C6 | Generic invocation, version pinning, upgrade | `loop/capabilities/invoke.py` | EX08–EX11 |
| C7 | `capability init/validate/test/enable` + template | `loop/cli/capability.py` | EX12–EX14 |
| C8 | Stage C demo: two unrelated packs, zero core edits | `tests/e2e/test_stage_c.py` | End-to-end |

### Stage D — proactive support (P01–P21, WF01–WF24)

| # | Milestone | Key files | Proves |
|---|---|---|---|
| D1 | Connector health/cursor state, independent degradation | `loop/connectors/health.py` | P01–P05 |
| D2 | Routines: documents, activation authority, validation | `loop/runtime/routines.py` | P06–P11 |
| D3 | Observations: expiry, conflict, three-valued logic | `loop/services/observations.py` | P12–P16 |
| D4 | Notification policy: quiet hours, caps, digest, categories | `loop/runtime/notify_policy.py` | P17–P21 |
| D5 | Weather bundle: multi-source, local-preferred, evidence | `loop/capabilities/weather/` | WF01–WF16 |
| D6 | Disagreement, fallback, staleness, warnings | `loop/capabilities/weather/compare.py` | WF17–WF24 |
| D7 | Stage D demo | `tests/e2e/test_stage_d.py` | End-to-end |

### Stage E — full operation (TR01–TR24, O01–O14, remainder)

| # | Milestone | Key files | Proves |
|---|---|---|---|
| E1 | Preferences, hypotheses, feedback, forgetting | `loop/services/learning.py` | P-series remainder |
| E2 | Research → save → output drafting with source chain | `loop/agents/seeker.py` | R3 scenarios |
| E3 | Travel: plan/compare/revise/select/monitor | `loop/capabilities/travel/` | TR01–TR24 |
| E4 | Remaining CLI, `/api/v1`, dashboard pages, Telegram set | `loop/cli/`, `loop/api/` | O-series interfaces |
| E5 | Optional-provider degradation matrix | `loop/doctor.py` | O-series |
| E6 | Packaging: uv.lock, Docker build check, install outside tree | `Dockerfile`, CI | O-series build |
| E7 | Backup/restore, migration verification | `loop/backup.py` | O-series ops |
| E8 | Stage E demo + full transcripts | `tests/e2e/test_stage_e.py` | End-to-end |

## 3. Package layout

New code lands under a `loop/` package so the legacy modules stay importable during migration and
are removed only once their scenarios are verified elsewhere.

```
loop/
  core/        settings, clock, ids, privacy, errors, types
  db/          models, migrations/, session
  runtime/     intake, planner, jobs, triggers, operations, notifications, outbox, service
  services/    tasks, observations, learning, preferences
  vault/       gateway, capture, ledger, policy, receipts, search, onboarding
  agents/      coordinator, commitments, scribe, compiler, seeker, daily_life, reviewer
  capabilities/ registry, invoke, builtin/, weather/, travel/
  connectors/  wrappers over existing integrations/ clients
  cli/         typer command groups
  api/         FastAPI app, routers, templates
```

## 4. Cross-cutting rules that apply to every milestone

Drawn from the contracts; violating one is a defect even if tests pass.

1. **Deterministic code owns effects.** Models propose typed plans; the executor performs mutations.
2. **Injected clock, model, transport everywhere.** No test touches real `.env`, network, or paid model.
3. **Privacy label on every content-bearing row**, merged by `local_only` OR / `sensitive` OR /
   origins ∪ / destinations ∩. A caller-supplied `local_only: false` can never downgrade.
4. **`expected_version` on every mutation.** Mismatch is `conflict`, never last-write-wins.
5. **No transaction held across a model or network call.**
6. **Verify actual results**: a trigger row exists, a capture has file *and* ledger, an operation has
   evidence, a send has a receipt. Absence of proof is `unknown`, never success.
7. **GlebOS is read-only in development.** Synthetic vaults only; no live writes.
8. **Never weaken an invariant or reduce scope to make a test pass** — record a deviation instead.

## 5. Verification per milestone

`pytest` (unit + integration + crash/restart), `ruff`, `mypy`; plus for Stage E: package install
outside the source tree, Docker build, process-restart recovery, backup/restore, and `shellcheck`
when shell scripts change. Every acceptance row names its test and carries one of:
`pending` · `implemented` · `verified` · `blocked` · `optional-unconfigured`.

## 6. Known specification decisions to resolve before dependent code

Recorded here when found; resolved in `IMPLEMENTATION_STATUS.md` with rationale.

| # | Question | Affects | Status |
|---|---|---|---|
| 1 | Legacy `tasks` rows carry `due_date` only; vNext requires exactly one of `due_date`/`due_at` | A2 migration | Resolved: keep dates as dates (interfaces §9 is explicit) |
| 2 | Existing 288 tests target legacy modules that Stage A replaces | A2–A9 | Keep passing until the owning scenario is verified against new code, then migrate the test |
