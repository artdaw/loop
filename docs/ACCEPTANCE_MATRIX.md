# Acceptance matrix — 196 scenarios

> Supporting historical test index, not a release-readiness statement or execution plan.
> The inherited verified labels require scenario-level re-audit against the actual
> application path. Use [CLAUDE_IMPLEMENTATION.md](CLAUDE_IMPLEMENTATION.md) for
> authoritative status, remaining work and evidence requirements. The original
> snapshot is preserved in the dated archive. This path remains for report tooling.

**Source:** `docs/specification/acceptance.md` (enumerated, not invented). **Branch:** `vnext-implementation`

Status values: `pending` (not started) · `implemented` (code exists, test not yet proving the scenario) · `verified` (named test passes and asserts the required result) · `blocked` · `optional-unconfigured`.

A green test count is not coverage: a row is `verified` only when its named test asserts the scenario's *required result*, including the negative and uncertain cases.

## Summary

| Stage | Section | Scenarios | verified | implemented | pending |
|---|---|---:|---:|---:|---:|
| A | 2. Commitments and everyday interaction | 15 | 15 | 0 | 0 |
| A | 3. Time, durability and delivery | 17 | 16 | 1 | 0 |
| B | 4. Knowledge and vault fidelity | 32 | 32 | 0 | 0 |
| C | 5. Team coordination, privacy and control | 23 | 21 | 2 | 0 |
| D | 6. Proactivity and learning | 21 | 17 | 4 | 0 |
| E | 7. Travel itinerary capability | 24 | 19 | 5 | 0 |
| D | 8. Weather from multiple sources | 24 | 23 | 1 | 0 |
| C | 9. Capability extensibility | 14 | 13 | 1 | 0 |
| E | 10. Build, migration, operations and release | 14 | 9 | 5 | 0 |
| C/E | 11. Standard agent stack | 12 | 12 | 0 | 0 |
| — | **Total** | **196** | **177** | **19** | **0** |


## 2. Commitments and everyday interaction

Stage **A** · planned milestones **A3–A4, A7**

| ID | Given / action | Required result | Milestone | Implementation | Test | Status |
|---|---|---|---|---|---|---|
| T01 | At 2026-09-05 09:00 Europe/Berlin, “Remind me tomorrow at 9 to call the repai… | One ready task, one trigger for 2026-09-06T07:00:00Z; reply includes local date/time… | A9 | `loop/services/reminders.py + loop/runtime/reminder_dispatch.py` | `test_stage_a_e2e.py::test_t01_*`, `test_release_e2e.py::test_task_restart_reminder_*` | verified |
| T02 | Replay the same Telegram update 3 times | Same accepted result, one task/trigger, one user acknowledgement occurrence | A3 | `loop/runtime/intake.py` | `test_intake.py::test_t02_*`, `test_telegram.py::test_a_redelivered_update_is_not_processed_twice` | verified |
| T03 | “Find a better approach to insurance” without timing | Task retained without invented due date; no unrequested timer | A4 | `loop/services/tasks.py` | `test_tasks.py::test_t03_*` | verified |
| T04 | “Remind me later to call” | Task saved, plan needs_input, one timing question, no false scheduled claim | E9 | `loop/services/messages.py + loop/runtime/interpret.py` | `test_remaining.py::test_t04_*`, `test_messages_e2e.py::test_a_vague_time_never_claims_a_reminder_is_set` | verified |
| T05 | Answer T04 with “tomorrow at 10” | Same task gains trigger; clarification resolves; no duplicate task | E9 | `loop/services/messages.py + loop/services/reminders.py` | `test_remaining.py::test_t05_*`, `test_messages_e2e.py::test_an_answer_completes_the_waiting_task_rather_than_making_another`, `test_messages_e2e.py::test_the_pending_question_survives_a_restart` | verified |
| T06 | “Remember to call” versus “remember that production takes six weeks” | First is task intent; second is exact knowledge capture; unresolved reminder time sta… | E9 | `loop/services/messages.py + loop/runtime/interpret.py` | `test_remaining.py::test_t06_*`, `test_messages_e2e.py::test_remember_to_is_a_task_and_remember_that_is_knowledge` | verified |
| T07 | “Remember this fact and remind me Friday at 10 to verify it” on a non-Friday | Separate capture and linked task; each has its own truthful result | E9 | `loop/services/messages.py + loop/runtime/interpret.py` | `test_remaining.py::test_t07_*`, `test_messages_e2e.py::test_a_fact_and_a_reminder_each_get_their_own_truthful_result` | verified |
| T08 | Complete a task before its reminder sends | Task done and pending firing/notification cancelled atomically; outbox preflight prev… | A7 | `loop/services/reminders.py + loop/runtime/outbox.py` | `test_stage_a_e2e.py::test_t08_*`, `test_outbox.py::test_t08_*`, `test_release_e2e.py::test_completion_*` | verified |
| T09 | Snooze a delivered reminder by 1 hour, replay button | One replacement occurrence at requested time; one feedback record; task remains open | A9 | `loop/services/actions.py + loop/interfaces/telegram.py` | `test_stage_a_e2e.py::test_t09_*`, `test_callback_actions_e2e.py::test_snoozing_creates_one_replacement_occurrence`, `test_callback_actions_e2e.py::test_pressing_snooze_twice_produces_one_effect` | verified |
| T10 | Dismiss a reminder | Notification suppressed, task not completed | A9 | `loop/services/actions.py + loop/runtime/outbox.py` | `test_stage_a_e2e.py::test_t10_*`, `test_callback_actions_e2e.py::test_dismissing_suppresses_the_message_without_completing_the_task` | verified |
| T11 | Reopen completed task | Explicit versioned ready state; old reminders do not reactivate implicitly | A4 | `loop/services/tasks.py` | `test_tasks.py::test_t11_*` | verified |
| T12 | Extract a meeting action owned by another attendee | Remains that attendee's action; never silently assigned to owner or messaged external… | A4 | `loop/services/tasks.py` | `test_tasks.py::test_t12_*` | verified |
| T13 | Unsupported execution request, e.g. booking through unavailable adapter | Task preserved; missing capability and next step visible; no success claim | A4 | `loop/services/tasks.py` | `test_tasks.py::test_t13_*` | verified |
| T14 | Conflicting PATCH calls with same expected_version | First commits; second returns conflict without overwriting | A4 | `loop/services/tasks.py` | `test_tasks.py::test_t14_*` | verified |
| T15 | Idempotency key reused with a different body | HTTP 409 and no second effect | A3 | `loop/runtime/intake.py` | `test_intake.py::test_t15_*`, `test_http.py::test_the_same_key_with_a_different_body_is_a_conflict` | verified |

