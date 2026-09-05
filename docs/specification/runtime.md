# Runtime contract: the persistent team

Status: normative target for Loop vNext. Read with [the main specification](../SPECIFICATION.md),
[the vault contract](vault.md), [interfaces](interfaces.md), and [acceptance tests](acceptance.md).
MUST and MUST NOT are requirements; SHOULD permits a documented reason to differ.

## 1. Service and team model

Loop is one persistent service for one authenticated owner. Its specialists are
logical roles with durable work assignments. A role may use an LLM, deterministic
code, or both; it is not an independent unbounded chat process.

| Role | Owns | Produces |
|---|---|---|
| Coordinator | User intent, decomposition, dependencies, final response | Work plans and assignments |
| Commitments | Tasks, reminders, follow-ups, waiting states | Task mutations and trigger proposals |
| Scribe | Exact captures and source registration | Raw capture operations |
| Compiler | Source-grounded knowledge maintenance | Validated wiki/ledger patch sets |
| Seeker / Researcher | Retrieval, questions, authorized external research | Cited findings and knowledge gaps |
| Daily-life planner | Weather, calendar context, travel itineraries, practical preparations | Compared plans, routine results and notification candidates |
| Reviewer | Outcome checks, weekly review, preference hypotheses | Validation reports and scoped proposals |

These extend the vault's Scribe, Compiler, Seeker, and Edward responsibilities;
Edward's synthesis/review work maps to Coordinator/Reviewer. Do not replace the
vault role documents or create parallel canonical memory for each role.

Registry IDs are coordinator, commitments, scribe, compiler, seeker, daily_life,
and reviewer. A deployment may use one model for all roles; role identity determines
instructions and tool scope, not a requirement for seven separate model servers.

Roles cooperate through assignments, immutable result artifacts, events, and
object references in SQLite. Private chain-of-thought is neither required nor
persisted. Store short decisions, evidence, uncertainty, tool results, and outcomes.
Agents cannot converse indefinitely or wake each other through arbitrary text.

The Coordinator owns each plan. A task has one accountable role per active step.
Specialists propose operations; the runtime executor alone performs mutations.
Reviewer checks cannot grant permissions. Failed checks return bounded repair work
to the Coordinator. Deterministic validators run even when an LLM reviewer agrees.

Parallel execution is useful for independent evidence gathering: weather and
calendar reads, or different research questions. Dependent steps await committed
results: capture → compile → draft. Serialize writes to each shared resource.
Two specialists never overwrite a shared file from separate stale snapshots.

## 2. Runtime cycle and bounded autonomy

1. Accept and deduplicate an authenticated event; commit it before acknowledging.
2. Resolve relevant policy, privacy labels, object versions, and existing assignments.
3. Apply deterministic intent handlers first for explicit commands/callbacks.
4. For free text, request a typed plan from the Coordinator with an allowlisted
   capability catalogue. Multiple intents are allowed in a single message.
5. Validate schema, references, scope, budgets, permissions, and dependencies.
6. Persist the plan/steps and schedule runnable steps in the same transaction.
7. Claim work, collect evidence, propose changes, gate and execute operations.
8. Verify actual results, commit state plus resulting events/outbox entries.
9. Reply, record the next wake-up condition, and sleep until work is due.

Always-on refers to the service, queue, subscriptions, and durable commitments.
Idle polling MUST NOT invoke a model. There is no continuous agent debate.
A periodic deterministic reconciliation sweep catches lost watcher events.

Default budgets per root request, shared across its child assignments:

| Class | Wall-clock deadline | Model calls | Total model tokens, input + output | Tool calls |
|---|---:|---:|---:|---:|
| Interactive | 120 seconds | 12 | 32,000 | 30 |
| Background routine | 90 seconds | 4 | 16,000 | 12 |
| Explicit research/compile batch | 600 seconds | 24 | 96,000 | 80 |

