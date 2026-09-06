# Interfaces, configuration, and operation

Status: normative target for Loop vNext. Commands and endpoints below are
**required target interfaces**, not a claim that they exist in today's checkout.
Read [the main specification](../SPECIFICATION.md) and [runtime](runtime.md).

## 1. User interaction contract

Use ordinary language by default. An accepted message may become an answer, task,
capture, routine, correction, or several of these. Commands are predictable
shortcuts, not a requirement to learn a special task grammar.

Every response states its actual result: saved, scheduled with local date/time,
queued, proposed, needs information, unavailable, or failed. Provide the relevant
task/run ID or vault path. Never say “I'll remember/remind you” unless the durable
record and, for reminding, its trigger exist.

Disambiguation preserves known information first. Ask only for the blocking detail,
such as reminder time, which city, or which of two projects. Store that clarification
against the plan so the next reply resumes it; pending clarifications expire after
7 days without deleting the original capture/task. A new unrelated message starts
separate work. /cancel with a plan ID cancels that pending work.

Distinguish “remember this fact” (knowledge), “remember to call” (task), “what do
you know about…” (retrieval), and “every morning…” (routine). Intent classifiers
must produce typed multi-intent output; deterministic command handlers skip them.
A research answer stays in conversational state unless the user asks to save it
or activates a research-session capture preference.

By default an explicit “remember this fact” or /remember authorizes raw capture
and a bounded single-source compile under current GlebOS rules, once registration
succeeds. It does not authorize external research, a whole-vault sweep, publication,
or arbitrary project changes. “Just capture, don't compile” stops after registration.
Return the capture acknowledgement promptly and report compilation separately when
finished; awaiting context or model availability is a visible pending state.

## 2. Telegram

The owner creates a bot using Telegram's BotFather, configures its token locally,
starts Loop, and opens the exact username shown by loop status after a successful
getMe. Display https://t.me/<actual_username>; never invent a bot name. The owner
must start the bot conversation before proactive delivery can work.

Onboarding uses a short-lived local pairing flow or explicit TELEGRAM_CHAT_ID and
TELEGRAM_USER_ID settings. An unbound instance MUST NOT accept the first arbitrary
/start as its owner. Verify both chat ID and sender user ID for text, voice,
commands, edits, and callback queries. Initially allow only the paired private
chat; groups require an explicit later policy with independent privacy rules.
Unsupported edits to an already executed message require a new correction event.

Target command set:

| Command | Behavior |
|---|---|
| /start, /help | Capabilities, connection/identity state, concise examples |
| /status | Worker, next wakes, pending work/approvals, connector freshness |
| /task <text> | Persist a task and parse explicit timing |
| /tasks [today\|open\|waiting] | Paginated tasks with stable short IDs |
| /done <id> | Complete owned task and cancel its pending reminders |
| /remind <text> | Task plus reminder; retain and clarify missing time |
| /snooze <id> <duration> | Reschedule that occurrence; default 1 hour if omitted |
| /remember <text> | Exact raw capture, queued for authorized compilation |
| /find <query>, /ask <question> | Cited retrieval / grounded answer |
| /travel <description>, /trips, /trip <id> | Create a trip brief, compare sourced itineraries, inspect a saved trip |
| /weather <place or request> | Compare suitable weather sources, preferring local services, and prepare advice |
| /capabilities, /do <operation> <request> | Discover enabled abilities and invoke their validated inputs |
| /briefing | Current available facts; annotate missing sources |
| /routine <description> | Draft or activate the explicitly requested routine |
| /routines | Active/paused/proposed routines and their next runs |
| /pause <routine-id>, /resume <routine-id> | Scoped lifecycle change |
| /review | Weekly commitments, knowledge gaps, proposed adaptations |
| /why <id> | Evidence, rule revision, and reason for task/routine/message |
| /cancel <id> | Cancel task, reminder, or run by typed/unambiguous ID |

When IDs are ambiguous, present matches instead of selecting the first.
Buttons: Done, Snooze, Dismiss, Why, Approve, Reject as applicable. Callback data
uses compact opaque IDs; look up the current operation and validate actor, expiry,
subject state, and input hash. Callback acknowledgement is not execution success.
“Dismiss” suppresses a notification; it does not complete the underlying task.