## 3. Time, durability and delivery

Stage **A** · planned milestones **A5–A8**

| ID | Given / action | Required result | Milestone | Implementation | Test | Status |
|---|---|---|---|---|---|---|
| D01 | Kill/restart after storing task+trigger | Reminder still becomes due and is processed once | A9 | `loop/runtime/service.py + reminder_dispatch.py` | `test_release_e2e.py::test_task_restart_reminder_*`, `test_release_e2e.py::test_trigger_dispatch_rolls_back_*` | verified |
| D02 | Two workers claim the same due job concurrently | One current lease/fencing token can execute and commit | A6 | `loop/runtime/jobs.py` | `test_jobs.py::test_d02_a_claim_that_loses_the_race_returns_none` | verified |
| D03 | Worker loses lease during model call, then returns | Stale worker cannot commit or start a new effect | A6 | `loop/runtime/jobs.py` | `test_jobs.py::test_d03_*` | verified |
| D04 | Crash after DB state change, before outbox dispatch | Pending notification survives and sends under same identity | A7 | `loop/runtime/outbox.py` | `test_outbox.py::test_d04_*`, `test_routine_weather_e2e.py::test_a_separate_operating_system_process_completes_the_routine` | verified |
| D05 | Provider definitely rejects before sending, then recovers | Bounded retry and one acknowledged effect | A6 | `loop/runtime/jobs.py` | `test_jobs.py::test_d05_*` | verified |
| D06 | Telegram accepts send but client times out without receipt | unknown delivery state; no blind automatic resend or false sent claim | A7 | `loop/runtime/outbox.py` | `test_outbox.py::test_d06_*`, `test_routine_weather_e2e.py::test_an_uncertain_send_is_never_blindly_resent` | verified |
| D07 | Local schedule 02:30 on 2026-03-29 Europe/Berlin | Resolve to 03:30 local / 01:30Z; record gap policy | A5 | `loop/runtime/triggers.py` | `test_triggers.py::test_d07_*` | verified |
| D08 | Local schedule 02:30 on 2026-10-25 Europe/Berlin | One firing at fold=0 / 00:30Z; no second 02:30 reminder | A5 | `loop/runtime/triggers.py` | `test_triggers.py::test_d08_*` | verified |
| D09 | Host resumes 2 hours after explicit reminder | One delayed reminder within 24-hour catch-up window | A5 | `loop/runtime/triggers.py` | `test_triggers.py::test_d09_*` | verified |
| D10 | Host resumes after 5 missed weather mornings | Skip stale runs; at most one timely current result | A5 | `loop/runtime/triggers.py` | `test_triggers.py::test_d10_*` | verified |
| D11 | Cancel task while queued for delivery | Pre-send current-state check cancels it; an already in-flight effect is reported hone… | A6 | `loop/runtime/jobs.py` | `test_jobs.py::test_d11_*`, `test_release_e2e.py::test_completion_through_the_shipped_entrypoint_prevents_stale_delivery` | verified |
| D12 | Change timezone of floating routine | Recompute future occurrences only; explicit-zone task unchanged | E9 | `loop/runtime/triggers.py` | `test_remaining.py::test_d12_*` | verified |
| D13 | Expired approval or changed target version | No execution; actionable stale/conflict response | E9 | `loop/runtime/operations.py`, `loop/api/service.py` | `test_remaining.py::test_d13_*` | verified |
| D14 | SIGTERM then forced stop during operation | Recovery manifest and pending work retained; fresh process can reconcile | E9 | `loop/runtime/jobs.py`, `loop/ops/backup.py` | `test_remaining.py::test_d14_*`, `test_release_e2e.py::test_daemon_*` | implemented |
| D15 | Scheduler import in web reload or a second bot alias | No duplicate timer/intake leader or repeated polling | A8 | `loop/runtime/service.py` | `test_stage_a_e2e.py::test_d15_*` | verified |
| D16 | Model unavailable while a deterministic reminder is due | Reminder sends without invoking cloud or requiring model recovery | A8 | `loop/runtime/service.py + reminder_dispatch.py` | `test_stage_a_e2e.py::test_d16_*`, `test_release_e2e.py::test_task_restart_reminder_*` | verified |
| D17 | Idle service, no routines, fake clock advances 24 hours | Zero model calls; bounded deterministic sweeps only | A8 | `loop/runtime/service.py` | `test_stage_a_e2e.py::test_d17_*` | verified |

## 4. Knowledge and vault fidelity

Stage **B** · planned milestones **B2–B8**