Default maximum active assignments: 2; concurrent local inference: 1; child
assignments per plan: 4; dependency depth: 4; causal event hops: 8. These are
configurable deployment limits, never bypassed through child runs. Large source
batches checkpoint and schedule continuation under the same user-visible job;
continuations count against an explicit total job budget (default 10 batches).
Reaching a limit yields partial results and a resumable status, not a success
claim or silent fresh budget. A user can explicitly extend a paused job.

Infrastructure retries do not create new logical actions. Semantic replanning
gets at most two attempts after the initial plan. Invalid model output gets one
schema-repair attempt within the same budget. LLM confidence is advisory; it
cannot substitute for missing date, location, connector, source, or permission.
Do not infer model quality from response length alone.

## 3. Shared types and storage conventions

Python 3.12+, typed Pydantic boundary objects, async application lifecycle.
Use UTC RFC3339 timestamps ending in Z in public JSON; persist integer UTC
microseconds in SQLite. Local dates remain ISO YYYY-MM-DD strings and MUST NOT
be coerced into UTC-midnight timestamps. Use IANA timezone names, UUID4 IDs,
UTF-8 text, versioned JSON objects, and SHA-256 content hashes.

Every mutable domain object has id, version (initially 1), created_at, updated_at.
Updates require expected_version; a mismatch is a conflict, never last-write-wins.
Every content-bearing object/artifact/event/result has a PrivacyLabel and
provenance references. Nullable means explicitly absent, not guessed.

PrivacyLabel:
- model_scope: local_only | cloud_allowed.
- origins: set of source classifications.
- allowed_destinations: set of configured destination IDs, e.g. owner:telegram.
- sensitive: boolean; controls redaction/export handling, not autonomy.
- policy_revision: ID of the classification decision.

Merge labels by local_only OR, sensitive OR, union of origins, and intersection
of allowed destinations. An empty destination set permits local storage/use only.
Unlabelled imported content defaults to local_only and no external destination.
The label applies to summaries, task titles, prompts, tool queries, caches,
embeddings, intermediate plans, errors, and feedback, not just original messages.

A capability sees the minimal fields required. Local-only means no remote model;
outbound delivery and non-model network requests have a separate destination and
field scope. Telegram is a remote transport, so “local-only” does not mean that
Telegram itself never receives messages. The owner may authorize delivery of
selected private results to their configured chat without authorizing cloud AI.

## 4. Normative logical database schema

Use SQLAlchemy 2.x with SQLite, foreign_keys=ON, WAL, synchronous=FULL, and
busy_timeout=5000 on local disk. Never hold a transaction across model/network
calls. Each row below is a required logical table; internal physical splitting
is permitted if constraints and API behavior remain equivalent.