Use one async bot lifecycle under the service's event loop. Do not reuse a Bot
across repeated asyncio.run calls or use fire-and-forget tasks for durable sends.
Persist inbound update before advancing the polling acknowledgement/checkpoint.
Deduplicate provider redelivery. Shut down intake, workers, polling, then clients
in a controlled order. Consult the pinned provider library's lifecycle methods;
its initialize/start and stop/shutdown are separate concerns.
[python-telegram-bot lifecycle reference](https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html).

Split long replies at safe boundaries according to the provider's current message
limit, including Unicode length and escaped markup. Store deterministic chunk IDs;
retry only definite unsent chunks. Sensitive notification mode can send a neutral
“Your reminder is ready” with local UI detail; never expose a private title on a
shared channel merely because the owner authorized some notifications.

Voice: bounded download (default 25 MB and 15 minutes), local faster-whisper,
temporary private file, delete temporary audio after successful transcription
unless explicit retention is configured. Transcription errors cannot yield
fabricated text. Transcript is local_only. Apply the same task/capture routing
and gateway rules as text; collisions must never overwrite earlier voice notes.

## 3. CLI and common application service

Every interface calls the same application services, gates, persistence, and
normalizers. Never implement separate CLI and bot reminder semantics.

Required target command groups:

```text
loop init --vault PATH [--dry-run | --apply]
loop doctor [--json]
loop run [--once]
loop status [--json]
loop task add TEXT [--due VALUE] [--remind VALUE] [--project REF]
loop task list [--status STATUS] [--due today]
loop task done ID
loop task update ID [--title TEXT] [--due VALUE] [--status STATUS]
loop remind TEXT --at VALUE
loop snooze ID --hours NUMBER
loop remember TEXT [--source URL]
loop find QUERY
loop ask QUESTION
loop research QUESTION [--save]
loop capability init PACK_ID --role daily_life --mode agent --output PATH
loop capability list
loop capability inspect PACK_ID
loop capability validate PATH
loop capability test PATH --offline
loop capability enable PACK_ID --version VERSION
loop capability disable PACK_ID
loop capability run OPERATION --input-json PATH
loop capability upgrade PACK_ID --version VERSION [--dry-run | --apply]
loop weather [LOCATION] [--hours N] [--compare]
loop weather sources [--location LOCATION]
loop travel plan DESCRIPTION
loop travel list
loop travel show ID
loop travel revise ID DESCRIPTION
loop travel select ID --revision REVISION --option OPTION
loop travel recheck ID
loop travel monitor ID --revision REVISION --option OPTION
loop travel stop-monitoring ID
loop travel save ID --revision REVISION --project SLUG
loop travel cancel ID
loop compile [--source PATH | --sweep] [--allow-fetch]
loop routine add DESCRIPTION
loop routine list
loop routine pause ID
loop routine resume ID
loop why ID
loop approve ID
loop reject ID
loop cancel ID
loop briefing
loop review
loop sync [--dry-run]
loop autonomy
loop autonomy-set ACTION LEVEL
loop note-from-audio PATH
loop metrics [--days NUMBER]
loop policy validate
loop policy diff
loop vault reconcile [--dry-run | --apply]
loop migrate [--dry-run | --apply]
loop backup --output PATH
loop restore --from PATH --dry-run
loop restore --from PATH --apply
```

For init/migrate/reconcile/restore, absence of an apply flag defaults to dry-run.
Research without --save returns cited findings without permanent vault ingestion.
Compile --source maps to no-fetch single ingest; --allow-fetch is valid for sweep
or a specifically authorized research/capture workflow and errors on no-fetch
single ingest. Resume retains the previously approved routine's exact scope.

All mutations accept --request-id UUID for caller-supplied idempotency. --json
is supported for automation on every command, returns the HTTP response envelope,
and never prints secrets. Exit codes: 0 succeeded/accepted; 2 invalid input;
3 needs information/approval; 4 unavailable/degraded requested operation;
5 conflict; 1 unexpected error. Human status remains useful when models are down.

loop telegram remains a compatibility alias for loop run with Telegram enabled;
it MUST start the scheduler and queue as well as chat. loop run --once processes
currently due work to quiescence within a fixed budget and exits; future jobs
remain persisted. It must not turn recurrent schedules into an infinite test run.