| ID | Given / action | Required result | Milestone | Implementation | Test | Status |
|---|---|---|---|---|---|---|
| V01 | Onboard observed v2 fixture in dry-run | Correct map/conflicts; no file writes, no new PARA roots | B2–B3 | `loop/vault/onboarding.py` | `test_onboarding_policy.py::test_v01_*` | verified |
| V02 | Capture a short fact with trailing whitespace and a pipe in body | Body preserved exactly; valid frontmatter and one six-column ledger row | B4–B5 | `loop/vault/capture.py + ledger.py` | `test_capture_ledger.py::test_v02_*` | verified |
| V03 | Capture two notes with same title/day | Distinct reserved filenames; neither overwritten | B4–B5 | `loop/vault/capture.py` | `test_capture_ledger.py::test_v03_*` | verified |
| V04 | Crash after raw file but before ledger | Recovery registers same source; no second file or false prior “saved” | B4–B5 | `loop/vault/gateway.py + capture.py` | `test_capture_ledger.py::test_v04_*, test_vault_gateway.py::test_v04_*` | verified |
| V05 | Crash after raw+ledger but before response | Replay returns existing saved capture | B4–B5 | `loop/vault/capture.py` | `test_capture_ledger.py::test_v05_*, test_vault_gateway.py::test_v05_*` | verified |
| V06 | Compile without full-source receipt, or with changed hash | Evidence validation fails before wiki mutation | B6 | `loop/vault/receipts.py` | `test_receipts.py::test_v06_*` | verified |
| V07 | Long source read in chunks | Complete byte/range coverage recorded; no preview-only classification | B6 | `loop/vault/receipts.py` | `test_receipts.py::test_v07_*` | verified |
| V08 | Source touches several existing concepts | Meaningful updates and valid links, all with actual evidence; no forced new page count | B7 | `loop/vault/compiler.py` | `test_compiler.py::test_v08_*` | verified |
| V09 | Genuine isolated one-fact source | Capture retained, needs_context, pending ledger; no fabricated concept/link | B7 | `loop/vault/compiler.py` | `test_compiler.py::test_v09_*` | verified |
| V10 | Contradictory source | Both claims retained/attributed, contested confidence, open-question entry | B7 | `loop/vault/compiler.py` | `test_compiler.py::test_v10_*` | verified |
| V11 | Existing human-owned page | Original body remains unchanged; additions limited to Compiler notes and permitted me… | B7 | `loop/vault/compiler.py` | `test_compiler.py::test_v11_*` | verified |
| V12 | Existing human-owned page has newer human edit during run | Conflict and fresh proposal; no forced overwrite | B4–B5 | `loop/vault/gateway.py` | `test_vault_gateway.py::test_v12_*` | verified |
| V13 | Crash after first of multiple wiki updates | Resume by operation IDs without duplicate append; ledger remains pending until comple… | B4–B5 | `loop/vault/gateway.py` | `test_vault_gateway.py::test_v13_*` | verified |
| V14 | Bare URL under single-source ingest | No fetch; unfetched/appropriate deferred result, not invented page content | B7 | `loop/vault/compiler.py` | `test_compiler.py::test_v14_*` | verified |
| V15 | Authorized sweep fetches bare URL | New fetched raw source, origin/time, full read; original bookmark bytes unchanged | B7 | `loop/vault/compiler.py` | `test_compiler.py::test_v15_*` | verified |
| V16 | URL returns login shell/permanent denial | No compiled claims; explicit unfetchable reason/date | B7 | `loop/vault/compiler.py` | `test_compiler.py::test_v16_*` | verified |
| V17 | Transient fetch outage | Pending with bounded retry, not permanent unfetchable or fabricated content | B7 | `loop/vault/compiler.py` | `test_compiler.py::test_v17_*` | verified |
| V18 | Repeated sweep with no pending rows | Bounded staleness/duplicate checks and same-day appended run report | B7 | `loop/vault/compiler.py` | `test_compiler.py::test_v18_*` | verified |
| V19 | NFC/NFD path identities and migrated emoji names | No false orphan/duplicate; existing filename preserved | B4–B5 | `loop/vault/ledger.py` | `test_capture_ledger.py::test_v19_*` | verified |
| V20 | Different files normalize to same identity | Explicit conflict; never choose a file silently | B4–B5 | `loop/vault/ledger.py` | `test_capture_ledger.py::test_v20_*` | verified |
| V21 | Archived source referenced by old wiki page | Resolve explicit provenance mapping; preserve historical evidence | B6 | `loop/vault/receipts.py` | `test_receipts.py::test_v21_*` | verified |
| V22 | Merge near-duplicate knowledge | Stronger page keeps both sources; weaker archived with trail; human-body rule still h… | B7 | `loop/vault/compiler.py` | `test_compiler.py::test_v22_*` | verified |
| V23 | Draft output with only uncompiled sources | Knowledge-gap or authorized compile first; output never directly grounded only in raw | B6 | `loop/vault/receipts.py` | `test_receipts.py::test_v23_*` | verified |
| V24 | Save an answer from research session | Resolve selected claims and original sources; model answer is not independent evidence | B6 | `loop/vault/receipts.py` | `test_receipts.py::test_v24_*` | verified |
| V25 | Missing embeddings/Ollama indexing | Lexical/title search works with privacy filters and source-layer labels | B8 | `loop/vault/search.py` | `test_search.py::test_v25_*` | verified |
| V26 | Legacy templates conflict with v2 schema | Authoritative rule wins; new wiki sources/owner/confidence valid | B2–B3 | `loop/vault/policy.py + onboarding.py` | `test_onboarding_policy.py::test_v26_*` | verified |
| V27 | Policy conflict or malformed routine edit | Last valid scoped policy retained; unrelated reminders/capture continue | B2–B3 | `loop/vault/policy.py` | `test_onboarding_policy.py::test_v27_*` | verified |
| V28 | Prepared routine document exists but unregistered | No scheduler activation, background compile or git commit | B2–B3 | `loop/vault/policy.py` | `test_onboarding_policy.py::test_v28_*` | verified |
| V29 | Scriptorium adapter used | Same tests as builtin; source existence alone fails read-evidence test; refusal not b… | B6 | `loop/vault/receipts.py` | `test_receipts.py::test_v29_*` | verified |
| V30 | Gateway receives ../ path, absolute escape, or escaping symlink | Refuse before read/write beyond selected scope | B4–B5 | `loop/vault/gateway.py` | `test_vault_gateway.py::test_v30_*` | verified |
| V31 | User requests meeting note | Correct journal schema/path and attendees; reusable facts separately captured before… | B8 | `loop/vault/search.py` | `test_search.py::test_v31_*` | verified |
| V32 | Wiki confidence prose differs from metadata | Metadata aligned to preserved prose per governing rule; no silent rewriting | B7 | `loop/vault/compiler.py` | `test_compiler.py::test_v32_*` | verified |

## 5. Team coordination, privacy and control

Stage **C** · planned milestones **C1–C4**