| Table | Required fields beyond common identity/version/times | Constraints |
|---|---|---|
| events | kind, schema_version, origin, origin_id, idempotency_key, occurred_at, received_at, root_id, causation_id?, hop_count, payload_json, privacy, state | UNIQUE(origin, idempotency_key); state accepted/processing/handled/failed |
| plans | root_event_id, intent_summary, status, policy_revision, budget_json, usage_json, deadline_at | status queued/running/needs_input/awaiting_approval/paused/succeeded/partial/failed/cancelled |
| work_items | plan_id, role, objective, inputs_json, result_schema, dependencies_json, status, lease_owner?, lease_until?, fencing_token, attempt, result_artifact_id?, error_code? | status queued/running/needs_input/awaiting_approval/paused/succeeded/partial/failed/cancelled; dependency DAG validated; terminal results immutable |
| artifacts | plan_id?, kind, payload_json or content_path, content_hash, evidence_json, privacy | content immutable; paths confined to managed storage |
| capability_registry | pack_id, package_version, package_path, package_hash, manifest_json, status, enabled, registry_revision, authority_ref?, validation_errors_json | UNIQUE(pack_id, package_version); one enabled version per pack; operation names globally owned |
| capability_objects | pack_id, object_type, object_key, object_schema_version, payload_json, privacy | UNIQUE(pack_id, object_type, object_key); schema validation and expected_version updates |
| tasks | title, description, original_event_id, owner, status, priority, project_ref?, due_date?, due_at?, timezone, blocked_reason?, waiting_on?, parent_task_id?, context_json, completed_at?, cancelled_at?, privacy | status inbox/ready/in_progress/waiting/blocked/done/cancelled; priority low/normal/high; one due representation |
| trips | owner, original_event_id, title, brief_artifact_id, current_revision_id?, selected_revision_id?, selected_option_id?, status, monitoring_routine_id?, project_ref?, privacy | status planning/needs_input/ready/no_feasible_plan/active/completed/cancelled; selected option belongs to selected revision; [travel contract](capabilities/travel.md) |
| observations | key, subject_ref, value_json, evidence_json, valid_from, expires_at, confidence, privacy, supersedes? | confidence stated/observed/inferred; expired or conflicting values evaluate unknown |
| subscriptions | owner_ref, event_kind, subject_filters_json, context_keys_json, handler, cooldown_seconds, enabled, authority_ref | handler names a registered deterministic handler or bounded role; no executable text |
| triggers | subject_type, subject_id, kind, definition_json, enabled, revision, next_fire_at?, last_fire_at?, expires_at?, privacy | kind at/local_schedule/event/condition; active subject required |
| trigger_firings | trigger_id, trigger_revision, occurrence_key, nominal_at, effective_at, status, plan_id? | UNIQUE(trigger_id, trigger_revision, occurrence_key); status queued/running/succeeded/skipped/failed/cancelled |
| jobs | kind, payload_json, dedupe_key, state, run_after, deadline_at?, attempts, max_attempts, lease_owner?, lease_until?, fencing_token, last_error_code? | UNIQUE(dedupe_key); state queued/running/retry_wait/succeeded/failed/cancelled |
| routines | slug, source_path, source_hash, policy_revision, config_json, enabled, activation_event_id?, status | UNIQUE(slug); status inactive/active/paused/invalid |
| notifications | category, subject_ref, occurrence_key, destination_id, payload_json, sensitivity_mode, not_before, expires_at?, state, acknowledgement_at? | UNIQUE(category, subject_ref, occurrence_key, destination_id); state pending/deferred/sending/sent/unknown/failed/expired/cancelled |
| outbox | notification_id or operation_id, channel, payload_json, dedupe_key, state, attempts, retry_at, provider_receipt?, last_error_code? | UNIQUE(dedupe_key); state pending/sending/delivered/unknown/failed/cancelled |
| operations | action, target, input_hash, expected_version_or_hash?, policy_revision, authority_ref, status, idempotency_key, result_json?, error_code? | UNIQUE(idempotency_key); status proposed/awaiting_approval/authorized/running/committed/failed/unknown/cancelled |
| approvals | operation_id, input_hash, destination_id, expires_at, resolved_at?, resolution?, actor_id? | resolution approved/rejected; single use, bound to operation and input hash |
| captures | event_id, capture_index, source_path, original_body, body_hash, ledger_identity, status, privacy | UNIQUE(event_id, capture_index); status accepted/file_created/registered/failed; capture_index is an integer |
| vault_transactions | plan_id, operation_ids_json, manifest_json, state, staging_path, committed_files_json | state prepared/applying/committed/conflicted/failed; manifest includes expected and desired hashes |
| read_receipts | run_id, source_id, path, hash, byte_count, covered_ranges_json, read_at | evidence requires complete coverage of cited source version |
| source_refs | source_id, canonical_path, current_path, layer, hash, archived, metadata_json, privacy | UNIQUE(canonical_path after NFC); retain path mapping for archives |
| policy_revisions | scope, source_hashes_json, normalized_policy_json, status, problems_json, activated_by? | status proposed/active/invalid/superseded |
| preferences | key, value_json, scope, state, evidence_json, source_path, review_after?, supersedes? | one active explicit/confirmed value per key/scope; other fields follow vault contract |
| feedback | subject_ref, event_id, kind, value_json, occurred_at, privacy | UNIQUE(event_id, subject_ref, kind); kind accept/reject/snooze/dismiss/correct |
| connector_state | connector_id, cursor_json, last_attempt_at?, last_success_at?, last_error_code?, health | UNIQUE(connector_id); health ready/unconfigured/auth_required/degraded/offline |
| remote_links | provider, account_id, remote_id, object_type, local_id, remote_version?, sync_base_json | UNIQUE(provider, account_id, remote_id); explicit project/export scope |
| conversations | session_id, channel_id, event_id, role, body, summary_of_json?, privacy | one retained message per event/role; local-only summaries |
| audit | actor, event_id?, operation_id?, action, decision, reason_code, target_id?, hashes_json, backend?, latency_ms?, token_usage_json? | append-only metadata, never prompts/bodies/tokens/secrets |