## 4. HTTP API and dashboard

FastAPI, Jinja2, HTMX; no required browser framework. Default bind 127.0.0.1.
Pages: Today, Tasks, Inbox/Knowledge, Routines, Team activity, Approvals, Memory,
Connections, Settings. Team activity shows responsibilities, queued/running
assignments, evidence and outcomes, not fabricated agent conversations.

API prefix /api/v1. Requests/responses have schema_version=1. Mutation requests
include Idempotency-Key; object updates include expected_version. Successful
envelope: {data, request_id, warnings}; asynchronous accepted result includes
run_id and current status with HTTP 202. Error envelope:
{error:{code,message,details},request_id}, with sanitized details.

Required endpoints:
- POST /messages {text, client_message_id, session_id?}; GET /runs/{id}.
- GET/POST /tasks; GET/PATCH /tasks/{id}; POST /tasks/{id}/complete.
- POST /reminders; POST /reminders/{id}/snooze; DELETE /reminders/{id}.
- POST /captures {text,title?,origin?}; GET /captures/{id}.
- GET /search?q=...; POST /questions; POST /research; POST /compile.
- GET /capabilities; GET /capabilities/{pack_id}; POST /capabilities/{operation}/invoke;
  POST /capabilities/{pack_id}/enable or /disable; [shared extension bodies](capabilities/README.md).
- POST /weather/briefings; GET /weather/sources; [weather bodies](capabilities/weather.md).
- GET/POST /trips; GET/PATCH /trips/{id}; POST /trips/{id}/select, /recheck,
  /monitor, /stop-monitoring, /save, /cancel; bodies in [travel contract](capabilities/travel.md).
- GET/POST /routines; GET/PATCH /routines/{id}; POST /routines/{id}/pause or /resume.
- GET /approvals; POST /approvals/{id}/resolve {decision}.
- POST /feedback; GET /memory; PATCH/DELETE /memory/{id}.
- GET /status; GET /activity; GET /why/{typed_id}; GET /policy;
  POST /policy/validate; POST /policy/apply {proposal_id,expected_hash}.
- GET /health/live; GET /health/ready (minimal operational information only).

Task bodies mirror runtime fields, excluding server-owned identity, authority,
privacy and lifecycle timestamps. POST /reminders takes task_id or text, at,
timezone?, expected_task_version?; snooze takes duration_seconds or at, exactly
one. DELETE cancels logically; no task deletion is implied.
Collection responses use {items,next_cursor}, limit default 50/max 100, stable
created_at+id ordering; query filters never bypass ownership/privacy.

HTTP statuses: 400 invalid input; 401 unauthenticated; 403 forbidden; 404 missing;
409 version/idempotency conflict; 422 schema validation; 429 rate limit;
503 required dependency unavailable. Reusing an idempotency key with the same
request returns the previous result; different content returns 409.
Long-polling GET /runs/{id} is sufficient; event streaming is optional.

API bearer token is required even on localhost for content/mutations. The local
CLI uses OS-user authority through the application service and does not require
an API call. UI login exchanges a configured token through POST for an opaque
HttpOnly SameSite=Strict session, default lifetime 12 hours. Require CSRF tokens
and Origin checks on browser mutations, strict CORS, and Secure cookies under
HTTPS. Tokens never appear in URLs, logs, or HTML after login. Non-local bind
requires an explicit setting and authenticated TLS reverse proxy configuration.

HTML form actions commit/queue then return 303 to their listing/detail. HTMX
requests can return fragments. Completion notices must distinguish queued from
committed. /health/live means process alive; /health/ready means DB/migrations/
worker heartbeat healthy. Optional model/connector outages show degraded details
to authenticated status clients without killing readiness or inventing empty data.

## 5. Connector contracts and dynamic capabilities

All integrations normalize transport separately from pure parsers and inject
HTTP/SDK clients. Capability status is ready/unconfigured/auth_required/degraded/
offline, with last success and error reason. One failed provider never discards
another's results. Poll each independently and update cursors only after durable
ingestion. On startup reconcile from stored cursor; full resync must be bounded.

Normalized provider objects include provider, account_id, remote_id, revision,
observed_at, source timezone, privacy, and:
- Mail: thread_id, message_id, sender, recipients, subject, body_text, sent_at,
  received_at, labels, reply_to IDs, attachments metadata, link.