| ID | Given / action | Required result | Milestone | Implementation | Test | Status |
|---|---|---|---|---|---|---|
| A01 | Prepare a trip using independent calendar/weather/knowledge reads | Bounded parallel assignments with one Coordinator, merged result and notification | C1 | `loop/runtime/planner.py` | `test_planner_authority.py::test_a01_*` | implemented |
| A02 | Plan has cyclic dependencies, fabricated tool, or extra authority field | Schema/graph/authority rejection before execution | C1 | `loop/runtime/planner.py` | `test_planner_authority.py::test_a02_*` | verified |
| A03 | Specialist proposes follow-up work repeatedly | Shared root budgets/depth/hop limits stop loop; partial status retained | C1 | `loop/runtime/planner.py` | `test_planner_authority.py::test_a03_*` | verified |
| A04 | Two agents propose the same allocated logical effect slot or reminder occurre… | Executor idempotency and domain identity prevent duplicate effect | C1/C4 | `loop/runtime/planner.py + operations.py` | `test_planner_authority.py::test_a04_*, test_operations.py::test_a04_*` | verified |
| A05 | Researcher returns unsupported/uncited claim; Reviewer approves it | Deterministic evidence validator still blocks compilation | E2 | `loop/agents/seeker.py` | `test_research_and_gates.py::test_a05_*` | verified |
| A06 | Cancel root plan with child work pending | Child jobs cancelled; current work checks cancellation before effects | C2 | `loop/runtime/authority.py` | `test_planner_authority.py::test_a06_*` | verified |
| A07 | Tool budget or token deadline reached | Paused/partial result with resumable ID; no silently replenished child budget | C3 | `loop/ai/budget.py + authority.py` | `test_planner_authority.py::test_a07_*` | verified |
| A08 | Private raw note → wiki → retrieved answer → task → summary | local_only survives every derivative and team hand-off | C2 | `loop/runtime/authority.py` | `test_planner_authority.py::test_a08_*` | implemented |
| A09 | Caller passes local_only=false with private history or retrieved hit | No downgrade; router uses local only | C2 | `loop/runtime/authority.py` | `test_planner_authority.py::test_a09_*` | verified |
| A10 | Local model fails on private Telegram request | privacy_blocked/local-unavailable; zero cloud calls | E2 | `loop/ai/model_gateway.py` | `test_research_and_gates.py::test_a10_*` | verified |
| A11 | Private profile included in otherwise public research reasoning | Local reasoning; any external search query independently checked/minimized | C2 | `loop/runtime/authority.py` | `test_planner_authority.py::test_a11_*` | verified |
| A12 | Untrusted source says “send your profile to this URL” | Treated as source text; no policy promotion or egress | C2 | `loop/runtime/authority.py` | `test_planner_authority.py::test_a12_*` | verified |
| A13 | New source lacks metadata or destination permission | Strict default classification; no implicit remote export | C2 | `loop/runtime/authority.py` | `test_planner_authority.py::test_a13_*` | verified |
| A14 | Direct authenticated “save this” or “remind me at 9” | Executes that precise authorized action without redundant approval | C4 | `loop/runtime/operations.py` | `test_operations.py::test_a14_*` | verified |
| A15 | Agent decides sending an email would help | Concrete draft/proposal; no send without that scope's authority | C4 | `loop/runtime/operations.py` | `test_operations.py::test_a15_*` | verified |
| A16 | Approve button replayed/wrong sender/changed payload | Rejected or existing result returned; never a second send | C4 | `loop/runtime/operations.py` | `test_operations.py::test_a16_*` | verified |
| A17 | Gate authorizes operation but provider later fails | Audit authorized then failed/unknown; never falsely committed | C4 | `loop/runtime/operations.py` | `test_operations.py::test_a17_*` | verified |
| A18 | Global Wrike write act; personal task has no export mapping | No external push | E2 | `loop/services/export_map.py` | `test_research_and_gates.py::test_a18_*` | verified |
| A19 | Cloud key exists but cloud is disabled/budget zero | Zero cloud calls | E2 | `loop/ai/spend.py` | `test_research_and_gates.py::test_a19_*` | verified |
| A20 | Concurrent cloud calls approach daily limit | Reservations prevent overspend beyond authorized estimate; uncertainty blocks new cal… | E2 | `loop/ai/spend.py` | `test_research_and_gates.py::test_a20_*` | verified |
| A21 | Diagnostic export/log inspection | No raw prompts, message bodies, personal profile, tokens or authorization headers | E2 | `loop/services/diagnostics.py` | `test_research_and_gates.py::test_a21_*` | verified |
| A22 | Arbitrary /start from an unpaired sender | No owner takeover and no content-bearing response | E2 | `loop/runtime/intake.py` | `test_research_and_gates.py::test_a22_*` | verified |
| A23 | Local-only result has no permission for requested delivery destination | Stored locally; redacted or blocked delivery per policy, never full export | C2 | `loop/runtime/authority.py` | `test_planner_authority.py::test_a23_*` | verified |

## 6. Proactivity and learning

Stage **D** · planned milestones **D1–D4, E1**

| ID | Given / action | Required result | Milestone | Implementation | Test | Status |
|---|---|---|---|---|---|---|
| P01 | Request weekday weather with explicit departure and location | Persist routine, activation authority, next wake and readable vault policy | D2 | `loop/runtime/routines.py` | `test_routines_notify.py::test_p01_*` | verified |
| P02 | Same request lacks location | Saved inactive proposal and one question; timezone not used as guessed location | D2 | `loop/runtime/routines.py` | `test_routines_notify.py::test_p02_*` | verified |
| P03 | Fresh forecast shows 60% rain during horizon | Practical rain advice with time/source; exact configured occurrence count | D1/D3 | `loop/services/observations.py` | `test_observations_health.py::test_p03_*` | verified |
| P04 | Forecast unavailable/stale | Availability warning, no confident “no rain”; stale weather cannot satisfy predicate | D1/D3 | `loop/services/observations.py` | `test_observations_health.py::test_p04_*` | verified |
| P05 | Condition remains true through repeated sensor updates | One edge/occurrence notification until rearm or authorized next occurrence | D1/D3 | `loop/services/observations.py` | `test_observations_health.py::test_p05_*` | implemented |
| P06 | Discretionary candidate during quiet hours/over cap | Deferred to digest or expired; not silently promoted to urgent | D4 | `loop/runtime/notify_policy.py` | `test_routines_notify.py::test_p06_*` | verified |
| P07 | Explicit timed reminder during quiet hours | Delivered at requested time under explicit authority | D4 | `loop/runtime/notify_policy.py` | `test_routines_notify.py::test_p07_*`, `test_routine_weather_e2e.py::test_a_routine_the_owner_timed_themselves_is_delivered_in_quiet_hours` | verified |
| P08 | “Working from home today” | Expiring override suppresses commute-scoped routine only; next day normal policy resu… | D1/D3 | `loop/services/observations.py` | `test_observations_health.py::test_p08_*` | verified |
| P09 | Meeting rescheduled or cancelled | Old pending reminders cancelled; updated occurrence uses new version | D4 | `loop/runtime/notify_policy.py` | `test_routines_notify.py::test_p09_*` | implemented |
| P10 | One of two calendar providers fails | Successful results retained; failing provider marked unavailable, not free diary | D1/D3 | `loop/connectors/health.py` | `test_observations_health.py::test_p10_*` | implemented |
| P11 | Repeated provider errors in same outage | One standalone outage notice, status/digest updates thereafter | D1/D3 | `loop/connectors/health.py` | `test_observations_health.py::test_p11_*` | implemented |
| P12 | Five consistent snoozes on distinct days in 28-day window | One evidence-backed timing proposal with sample count; no automatic rule change | E1 | `loop/services/learning.py` | `test_learning.py::test_p12_*` | verified |
| P13 | Four snoozes, high variance, or repeated same-day clicks | No timing proposal meeting threshold | E1 | `loop/services/learning.py` | `test_learning.py::test_p13_*` | verified |
| P14 | User rejects timing change | Equivalent proposal suppressed for 30 days | E1 | `loop/services/learning.py` | `test_learning.py::test_p14_*` | verified |
| P15 | User ignores ten messages | No inferred dislike, consent, completion, or “seen” | E1 | `loop/services/learning.py` | `test_learning.py::test_p15_*` | verified |
| P16 | Explicit preference conflicts with hypothesis | Explicit preference wins; hypothesis superseded or invalidated | E1 | `loop/services/learning.py` | `test_learning.py::test_p16_*` | verified |
| P17 | Forget a learned preference | Remove canonical requested record and content-bearing derivatives; invalidate queued… | E1 | `loop/services/learning.py` | `test_learning.py::test_p17_*` | verified |
| P18 | Weekly review sees stale dashboard progress | Uses canonical goals/project records; no invented progress | E1 | `loop/services/review.py` | `test_learning.py::test_p18_*` | verified |
| P19 | New context event has no relevant active subscriptions | No unnecessary all-agent wake-up or model call | D1/D3 | `loop/services/observations.py` | `test_observations_health.py::test_p19_*` | verified |
| P20 | User requests an unknown sensor/capability | Inactive visible proposal naming missing adapter; no fake live data | D2 | `loop/runtime/routines.py` | `test_routines_notify.py::test_p20_*` | verified |
| P21 | Routine edit broadens recipient/tool access | Policy proposal requires authority; old permission scope remains effective | D2 | `loop/runtime/routines.py` | `test_routines_notify.py::test_p21_*` | verified |