Root privacy applies to all rows carrying content or references that expose content,
even where the table abbreviates that common field. Foreign-key references MUST
resolve; dependency/reference lists are validated transactionally. Add indexes on
jobs(state, run_after), triggers(enabled, next_fire_at), outbox(state, retry_at),
tasks(status, due_at), and observations(key, subject_ref, expires_at).

Work items and artifacts also record operation_name, operation_version, package_hash
and input/output schema hashes for extension invocations. Registry state transitions,
upgrades and generic object storage follow the [extension contract](capabilities/README.md).
Builtin provenance uses its application version; migrations preserve unknown legacy
provenance explicitly instead of inventing a package version. Registry updates
atomically validate cross-pack names/dependencies and advance registry_revision.

Observations are temporary context, not permanent wiki facts. Each key declares a
typed value and maximum freshness in the capability/policy schema. Explicit current
user statements override conflicting inferences within the same scope; conflicting
provider evidence stays unknown until reconciled. Superseding preserves provenance.
Subscriptions select only affected commitments/routines; self-produced watcher
events carry operation IDs so indexing can run without triggering compile loops.

job/work lease fields are omitted on other tables intentionally: a job owns the
execution lease; a work item references the same current fencing token. Mutations
and outbox inserts compare that token before committing. Orphan recovery MUST
invalidate the prior token. created/updated fields need not be mutable on audit,
read receipts, artifacts, feedback, or firings.

Use FTS5 tables for labelled searchable text, keyed to source/object IDs.
Vector indexes are disposable derivatives, never the authoritative task or source
store. Schema migrations are versioned, transactional where SQLite allows it,
backed up before destructive changes, and tested on a populated old database.
create_all alone is not migration.

## 5. Event, plan, and capability contracts

Minimum event envelope (illustrative IDs below; production IDs are UUID4):

```json
{
  "schema_version": 1,
  "id": "event-1",
  "kind": "message.received",
  "origin": "telegram",
  "origin_id": "update:12345",
  "idempotency_key": "update:12345",
  "occurred_at": "2026-09-05T07:00:00Z",
  "received_at": "2026-09-05T07:00:00Z",
  "root_id": "event-1",
  "causation_id": null,
  "hop_count": 0,
  "payload": {"text": "Remind me tomorrow at 9 to take an umbrella"},
  "privacy": {
    "model_scope": "local_only",
    "origins": ["telegram_private"],
    "allowed_destinations": ["owner:telegram"],
    "sensitive": true,
    "policy_revision": "privacy-v1"
  }
}
```

Core event kinds: message.received, voice.received, vault.changed,
policy.changed, task.changed, trigger.due, connector.changed, approval.resolved,
operation.committed, operation.failed, feedback.received, run.completed.
Every producer defines a stable origin key. Watchers use normalized path + content
hash, provider updates use account + remote ID + remote version, timers use
trigger + revision + occurrence. An event has one root and bounded causation depth.