- Calendar: title, start_at/end_at OR start_date/end_date for all-day, timezone,
  attendees, location?, recurrence_instance_id, cancelled, link.
- Remote task: title, status, due representation, project/folder, assignees,
  description, remote_updated_at, link.
- Weather: requested_location_ref, latitude/longitude, fetched_at, hourly
  samples with valid_at, temperature_c, apparent_temperature_c,
  precipitation_probability_pct, precipitation_mm, wind_kph; provider attribution.
- Research result: URL, title, author?, published_at?, fetched_at, extracted body,
  extraction status, content hash. Search snippets alone are not read evidence.
- Travel: date-specific routes, lodging/venue options and money estimates with
  timezone, applicability, quote expiry, inclusions and evidence; normalized
  contracts in [travel.itinerary](capabilities/travel.md).

Google mail/calendar share OAuth token state and a union of requested scopes;
request write scopes only when enabling writes. Outlook uses delegated device
authorization for /me resources; calendarView expands occurrences. Preserve
provider timezones/all-day dates and deduplicate recurrence instances. Microsoft
client secrets are not required for the inspected delegated Outlook flow.
Expired auth becomes auth_required; never pretend that provider returned no data.

Wrike remains optional. Pull is read-only. Push requires explicit project mapping,
field scope, and authority for those tasks; personal captures MUST NOT be exported
because a global wrike_write level is act. Store a sync base and remote revision.
Completion reconciliation may preserve a known done state; conflicting concurrent
title/date edits produce a conflict proposal. Remote absence counts as deletion
only after a successful complete scoped listing, never after a partial/outage page.
Retries preserve remote IDs; do not duplicate task creation.

Mail follow-up default: after 48 hours without an inbound reply in the relevant
thread, propose one follow-up candidate. Exclude drafts, automated no-reply
addresses, and explicitly dismissed threads. Receipt of reply cancels pending
candidate. Drafting is allowed within requested scope; sending needs its own
authority. Calendar reminder defaults 30 and 5 minutes before non-cancelled timed
events, activated at onboarding; changes cancel stale future occurrences.
These are configurable vault routines, not hardcoded specialist loops.

New sensors (location, transit, packages, health devices) are optional adapters.
Do not infer live location, weather, departure, or device state without a configured
source. Adding a declarative routine can compose existing tools; adding an unknown
tool requires implementation and tests. Unknown capabilities remain visible and
inactive instead of silently converting into generic chat.

## 6. Weather and daily-life behavior

The [weather.local capability](capabilities/weather.md) owns source selection,
normalization and comparison. Prefer fresh, suitable official local forecasts,
nearby representative observations/nowcasts, and authoritative warnings. Compare
other supported products, record shared origins, and explain disagreements.
Local-source failure uses a clearly labelled suitable fallback, not fabricated data.