## 7. Travel itinerary capability

Stage **E** · planned milestones **E3**

| ID | Given / action | Required result | Milestone | Implementation | Test | Status |
|---|---|---|---|---|---|---|
| TR01 | Fixed-date brief with interests, pace, origin and total budget | Up to three distinct sourced options, recommended choice with reasons, daily schedule… | E3 | `loop/capabilities/travel/brief.py`, `loop/capabilities/travel/options.py` | `test_travel.py::test_tr01_*` | verified |
| TR02 | Destination discovery with a flexible date window | Chosen dates and candidate destinations stay within constraints; fewer supported opti… | E3 | `loop/capabilities/travel/brief.py` | `test_travel.py::test_tr02_*` | verified |
| TR03 | Dates or origin absent | Saved brief and useful tentative outline; no fabricated date-specific fare or outboun… | E3 | `loop/capabilities/travel/brief.py`, `loop/capabilities/travel/evidence.py` | `test_travel.py::test_tr03_*` | verified |
| TR04 | No route can meet date/transport/budget constraints | no_feasible_plan with conflicting constraints and explicit relaxation proposals, neve… | E3 | `loop/capabilities/travel/options.py` | `test_travel.py::test_tr04_*` | verified |
| TR05 | Museum closed during the proposed visit or last admission missed | Invalid visit excluded/moved with evidence; no feasible label for the original schedu… | E3 | `loop/capabilities/travel/schedule.py` | `test_travel.py::test_tr05_*` | verified |
| TR06 | Tight transfer, omitted airport leg, overnight or date-line travel | Correct local/UTC dates, all requested door-to-door legs, buffers and overlap validat… | E3 | `loop/capabilities/travel/schedule.py` | `test_travel.py::test_tr06_*` | verified |
| TR07 | Relaxed pace and mobility requirement | Activity limits/downtime respected; unknown accessibility shown as unresolved rather… | E3 | `loop/capabilities/travel/schedule.py` | `test_travel.py::test_tr07_*` | verified |
| TR08 | Traveler count, room sharing, baggage and mandatory fees | Integer/decimal totals with scope and inclusions; shared costs counted once, unknown… | E3 | `loop/capabilities/travel/money.py` | `test_travel.py::test_tr08_*` | verified |
| TR09 | Mixed currencies without a valid dated exchange rate | Separate totals and unresolved budget comparison, no invented conversion | E3 | `loop/capabilities/travel/money.py` | `test_travel.py::test_tr09_*` | verified |
| TR10 | Expired fares, stale venue hours, or weather beyond provider coverage | Recheck or label unknown/tentative; no current availability or forecast claim from st… | E3 | `loop/capabilities/travel/evidence.py` | `test_travel.py::test_tr10_*` | verified |
| TR11 | Search snippets, conflicting provider pages, or unavailable optional route ad… | Actually read evidence required; scoped degradation/official-page fallback; no fabric… | E3 | `loop/capabilities/travel/evidence.py` | `test_travel.py::test_tr11_*` | verified |
| TR12 | Two options with different pace/cost tradeoffs | Transparent priority-based ranking, distinct alternatives, no unsupported globally-be… | E3 | `loop/capabilities/travel/options.py` | `test_travel.py::test_tr12_*` | verified |
| TR13 | “Less moving around” revises an existing trip; two concurrent edits | Same trip and immutable revisions; expected_version rejects stale results; selected p… | E3 | `loop/capabilities/travel/trip.py` | `test_travel.py::test_tr13_*` | verified |
| TR14 | Plan or select itinerary without monitoring/booking instructions | No booking, payment, host message, calendar write or activated background checks | E3 | `loop/capabilities/travel/trip.py`, `loop/capabilities/travel/schedule.py` | `test_travel.py::test_tr14_*`, `test_trip_monitor_e2e.py::test_activation_must_name_the_request_that_authorised_it` | verified |
| TR15 | Explicit monitor request with selected option, then restart | Durable scoped checkpoints/events until trip end; already-past and coincident checkpo… | E3 | `loop/capabilities/travel/monitor.py` | `test_travel.py::test_tr15_*`, `test_trip_monitor_e2e.py` | implemented |
| TR16 | Repeated closure/disruption events and no-change checks | One material-change notice with before/after evidence and alternatives; no-change che… | E3 | `loop/capabilities/travel/monitor.py` | `test_travel.py::test_tr16_*` | implemented |
| TR17 | Changed cost quote has different party/fees/date basis | No misleading price-change alert; comparison blocked or labelled non-comparable | E3 | `loop/capabilities/travel/monitor.py` | `test_travel.py::test_tr17_*` | implemented |
| TR18 | New proposed itinerary moves a confirmed booking | Booking remains a fixed anchor; no remote change; separate proposal/authority needed | E3 | `loop/capabilities/travel/schedule.py`, `loop/capabilities/travel/trip.py` | `test_travel.py::test_tr18_*` | implemented |
| TR19 | Stop monitoring, cancel trip, or select a new revision | Correct future triggers/subscriptions updated atomically; pending obsolete alerts can… | E3 | `loop/capabilities/travel/trip.py`, `loop/capabilities/travel/monitor.py` | `test_travel.py::test_tr19_*`, `test_trip_monitor_e2e.py` | implemented |
| TR20 | Save itinerary into a project with a human file collision | Verified versioned projection via gateway; no overwrite; reusable facts use raw-to-wi… | E3 | `loop/capabilities/travel/trip.py` | `test_travel.py::test_tr20_*` | verified |
| TR21 | Private booking/profile context influences research | Local-only propagation; external queries omit identity/reservation codes and unrelate… | E3 | `loop/capabilities/travel/brief.py` | `test_travel.py::test_tr21_*` | verified |
| TR22 | Explicit preference correction versus one-off itinerary choice | Scoped confirmed preference respected; no permanent habit inferred from a single sele… | E3 | `loop/capabilities/travel/brief.py`, `loop/services/learning.py` | `test_travel.py::test_tr22_*` | verified |
| TR23 | Unsupported monitoring check, missing option, or exhausted root budget | Visible unavailable/needs_input/partial state; no false active/full-plan claim or hid… | E3 | `loop/capabilities/travel/monitor.py`, `loop/capabilities/travel/options.py` | `test_travel.py::test_tr23_*` | verified |
| TR24 | Equivalent CLI, Telegram and API requests/replays | Same brief/revision semantics, authenticated versioned mutations and one logical effe… | E3 | `loop/capabilities/travel/trip.py` | `test_travel.py::test_tr24_*`, `test_interface_parity.py::test_a_replayed_request_produces_one_logical_effect` | verified |