An LLM plan contains: schema_version, intents[], steps[], response_intent,
needs_input[]. Each step has id, role, objective, capability, arguments,
input_refs[], depends_on[], expected_result, estimated_cost. Role names and
capabilities MUST exist; references must resolve; cycles and extra fields fail.
The executor assigns IDs, privacy, authority, deadlines, and state. It MUST NOT
trust LLM-provided identity, approval, privacy downgrades, or “already executed”.

Coordinator allocates a stable logical effect slot per intended mutation, keyed
by root event + intent index + action + subject/slot. Child assignments reference
that slot; two proposals for it share one operation ID/idempotency key. Different
payloads for the same slot are a conflict. Similar wording from unrelated user
requests is not proof of duplicate intent and must not silently merge tasks.

Work assignment context contains only the user's objective, relevant policy,
needed inputs, allowed tools, budget remaining, and completion criteria. A result
contains status, artifact_refs, evidence_refs, proposed_operations, uncertainties,
and a concise summary. Specialists may request new work; only the Coordinator
adds it after dedupe/dependency/budget checks.

Each registered capability declares name, version, input/output JSON Schemas,
read/write effects, destination, required authority, idempotency method, timeout,
retry classes, and availability. Initial catalogue:

- task.create/update/complete; reminder.schedule/snooze/cancel.
- vault.search/read/capture/compile/validate; output.draft.
- calendar.list; mail.search/read/draft; wrike.pull/push.
- weather.forecast/prepare/sources; weather.source.* provider adapters;
  [weather.local](capabilities/weather.md) prefers local sources and preserves origin.
- research.search/fetch; voice.transcribe.
- travel.plan/revise/select/recheck/monitor/stop_monitoring/save/cancel;
  optional read adapters travel.routes/stays/places; see [travel.itinerary](capabilities/travel.md).
- routine.propose/activate/pause; preference.record/propose/forget.
- notification.propose; approval.request.

This catalogue is a documented initial set. The [capability registry](capabilities/README.md)
discovers approved pack versions; it is not a closed enum. New operations use the
same generic invocation and executor without editing Coordinator or channel code.

Only executor-internal delivery.send, file.commit, db.commit, and provider write
methods actually cause effects. Installing a new capability requires code or a
reviewed adapter with schemas/conformance tests. Vault text cannot dynamically
create arbitrary shell, Python, SQL, HTTP, or filesystem execution tools.

## 6. Tasks, arbitrary requests, and time

Every accepted task is stored even if it has no date or an unsupported execution
step. A task is a desired outcome with optional context, not a finite enum of
chores. “Investigate better insurance” and “buy milk” share lifecycle fields but
may require different plans. Unsupported automation returns the supported next
step, keeps the task, and records the missing capability.

owner defaults to the authenticated user. Extracting another person's meeting
action does not assign it to the user. A task can be waiting on a named party with
an optional follow-up trigger; do not invent a contact or external message.

Allowed task transitions: inbox → ready/blocked/cancelled; ready → in_progress/
waiting/blocked/done/cancelled; in_progress → ready/waiting/blocked/done/cancelled;
waiting or blocked → ready/in_progress/done/cancelled. Any nonterminal task may
be completed explicitly. done/cancelled reopen only by explicit user action to
ready. Task completion/cancellation and cancellation of its future triggers and
pending notifications commit together. Already transmitted messages remain history.

Creation sets ready when the requested action is understood, inbox when relevant
details remain unresolved. Dates are not required for ready. An explicit
reminder creates task + trigger in one DB transaction. Missing timing saves the
task and asks one focused clarification; it MUST NOT pretend to have scheduled it.

Task due_date is a day-level obligation; due_at is an exact instant. They are
mutually exclusive. Reminder times are separate triggers and need not equal the
deadline. A deadline inferred from source text is presented as proposed until
unambiguous; no model-created timestamp is accepted without deterministic parsing
and timezone validation.