The existing Open-Meteo /v1/forecast adapter remains a fallback/comparator for
supported temperature, apparent temperature, precipitation probability/amount and
wind fields. Preserve units, intervals and actual model origin. Its output is
normalized alongside other providers instead of being the sole source.
[Open-Meteo forecast reference](https://open-meteo.com/en/docs).

Use the location specified by the user or selected at onboarding; do not guess
from timezone. Coordinates may be city-level. Network authorization sends only
necessary coordinates, forecast fields and time range to that provider, not the
profile, home address text, calendar titles, or conversation.

Default horizon is the next 3 hours; forecast transport cache expires after 60 minutes.
Product issue age and coverage are checked separately; fetching an old run again
does not refresh its meteorological age. Observations/nowcasts and warnings have
shorter product-specific cache limits in the weather contract.
A fresh response covering the requested horizon is required for rain claims.
No forecast during an outage means “weather unavailable”; older results may be
shown with their timestamp but cannot be described as current or imply no rain.

Initial configurable advice rules (product heuristics, not provider claims):
- Rain preparation when max hourly probability >=40% OR precipitation >=0.2 mm
  within the horizon: suggest a waterproof layer/umbrella with the time window.
- Apparent temperature <=0°C: insulated layers; >0 and <10°C: warm jacket;
  10 to <18°C: light jacket/layer; >=18°C: light clothing with optional layer.
- Wind >=30 km/h: mention a wind-resistant outer layer; heavy wind means an
  umbrella may be less practical. Do not infer actual garments owned by the user.
- Explicit heat/cold sensitivity preferences adjust phrasing or configured
  thresholds; do not infer medical needs.

Apply these rules to valid source-backed fields. Incompatible intervals and
precipitation types cannot be silently merged. Disagreement changes the explanation
and may produce conditional advice; it never manufactures consensus. Preserve
official warnings and per-product availability, even when a forecast looks mild.

Structured evidence determines advice; a local model may phrase it concisely.
Every item can explain forecast time, departure assumption, and applied preference.
“Every morning give me weather” uses each_occurrence delivery. “Warn me if rain
before I leave” uses changes_or_actionable and condition/cooldown semantics.
Missing departure time or location creates a saved routine proposal and a focused
question. A confirmed “working from home today” suppresses only the commute-scoped
routine for that day. Do not invent a persistent commute from one calendar event.

## 7. Configuration: deployment versus behavioral rules

One Pydantic Settings object owns environment/deployment configuration; no SDK
reads ad hoc environment variables. Vault policy owns personal behavior and
routines. SQLite stores the validated policy revision/cache, not a second editable
set of competing rules. Credentials stay outside the vault and version control.

Required target settings, uppercase environment names, case-insensitive:

| Setting | Default / validation |
|---|---|
| ENVIRONMENT | development; development/production/test |
| TIMEZONE | Europe/Berlin; valid IANA name, initial owner-timezone default |
| DATABASE_URL | sqlite:///data/loop.db; local file; tests may use memory |
| DATA_DIR | data; resolved absolute path at startup |
| CAPABILITY_PATHS | JSON list ["capabilities", "data/capabilities"]; confined discovery roots, resolved at startup |
| OBSIDIAN_VAULT_PATH | empty; explicit selection required for vault writes |
| OBSIDIAN_PRIVATE_VAULT_PATH | empty; optional secondary read source |
| VAULT_BACKEND | builtin; builtin/scriptorium |
| SCRIPTORIUM_COMMAND | empty; explicit executable/configured argv, never shell eval |
| LOOP_POLICY_PATH | _ctx/loop/manifest.yaml; vault-relative and confined |
| OLLAMA_BASE_URL | http://localhost:11434 |
| OLLAMA_DEFAULT_MODEL | empty on fresh install; select/pin an installed model in init |
| OLLAMA_EMBED_MODEL | empty; embeddings optional until selected |
| CLOUD_ENABLED | false |
| ANTHROPIC_API_KEY | empty; secret, needed only for enabled cloud adapter |
| ANTHROPIC_MODEL | empty; explicit currently available model ID if cloud enabled |
| CLOUD_DAILY_BUDGET_USD | 0; positive budget required to enable paid fallback |
| CHROMA_PERSIST_DIR | data/chroma; optional local derivative |
| TELEGRAM_BOT_TOKEN | empty; secret |
| TELEGRAM_CHAT_ID, TELEGRAM_USER_ID | empty; both required after pairing |
| GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH | credentials.json, token.json; secret files |
| OUTLOOK_CLIENT_ID, OUTLOOK_TENANT_ID | empty, common |
| TEAMS_APP_ID, TEAMS_APP_PASSWORD | empty; optional connector secrets |
| WRIKE_API_KEY | empty; secret, optional |
| WEB_HOST, WEB_PORT | 127.0.0.1, 8000 |
| LOOP_API_TOKEN | empty initially; init generates a secret in private deployment storage |
| MAX_AUTONOMY_EMAIL_SEND | approve; deployment ceiling |
| PRIVATE_VAULTS | empty legacy list; GlebOS is local_only by default |
| PRIVATE_PERSONAL_CALENDAR, PRIVATE_TELEGRAM_PERSONAL | true, true; disabling never strips existing labels |
| MAX_ACTIVE_ASSIGNMENTS, MAX_LOCAL_INFERENCE | 2, 1 |
| JOB_LEASE_SECONDS, JOB_HEARTBEAT_SECONDS | 60, 20; heartbeat < lease / 2 |
| RECONCILE_SECONDS | 300; deterministic sweep, no idle model call |
| SHUTDOWN_GRACE_SECONDS | 30 |
| MODEL_TIMEOUT_SECONDS, HTTP_TIMEOUT_SECONDS | 60, 20; capped by remaining deadline |
| WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE | base, cpu, int8 |
| SESSION_TTL_HOURS | 24; session rollover, not content retention |
| CONVERSATION_RETENTION_DAYS, ARTIFACT_RETENTION_DAYS | 30, 30; active work pins needed content |
| AUDIT_RETENTION_DAYS | 90; metadata only |

Secrets generated during installation use cryptographic randomness, OS-restricted
files (0600), and are redacted from status/export. Token rotation invalidates UI
sessions. No cloud model gets enabled just because an API key happens to exist.
Reserve an estimated maximum call cost transactionally before a cloud call;
settle actual usage afterward. Unknown pricing or exhausted budget blocks that
call without affecting local inference. Pin a dated price configuration during
cloud adapter setup; this spec does not claim a current model or price.

Behavior documents have YAML frontmatter schema_version=1 and Markdown rationale.
Required normalized policy schema:
- behavior: owner_timezone, default_reminder_time, ambiguous_date_strategy=ask,
  task_due_day_reminder=true, locations map {id:{label,latitude,longitude}},
  departure schedules, project aliases/mappings; unspecified locations/schedules empty.
- notifications: quiet_start/quiet_end, digest_times, discretionary_daily_limit,
  subject_cooldown_seconds, redacted_destinations, vacation windows.
- permissions: defaults by action, deployment-ceiling reference, scopes with
  resource patterns, allowed fields/destinations, activation evidence.
- routines: exact schema in runtime; initially disabled unless expressly activated.
- learning: timing_min_samples=5, timing_window_days=28, min_shift_minutes=15,
  max_iqr_minutes=30, rejection_cooldown_days=30, hypothesis_ttl_days=30.
- travel: preferences, candidate limits, pace, transfer buffers, evidence freshness
  and monitoring thresholds; defaults in [travel policy](capabilities/travel.md#8-configuration-and-conformance).
- weather: prefer_local=true, source priorities, comparison thresholds and product
  freshness; defaults in [weather policy](capabilities/weather.md#7-interfaces-and-editable-policy).
- capabilities: per-pack enablement and approved scopes in _ctx/loop/capabilities;
  installed manifests/lifecycle follow the [extension contract](capabilities/README.md).
- limits: per-root budgets from runtime, never above deployment ceilings.

Compiled policy hashes include every governing source file. A watcher validates
changed policy and publishes an atomic revision. Invalid scope retains its last
valid revision and shows the error. Permission increases always need a matching
authenticated approval/activation record. Prompt prose can guide reasoning but
does not bypass typed constraints. UI/chat edits generate a reviewable vault patch,
then apply it under existing scope; no hidden preferences database that overrides
the edited vault on restart.

## 8. Packaging, deployment, and lifecycle

Required build artifacts: pyproject.toml, uv.lock, .python-version (3.12 minimum),
config/.env.example, versioned migrations, Dockerfile, Compose configuration,
setup script, and a lockfile-based CI workflow. Pin release dependencies in uv.lock;
declare pydantic-settings explicitly so an activated-looking Conda prompt cannot
hide a missing runtime dependency. Project environments and locks follow uv's
documented model. [uv project guide](https://docs.astral.sh/uv/guides/projects/).

Target development workflow:

```bash
uv sync --locked --extra dev
uv run loop init --vault ~/Claude_Cowork/GlebOS --dry-run
uv run loop init --vault ~/Claude_Cowork/GlebOS --apply
uv run loop doctor
uv run loop run
```

A fresh implementation must generate and commit uv.lock before --locked setup.
Optional voice install uses uv sync --locked --extra dev --extra voice.
All examples use uv run to select the project interpreter. Setup prints the
resolved interpreter and reports Python <3.12 before imports fail. Packaging MUST
include all declared modules, templates and static assets. Docker must copy package
sources before installing the project; dependency-only layers can use uv's
no-install-project approach. The earlier “package directory core does not exist”
build failure must have a regression build check.

Docker runs a non-root user with writable data and configured vault access;
Ollama is a separate optional service or configured host endpoint. Bind-mount the
actual vault root at a container path and set OBSIDIAN_VAULT_PATH to that container
path. Mount credentials deliberately; never bake .env, raw notes, model caches,
or tokens into an image. SQLite WAL lives on a local filesystem, not network
storage; SQLite documents the WAL shared-memory constraint.
[SQLite WAL reference](https://www.sqlite.org/wal.html).

Default container command is loop run. One process owns scheduler/inbound poller
leases per instance; a web reload worker MUST NOT start duplicate schedulers.
Use a single async lifespan to start DB, policy/cache, gateway/recovery, job
workers, timer service, independently configured connectors, intake, and health.
Optional UI may serve in the same process or connect to the application API;
standalone web imports have no scheduling side effects.

On SIGTERM stop accepting new work, stop issuing effects, checkpoint active jobs,
drain acknowledged outbox results up to grace time, close clients, release leases.
After forced shutdown, lease expiry and operation journals recover. service/launchd/
systemd or Docker restart policies restart the process. A sleeping/offline laptop
cannot notify on time; show last heartbeat/catch-up state. Continuous availability
requires an always-on host with authorized vault access.

Backups use SQLite's backup API or a coordinated snapshot, plus vault snapshot,
policy revisions, and operation journal; exclude tokens by default. Record manifest
hashes and schema version. Pause writers during the short consistency checkpoint.
Restore dry-run validates and shows destination/conflicts; apply requires explicit
user authority and must not overwrite unrelated files. Test restore in isolation.
Derived search/vector indexes can be rebuilt. Data retention purges expired content
and derivatives while preserving pending commitments and minimum audit metadata;
ordinary vault sources are retained unless explicitly forgotten.

Health/metrics: oldest due-job age, queue depth, last worker heartbeat, next wakes,
connector freshness, outbox unknown/failed counts, source/ledger conflicts,
policy revision, model availability/latency/usage, budget remaining, active team
assignments. No fabricated all-green state when no provider is configured.

## 9. Migration from the inspected implementation

Preserve existing IDs, completed tasks, follow-ups, preferences, remote IDs and
privacy. Back up first; run a dry-run mapping report. Introduce versioned migrations
instead of relying on additive create_all bootstrapping. New tasks use flexible
context rather than mandatory Wrike/project fields. Legacy due dates remain dates;
do not silently turn them into midnight deadlines or activate old reminders.

Map legacy scheduler settings (BRIEFING_TIME, FOLLOW_UP_WINDOW_HOURS,
WEEKLY_REVIEW_DAY/TIME, WRIKE_SYNC_MINUTES) to proposed vault routine documents.
Map DEFAULT_AUTONOMY_LEVEL and autonomy.* preferences to typed proposed permissions,
preserving the more restrictive level until activated. PROJECTS CSV mappings become
aliases for actual 2-projects manifests; do not scan a nonexistent PARA Projects/.
WRIKE_FOLDER_ID is a suggested mapping, not global authority to push all tasks.
Existing explicit per-task approvals remain scoped to their original payload.

Keep current model IDs if available and owner-configured; verify installed/remote
availability during doctor, never silently replace them with a guessed “latest”.
Deprecate duplicate fallback-length/latency settings through warnings and a mapping
report; structured validity and the new budgets govern target routing.
OUTLOOK_CLIENT_SECRET is retained only as a deprecated ignored compatibility key
for delegated auth. Existing local-only data can never migrate to cloud_allowed
by omission of a new column.

Do not auto-register the vault's prepared daily compile prompt, rebuild its folder
structure, rewrite raw sources, or import archived architecture documents as active
rules. Reindex with corrected privacy labels after migration, then compare source
counts, ledger identity, open tasks, and next approved wakes. Keep the old database
backup and [historical specification](../archive/SPECIFICATION-phase-4-2026-09-05.md)
for traceability; it is not required to reconstruct the target.

## Agent execution storage and diagnostics (specification 1.3)

Apply [agent-stack.md](agent-stack.md) for graph storage, lifecycle and recovery.
The default local checkpointer is data/graph-checkpoints.sqlite, separate from
SQLAlchemy domain storage. Backup/restore must pause both writers and include
both databases with matching version/hash metadata. Status must expose run and
checkpoint references, pinned graph/package versions, pending approvals and
missing-version recovery blocks without displaying private checkpoint content.
No additional provider credentials are needed for the graph itself. Remote content
tracing remains disabled by default; environment flags must not bypass privacy.