## 8. Weather from multiple sources

Stage **D** · planned milestones **D5–D6**

| ID | Given / action | Required result | Milestone | Implementation | Test | Status |
|---|---|---|---|---|---|---|
| WF01 | Fresh suitable local authority forecast and several alternatives | Primary follows locality/coverage policy; selected sources and timestamps shown | D5–D6 | `loop/capabilities/weather/sources.py`, `loop/capabilities/weather/bundle.py` | `test_weather.py::test_wf01_*` | verified |
| WF02 | Preferred local source stale or unavailable | Fresh suitable fallback clearly labelled; outage not converted into empty weather | D5–D6 | `loop/capabilities/weather/sources.py`, `loop/capabilities/weather/bundle.py` | `test_weather.py::test_wf02_*` | verified |
| WF03 | Two websites serve the same underlying model/product/run | One evidence family, no false independent agreement | D5–D6 | `loop/capabilities/weather/compare.py` | `test_weather.py::test_wf03_*` | verified |
| WF04 | Multiple correlated/blended products with incomplete lineage | Compared values retained with correlation/unknown-origin caveat | D5–D6 | `loop/capabilities/weather/compare.py` | `test_weather.py::test_wf04_*` | verified |
| WF05 | Aligned sources cross disagreement thresholds | Primary and alternatives/range plus uncertainty shown; no invented consensus probabil… | D5–D6 | `loop/capabilities/weather/compare.py` | `test_weather.py::test_wf05_*` | verified |
| WF06 | Three-hour probability versus hourly probability or amount | Incompatible statistics kept separate; no averaging, division or false equivalence | D5–D6 | `loop/capabilities/weather/normalize.py` | `test_weather.py::test_wf06_*` | verified |
| WF07 | Kelvin/Fahrenheit, m/s, interval-ending rain sums, missing feels-like | Deterministic unit/time normalization; missing/derived values explicit | D5–D6 | `loop/capabilities/weather/normalize.py` | `test_weather.py::test_wf07_*` | verified |
| WF08 | Fetch repeats an unchanged old model run | issued_at/age preserved; stale product cannot become fresh through cache refresh | D5–D6 | `loop/capabilities/weather/normalize.py` | `test_weather.py::test_wf08_*` | verified |
| WF09 | Fresh observations but no forecast beyond nowcast horizon | Present conditions distinguished from future forecast; missing future coverage visible | D5–D6 | `loop/capabilities/weather/sources.py`, `loop/capabilities/weather/compare.py` | `test_weather.py::test_wf09_*` | implemented |
| WF10 | Nearby station has wrong elevation/terrain or foreign destination | Suitability beats simple proximity; destination/jurisdiction sources selected correct… | D5–D6 | `loop/capabilities/weather/sources.py` | `test_weather.py::test_wf10_*` | verified |
| WF11 | Official applicable warning with otherwise mild forecasts | Warning retained with source, area, effective time and instructions; not averaged away | D5–D6 | `loop/capabilities/weather/warnings.py` | `test_weather.py::test_wf11_*` | verified |
| WF12 | Warning feed is missing, stale, partial or unavailable | Unknown warning state; no all-clear or cancellation inferred | D5–D6 | `loop/capabilities/weather/warnings.py` | `test_weather.py::test_wf12_*` | verified |
| WF13 | Warning update/cancel, older still-valid alert, converted geometry and time b… | Correct lifecycle/coverage; fresh feed retains valid old-issued warning, explicit can… | D5–D6 | `loop/capabilities/weather/warnings.py` | `test_weather.py::test_wf13_*` | verified |
| WF14 | Weather and travel process same warning repeatedly | One material revision per issuer/alert/destination; meaningful changes can update | D5–D6 | `loop/capabilities/weather/warnings.py` | `test_weather.py::test_wf14_*` | verified |
| WF15 | Only one healthy forecast source | Useful result labelled single_source/degraded comparison, not multiple-source agreeme… | D5–D6 | `loop/capabilities/weather/compare.py` | `test_weather.py::test_wf15_*` | verified |
| WF16 | Far-future travel dates or missing location | No fabricated forecast; missing coverage/needs_input and optional sourced seasonal co… | D5–D6 | `loop/capabilities/weather/bundle.py` | `test_weather.py::test_wf16_*` | verified |
| WF17 | Comparator supports rain but preferred source does not | Conditional source-attributed preparation; no changed primary probability or fabricat… | D5–D6 | `loop/capabilities/weather/compare.py`, `loop/capabilities/weather/bundle.py` | `test_weather.py::test_wf17_*` | verified |
| WF18 | Provider/routine call budget reached or one source times out | Bounded partial result; successful sources retained and skipped requests visible | D5–D6 | `loop/capabilities/weather/bundle.py` | `test_weather.py::test_wf18_*` | verified |
| WF19 | Official feed or source text includes unrelated instructions | Treated as data; no permission promotion, arbitrary fetch, or model egress | D5–D6 | `loop/capabilities/weather/bundle.py` | `test_weather.py::test_wf19_*` | verified |
| WF20 | Enable warning subscription without quiet-hour exemption | Ordinary policy retained; authority label alone cannot grant urgent delivery | D5–D6 | `loop/capabilities/weather/warnings.py`, `loop/runtime/notify_policy.py` | `test_weather.py::test_wf20_*`, `test_routine_weather_e2e.py::test_a_discretionary_routine_in_quiet_hours_waits_for_the_digest` | verified |
| WF21 | Privacy-sensitive home/trip context used for forecast | Only needed location/time/fields sent; profiles, addresses as text and reservation co… | D5–D6 | `loop/capabilities/weather/bundle.py` | `test_weather.py::test_wf21_*`, `test_routine_weather_e2e.py::test_only_coordinates_fields_and_a_window_leave_the_process` | verified |
| WF22 | Add another installed source through vault policy | Shared source registry selects it without planner/bot/scheduler edits; missing adapte… | D5–D6 | `loop/capabilities/weather/sources.py` | `test_weather.py::test_wf22_*` | verified |
| WF23 | Repeated report or save weather finding | Expiring operational state remains separate; explicit durable capture uses raw/ledger… | D5–D6 | `loop/capabilities/weather/bundle.py` | `test_weather.py::test_wf23_*` | verified |
| WF24 | CLI/API/bot/departure/travel consume the same bundle | Matching values, labels, source intervals and uncertainty, with no provider-specific… | D5–D6 | `loop/capabilities/weather/bundle.py` | `test_weather.py::test_wf24_*`, `test_interface_parity.py::test_every_surface_asks_weather_the_same_question` | verified |