Default interpretation, overridden by approved vault behavior policy:
- Resolve relative dates against the inbound event's occurred_at in owner timezone.
- “Tomorrow at 9” means next local date at 09:00; confirm the full local date/time.
- “Tomorrow” on an explicit remind request means 09:00 next local day and says so.
- “Due tomorrow” sets due_date; one 09:00 due-day reminder is the onboarding default.
- A task without a date has no immediate reminder; include it in an enabled review.
- “Later”, “next Friday” when today is Friday, and missing AM/PM with conflicting
  context require clarification while retaining the task.
- An explicit past reminder time requires clarification; do not schedule silently
  for the next day. Imported overdue tasks enter the next digest.
- A user instruction with an explicit timezone retains that timezone.

For local recurring schedules, compute each future wall-clock occurrence with
zoneinfo; never add 24 hours to a UTC instant. Spring DST gap: shift forward by
the gap (02:30 → 03:30). Fall overlap: use first occurrence (fold=0) once.
Store local schedule, timezone, UTC instant, and chosen DST resolution.
Changing owner timezone does not rewrite explicit-zone tasks. Show affected
floating owner-timezone routines and recompute only their future occurrences.

## 7. Durable jobs, triggers, and routine definitions

Claim due work in a short BEGIN IMMEDIATE transaction using a compare-and-set
lease. Default lease 60 seconds, renewal every 20 seconds; fencing_token increments
per claim. Network/model timeouts are below the remaining step deadline. Workers
check cancellation and lease validity before each effect. An expired worker cannot
commit results or issue a new write. Already in-flight remote effects may still
finish; recover them using operation identity and provider reconciliation.

Transient retries: at most 5 attempts total, delays 5s, 30s, 120s, 600s with up to
20% jitter. Respect longer provider Retry-After if within deadline. Schema errors,
permission denial, unsupported capability, and authentication failure do not
blindly retry. Authentication failure pauses that connector until credentials
change or explicit retry. Deadline-exceeded work records its partial outcome.

A trigger definition uses one of:
- at: instant_utc, timezone, original_local, catch_up, expires_at?.
- local_schedule: days (mon..sun), at (HH:MM), timezone, catch_up, grace_seconds.
- event: event_kind, subject_filter, condition?, cooldown_seconds.
- condition: allowed fact predicate, evaluation event kinds and/or local schedule,
  cooldown_seconds, freshness_seconds.

Predicates use a typed AST with all/any/not and leaf {fact, op, value}. Operators
eq/ne/lt/lte/gt/gte/in/exists compare declared fields only; no eval or code strings.
Missing/stale evidence evaluates unknown. Three-valued logic applies: true may
fire, false does not, unknown records the missing fact. Cooldowns use UTC elapsed
time. Edge triggers fire on false/unknown → true; repeated true does not refire
until false or an explicitly defined occurrence boundary. A persistent true
condition cannot become an unlimited notification generator.

Occurrence keys: one-shot uses trigger ID; schedules use local date+wall time+
timezone+fold; event triggers use origin event ID; conditions use transition ID
or configured schedule occurrence. Trigger revision is always part of uniqueness.

Catch-up: explicit one-shot reminders fire once after restart if within 24 hours,
labelled delayed; older ones enter a catch-up digest once. Discretionary routines
default skip beyond 30 minutes. Recurring missed occurrences coalesce into at
most one useful current result, never replay a backlog of mornings. Do not send a
forecast for a departure time already passed.

Routine documents in _ctx/loop/routines have schema_version, id, title, enabled,
trigger, steps, notification, limits, privacy, and Markdown rationale. Example:

```yaml
---
schema_version: 1
id: weekday-departure-weather
title: Weather before leaving
enabled: false
trigger:
  kind: local_schedule
  days: [mon, tue, wed, thu, fri]
  at: "07:30"
  timezone: Europe/Berlin
  catch_up: skip
  grace_seconds: 1800
steps:
  - id: forecast
    capability: weather.forecast
    arguments:
      location_ref: home
      horizon_hours: 3
  - id: advice
    capability: notification.propose
    depends_on: [forecast]
    arguments:
      template: departure-weather
      include_temperature: true
notification:
  destination: owner:telegram
  category: requested_routine
  mode: each_occurrence
  cooldown_seconds: 21600
  expires_after_seconds: 1800
limits:
  model_calls: 1
  tool_calls: 12
privacy:
  model_scope: local_only
---
Check before the configured departure time. Explain useful clothing advice.
```

The YAML is declarative and validated against capability schemas. Literal inputs
and typed prior-result references are allowed; the runtime owns resolved values.
Registered template departure-weather uses the [weather bundle](capabilities/weather.md)
and deterministic advice rules in [interfaces](interfaces.md). The tool budget
includes nested source fetches. enabled:true without an activation authority is a
proposal, not permission. Authenticated “tell me every morning…” authorizes creation
and activation of that scoped routine; it does not require another approval click.
Later user edits to known routine files can update non-permission behavior after
validation; any broadened destination/tool authority remains a proposed change.

## 8. Notification policy and delivery truth

The Notification Manager, not each agent, decides delivery. All agents submit
candidates with category, relevance evidence, urgency, subject, occurrence,
destination, not_before, expiry, and why_now. It merges candidates for the same
subject/occurrence and rechecks current task/routine state just before sending.

Defaults supplied by onboarding policy:
- Owner timezone; quiet hours 22:00–07:00.
- Explicit timed reminders honor the requested instant even during quiet hours.
- Ordinary proactive suggestions defer to 08:00 or 18:00 digest; max 3 standalone
  discretionary messages per local day, one per subject per 6 hours.
- Explicitly requested scheduled routines deliver at the approved time, like
  reminders. Other suggestions produced by those routines remain discretionary;
  activation alone cannot exempt unrelated agent suggestions from the cap.
- A reply, requested reminder, or approval result is not discretionary.
- No repeated “overdue” nudge unless an escalation/repeat schedule was requested.
- During declared vacation/away/snooze windows, apply that scoped suppression.
- Notify about a connector outage once per outage episode, then show status in
  digest; don't generate daily repeated setup warnings.

Categories are reply, reminder, requested_routine, discretionary, digest, approval,
and health. Only the executor can assign a category from the originating authority.
Digest generation is deterministic unless its own activated policy permits a model;
delivery uses the same outbox and filters expired/cancelled candidates.

For competing discretionary candidates, order by latest useful delivery time,
then explicit project relevance (matched current project reference), then oldest
creation time. Above the cap, defer to digest until expiry. An LLM may suggest
phrasing/relevance but cannot promote a discretionary suggestion to a critical
alert or exempt it from the cap.

Persist a notification and outbox item transactionally with the result. Transport
workers send from the outbox and record provider message IDs. “Queued” means
committed locally; “sent” means provider acknowledged; “seen” requires actual
supported acknowledgement and is never inferred from silence. Buttons record
explicit done/snooze/dismiss/correct feedback.

Exactly-once processing of internal occurrence IDs is required; exactly-once
external delivery is not generally possible. On definitive pre-send errors retry.
On timeout/disconnect after an uncertain send, mark unknown, reconcile via
provider receipt if supported, and otherwise do not automatically resend the
same message. Show uncertainty in local status and the next relevant user query.
For a provider with real idempotency keys, safely retry the same key. Never claim
that an outbox by itself guarantees exactly-once Telegram delivery.

## 9. Vault/SQLite consistency and concurrent editors

File and SQLite commits cannot share an ACID transaction. Use a durable journaled
operation with deterministic identity:

1. Record operation intent, exact body/patch, expected hashes, reserved paths, and
   desired hashes in SQLite. Content storage is private, excluded from diagnostics.
2. Acquire the per-vault writer lease; verify policy and read receipts still apply.
3. Stage files on the same filesystem as the vault with restricted permissions;
   fsync staged bytes. Create new immutable raw files with exclusive creation.