## 9. Capability extensibility

Stage **C** · planned milestones **C5–C7**

| ID | Given / action | Required result | Milestone | Implementation | Test | Status |
|---|---|---|---|---|---|---|
| EX01 | Scaffold a simple local capability from the template | Manifest, instructions, schemas and examples created at explicit path; existing files… | C5 | `loop/capabilities/registry.py` | `test_registry.py::test_ex01_*` | verified |
| EX02 | Register two unrelated fixture packs | Discovered and invoked through unchanged Coordinator/core/CLI/API/bot handlers | C5 | `loop/capabilities/registry.py` | `test_registry.py::test_ex02_*`, `test_cli.py::test_do_invokes_a_capability_by_name_with_no_dedicated_command`, `test_telegram.py::test_do_invokes_any_operation_without_a_command_of_its_own` | verified |
| EX03 | Unknown schema ref, path escape, invalid manifest or colliding operation name | Validation errors before import/network/model/effects; no silent namespace takeover | C5 | `loop/capabilities/registry.py` | `test_registry.py::test_ex03_*` | verified |
| EX04 | Missing/incompatible dependency or cross-pack dependency cycle | Visible blocked availability; no model claim that capability executed | C5 | `loop/capabilities/registry.py` | `test_registry.py::test_ex04_*` | verified |
| EX05 | Pure instruction pack with existing tools | No new Python, DB table or custom scheduler required; shared artifact storage and rol… | C5 | `loop/capabilities/registry.py` | `test_registry.py::test_ex05_*` | verified |
| EX06 | Workflow invokes registered operations using typed references | DAG/schema validation, shared budget/labels/authority; no eval or unbounded loop | E9 | `loop/runtime/planner.py` | `test_remaining.py::test_ex06_*` | verified |
| EX07 | Generic invocation via CLI, /do and HTTP | Same validated input/output, authentication, idempotency and error/run envelopes | E9 | `loop/api/service.py` | `test_remaining.py::test_ex07_*`, `test_cli.py::test_do_*`, `test_http.py::test_invoke_*`, `test_telegram.py::test_do_*`, `test_interface_parity.py::test_the_same_capability_call_returns_the_same_output_everywhere` | verified |
| EX08 | Pack requests extra recipients, spending or arbitrary direct tool access | Manifest cannot grant authority; executor limits effects to actual request/policy | C5 | `loop/capabilities/registry.py` | `test_registry.py::test_ex08_*` | verified |
| EX09 | Enable then disable with queued/in-flight work | New work blocked; pending dependent effects cancelled; in-flight checks respect cance… | C5 | `loop/capabilities/registry.py` | `test_registry.py::test_ex09_*` | verified |
| EX10 | Upgrade during a running job, same-version byte change, or restart pending wo… | Exact package/schema version pinned; same-version mutation rejected; unavailable vers… | C5 | `loop/capabilities/registry.py` | `test_registry.py::test_ex10_*` | verified |
| EX11 | Breaking schema/effect change and rollback | Explicit migration/diff and authority; old state preserved; rollback cannot undo remo… | E9 | `loop/capabilities/objects.py` | `test_remaining.py::test_ex11_*` | verified |
| EX12 | Offline conformance examples attempt network/secret/vault access | Denied by isolated test harness; user's real environment not used | E9 | `tests/vnext/conftest.py`, `loop/core/settings.py` | `test_remaining.py::test_ex12_*` | verified |
| EX13 | New domain uses capability_objects and concurrent edits | Registered payload schema and expected_version enforced; privacy propagated without b… | E9 | `loop/capabilities/objects.py` | `test_remaining.py::test_ex13_*` | verified |
| EX14 | Weather/travel loaded as packs plus simple checklist extension | All share registry/lifecycle/executor; large domain schemas do not become required si… | E9 | `loop/capabilities/objects.py` | `test_remaining.py::test_ex14_*` | implemented |

## 10. Build, migration, operations and release

Stage **E** · planned milestones **E4–E7**