4. For mutable files, check expected hash again immediately before atomic rename;
   preserve a preimage in the operation journal until verification. Append semantics
   still require expected hashes. fsync directories where the platform supports it.
5. Record each committed path/hash. Reconcile ledger only after required files
   exist and match. Commit DB final state and resulting events/outbox transactionally.
6. On restart, compare actual files to desired/expected hashes and resume missing
   steps. Desired already present is success for that step; third-party content
   is conflict. Preserve partial valid writes and resume without duplicate append.

Crash before ledger registration leaves an orphan raw source that reconciliation
registers by reserved identity/body hash. Crash after file+ledger but before reply
reuses the capture ID and returns the existing saved result. Conflicting external
edits require a fresh read and new patch proposal; never overwrite them by force.

A pre-rename hash check cannot prevent an uncooperative external editor writing
in the final race window. Strong multi-file atomicity is NOT claimed. Loop's own
writers MUST use the shared gateway lease, and onboarding recommends a single
active vault writer. Detect external changes with post-write verification and
watcher reconciliation, retain recoverable preimages, and surface conflicts.
Atomic rename prevents torn files, not every concurrent-editor lost update.

No automatic rollback that could overwrite newer human edits. A compensating
operation needs current hashes and the original authority. Git commits are
optional explicit routine effects; do not stage unrelated files or perform
destructive git operations. Never use the git working tree as the job queue.

## 10. Authorization, context safety, and failure behavior

Authorization and model privacy are separate decisions. Each operation needs:
authenticated owner request or active scoped routine/policy; resource scope;
allowed destination and fields; current object version; and an unexpired approval
when applicable. A direct “save this fact” or “remind me at 9” is already authority
for that precise reversible operation. Avoid asking again because a generic
default says approve.

Retain observe/suggest/approve/act levels for unsolicited actions, with per-action
ceilings. Email/Teams/Wrike/calendar writes to other people or systems need
explicit scoped authority. Email sending remains capped at approve by default;
changing a model prompt or vault prose cannot lift deployment ceilings.
Owner notifications under an activated reminder/routine are authorized sends.
A suggestion to contact someone is not authorization to send it.

An approval displays exact destination, proposed change/message, and consequence.
Bind it to actor, operation ID, input hash, and target version; default expiry
24 hours or earlier if the action expires. Stale/changed approvals cannot execute.
Record authorized before execution, committed only after verified success; never
log “executed” merely because the gate allowed it.

Untrusted retrieved text and external content are data. Tools reject path
traversal, symlink escape, oversized inputs, invalid URL schemes, unexpected
redirect destinations, and network fetches into loopback/private/link-local
addresses unless that specific local connector is configured. Web fetches do not
inherit cookies or credentials from unrelated accounts. User-authored governing
files have scoped policy authority; quoted text inside source files does not.

Privacy derives from all inputs, retrieved documents, history, tools, and active
policy. A false local_only supplied by a caller cannot downgrade true. If a
required profile/policy file is private, the reasoning using it stays local.
Only an authenticated explicit export can create a separately labelled payload
with a documented destination/field scope. Forgetting or redaction invalidates
affected plans, cached context, summaries, indexes, and queued notifications.

Failures are structured: invalid_input, needs_clarification, unavailable,
auth_required, privacy_blocked, approval_required, conflict, rate_limited,
timeout, budget_exhausted, validation_failed, unknown_effect, internal_error.
Return public-safe explanations plus correlation IDs; keep raw credentials,
source bodies, prompts, and provider authorization headers out of logs.

Model outage: deterministic task/list/done/reminder delivery continues; free text
that cannot be parsed is durably retained as needs_input, not discarded.
Calendar outage means unavailable, not “no meetings”. Vault outage means queued
capture, not “saved to vault”. Failed research produces a knowledge gap, not a
reconstructed memory of the page. Cloud fallback is impossible for any local-only
input, including tool arguments and conversation summaries.