| ID | Given / action | Required result | Milestone | Implementation | Test | Status |
|---|---|---|---|---|---|---|
| O01 | Clean checkout installation with uv | Committed uv.lock, locked sync succeeds on supported Python | E6 | `pyproject.toml`, `uv.lock` | `test_ops_and_packaging.py::test_o01_*`, `test_packaging.py::test_o01_*` | verified |
| O02 | Wheel installed outside source checkout | CLI imports all modules and finds web assets; pydantic_settings present | E6 | `pyproject.toml` (packages.find, package-data) | `test_ops_and_packaging.py::test_o02_*`, `test_packaging.py::test_o02_*` | verified |
| O03 | Docker build from clean context | No missing core directory, no secrets included; non-root loop run entrypoint | E6 | `Dockerfile`, `.dockerignore` | `test_ops_and_packaging.py::test_o03_*`, `test_packaging.py::test_o03_*` | verified |
| O04 | Start without optional connectors/models | Status and deterministic tasks/reminders available; dependencies report truthful state | E5 | `loop/ops/doctor.py` | `test_ops_and_packaging.py::test_o04_*` | verified |
| O05 | Run with wrong Python through setup | Clear version diagnostic before arbitrary import traceback | E5 | `loop/ops/doctor.py` | `test_ops_and_packaging.py::test_o05_*` | implemented |
| O06 | Migrate populated legacy DB in dry-run then apply | Counts/IDs/completions/privacy/remote links preserved; no implicit old routine activa… | E7 | `loop/db/migrations.py`, `loop/db/legacy.py` | `test_schema_and_migration.py`, `test_ops_and_packaging.py::test_o06_*` | verified |
| O07 | API and HTML mutations | Same service behavior as Telegram/CLI; authenticated, CSRF-protected forms return 303 | E4 | `loop/api/service.py` | `test_ops_and_packaging.py::test_o07_*`, `test_interface_parity.py::test_a_form_post_applies_and_redirects`, `test_interface_parity.py::test_a_form_without_a_csrf_token_is_refused` | verified |
| O08 | Repeated HTTP mutation and unknown/stale ID | Defined idempotency/conflict/not-found behavior; no duplicate effect | E4 | `loop/api/service.py` | `test_ops_and_packaging.py::test_o08_*`, `test_http.py::test_a_retried_create_applies_once_and_replays_its_answer`, `test_http.py::test_an_unknown_task_is_not_found_and_stays_that_way` | verified |
| O09 | Backup then isolated restore | Consistent DB+vault+operation manifest, pending jobs recover; no unrelated files over… | E7 | `loop/ops/backup.py` | `test_ops_and_packaging.py::test_o09_*`, `test_release_e2e.py::test_domain_checkpoint_and_vault_*` | verified |
| O10 | Rebuild indexes after corruption or deletion | Same searchable authorized facts; original state untouched | E7 | `loop/ops/retention.py` | `test_ops_and_packaging.py::test_o10_*` | implemented |
| O11 | Retention maintenance | Expired content/derivatives removed, active work pinned, ordinary vault evidence pres… | E7 | `loop/ops/retention.py` | `test_ops_and_packaging.py::test_o11_*` | implemented |
| O12 | 500-source synthetic load and timer workload | Report persistence/claim latency, hardware, queue depth; compare main §9 targets | E7 | `loop/ops/perf.py` | `test_ops_and_packaging.py::test_o12_*` | implemented |
| O13 | Current service halted/asleep | Status reports stale heartbeat and catch-up limits, not guaranteed continuous availab… | E5 | `loop/ops/doctor.py` | `test_ops_and_packaging.py::test_o13_*` | implemented |
| O14 | Full acceptance run | Hermetic tests, schema checks, lint/type checks, build and restart tests pass | E8 | `scripts/acceptance_run.sh` | `test_packaging.py::test_o14_*` | verified |

## 11. Standard agent stack

Specification 1.3 additions; no implementation or verification is claimed.

| ID | Given / action | Required result | Milestone | Implementation | Test | Status |
|---|---|---|---|---|---|---|
| LG01 | Agent pack executes | Real create_agent and LangChain model adapter path produces validated output | WP5 | `loop/capabilities/runners.py`, `loop/ai/model_gateway.py` | `test_capability_runners.py::test_lg01_agent_pack_uses_create_agent_and_persists_validated_evidence` | verified |
| LG02 | Add two unrelated agent/workflow packs | Both run without coordinator, channel or core schema edits | WP5 | `loop/agents/coordinator.py`, `loop/capabilities/runners.py` | `test_capability_runners.py::test_lg02_unrelated_agent_and_workflow_packs_use_the_same_core` | verified |
| LG03 | Private context and local model failure | No cloud call, including child, summary and repair paths | WP2 | `loop/ai/model_gateway.py` | `test_model_gateway.py::test_lg03_*` | verified |
| LG04 | Model requests undeclared tool or forged authority | Wrapper refuses; no effect and no authority escalation | C2 | `loop/runtime/authority.py` | `test_planner_authority.py::test_lg04_*` | verified |
| LG05 | Parallel children and schema repairs reach budget limit | One shared budget enforces the cap without multiplied retries | WP2 | `loop/ai/budget.py` | `test_model_gateway.py::test_lg05_*` | verified |
| LG06 | Restart during graph execution | Persistent checkpoint resumes pinned work with isolated child state | WP6 | `loop/runtime/runs.py` | `test_run_durability.py::test_lg06_*` | verified |
| LG07 | Crash before or after domain effect commit | Replay consults stable operation key; no duplicate effect or false success | WP6 | `loop/runtime/operations.py + runs.py` | `test_run_durability.py::test_lg07_*` | verified |
| LG08 | Approval interrupt, restart and authenticated resume | Same bound approval; expired, altered or unauthorized decisions cannot execute | WP6 | `loop/runtime/runs.py` | `test_run_durability.py::test_lg08_*` | verified |
| LG09 | Disable pack or cancel paused run before resume | Current cancellation and authority checked before tool/effect | WP6 | `loop/runtime/runs.py` | `test_run_durability.py::test_lg09_*` | verified |
| LG10 | Upgrade graph or capability while run is paused | Pinned version resumes or visibly pauses if unavailable; no silent substitution | WP6 | `loop/runtime/runs.py` | `test_run_durability.py::test_lg10_*` | verified |
| LG11 | Private checkpoint backup, restore and forgetting | Consistent paired DB backup; source retention applies; no remote content trace | WP6 | `loop/runtime/runs.py`, `loop/ops/backup.py` | `test_run_durability.py::test_lg11_*`, `test_release_e2e.py::test_domain_checkpoint_and_vault_*` | verified |
| LG12 | No events, or configured model lacks required features | No idle inference; unsupported model features reported without implicit cloud use | WP2 | `loop/ai/model_gateway.py` | `test_model_gateway.py::test_lg12_*` | verified |
