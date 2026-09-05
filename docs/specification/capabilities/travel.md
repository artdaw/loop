# Travel itinerary capability

**Pack:** travel.itinerary · **Version:** 1.0.0 · **Status:** normative target,
not implemented by this specification update.

Read [main](../../SPECIFICATION.md), [runtime](../runtime.md),
[interfaces](../interfaces.md), [vault](../vault.md), and
[acceptance](../acceptance.md). This pack extends Daily-life planner with trip
discovery, itinerary comparison, practical scheduling, and optional monitoring.
Use the [shared extension contract](README.md) for packaging, registration,
generic invocation, upgrades and tests; this pack adds no separate plugin mechanism.

## 1. User outcome and scope

Example request: “Find a good five-day itinerary in northern Italy in October,
starting from Berlin, under €1,200 for one person. I like architecture, local food,
and a relaxed pace. Prefer trains.”

This is an illustrative request, not a researched trip or price claim. Loop saves
the brief, resolves missing dates/budget scope when needed, researches current
options, and returns up to three distinct itineraries with a recommended choice,
tradeoffs, day-by-day activities, door-to-door transport, lodging areas, costs,
source links, and unresolved decisions. “Less travel, more time in one city”
revises the same trip with the new constraint.

“Good” means feasible and suitable for the user's stated priorities. The system
does not claim a global optimum or simply rank by a model's enthusiasm.
Destination discovery is supported: if a user asks for a long weekend somewhere,
compare candidate destinations first using the same budget/time constraints.
Missing dates permit an explicitly provisional outline; they do not permit
invented live availability or precise fares.

Planning grants authority for scoped public research and local trip persistence.
It does not book, reserve inventory, pay, send messages to hosts, create calendar
events, or start indefinite monitoring. Those are separate actions/authorities.
A requested trip remains useful even when booking adapters are absent.

## 2. Pack and capability registration

Abridged registry overview; a file alone does not provide missing adapters.
A runnable pack supplies the full manifest/operation definitions from the shared
extension contract. Schemas below are normative and MUST be published as JSON
Schemas in the runtime registry when implemented.

```yaml
schema_version: 1
id: travel.itinerary
version: "1.0.0"
owner_role: daily_life
support_roles: [seeker, commitments, reviewer]
capabilities:
  - travel.plan
  - travel.revise
  - travel.select
  - travel.monitor
  - travel.recheck
  - travel.stop_monitoring
  - travel.save
  - travel.cancel
dependencies:
  required:
    research.search: ">=1.0.0,<2.0.0"
    research.fetch: ">=1.0.0,<2.0.0"
    vault.search: ">=1.0.0,<2.0.0"
    vault.read: ">=1.0.0,<2.0.0"
  optional:
    travel.routes: ">=1.0.0,<2.0.0"
    travel.stays: ">=1.0.0,<2.0.0"
    travel.places: ">=1.0.0,<2.0.0"
    weather.forecast: ">=1.0.0,<2.0.0"
    calendar.list: ">=1.0.0,<2.0.0"
defaults:
  enabled: false
  budget_class: research
effects: [local_domain_write, network_read, owner_notification_proposal, vault_write]
policy_namespace: travel
default_monitoring: false
```

Required research tools may use the existing configured search/fetch adapters;
specialized provider adapters are optional. Without current research, return a
saved brief/partial outline with missing capabilities, not verified recommendations.
Missing route/stay adapters may be replaced by actually read official provider
pages, with explicit evidence and equivalent normalized fields; do not fabricate
live results from a model or a search snippet.

Roles: Daily-life owns the trip brief and itinerary; Seeker gathers evidence;
Commitments proposes preparation tasks; Reviewer checks feasibility/evidence.
Coordinator controls assignments and shared budgets. Use the existing explicit
research budget for plan/revise and background-routine budget for recheck; all
child roles count against the same root budget. Planning can checkpoint by region
or day when budget runs out. No dedicated always-running travel agent is required.

## 3. Input contract and personal preferences

TripBrief schema_version=1 fields:

| Field | Type and meaning |
|---|---|
| origin | PlaceRef or null; required for claiming outbound/return feasibility |
| destinations | List of PlaceRef; empty when destination discovery is requested |
| date_window | start_date/end_date or earliest_departure/latest_return + duration_days; nullable for exploration |
| travelers | adults >=1, children count default 0, ages only when relevant to quotes; no inferred passport data |
| budget | amount_minor, currency, basis total/per_person, includes list, hard_limit boolean; nullable means no budget-fit claim |
| interests | Ordered weighted tags or descriptions explicitly requested/confirmed |
| pace | relaxed/balanced/busy; default balanced, disclosed |
| transport | allowed modes, preferred modes, max_transfers?, max_daily_travel_minutes? |
| accommodation | preferred area/type, rooms?, accessibility/amenity requirements? |
| constraints | must_include, must_avoid, mobility requirements, dietary requirements, fixed_segments |
| context_refs | Authorized project/calendar/booking references; empty by default |
| priorities | Ordered soft preference keys; defaults described in §5 |
| assumptions | Disclosed provisional values with reason/source; never satisfy an unknown hard constraint |

PlaceRef has label, country_code?, provider_place_id?, latitude?, longitude?,
timezone?. A city/station/airport must be disambiguated before using its coordinates
or computing exact transfer times. Display the interpreted date range including
year. For flexible dates, retain the search window and actual chosen dates for
each option. A per-person budget is multiplied by the stated traveler count;
shared room/group costs are counted once, with allocation shown.

Use exactly one fixed/flexible date representation when known; ISO local dates,
positive duration, end >= start, and enough days within the search window are
validated before search. Currency uses ISO 4217 codes, integer minor units and
decimal conversion, never binary floating-point arithmetic for budget totals.
Dates and origin may be absent for exploration, which yields tentative outlines.

Load confirmed travel preferences from _mem/loop/preferences and travel policy
from _ctx/loop/behavior.md. Explicit trip instructions override general preferences.
A preference learned on a work trip cannot silently change family-holiday rules.
Unspecified nice-to-have details use disclosed defaults; only blocking details
require a question. Unknown dates, arrival commitments, accessibility constraints,
or cost scope stay visible rather than being guessed into compliance.

Personal context remains local_only. External requests receive only necessary
route dates/locations, party size and relevant filters. Never submit a profile,
calendar title, passport number, payment detail, or private reservation code to
general search. Use an authorized connector for private booking records.

## 4. Research, evidence and freshness

Search compiled vault knowledge first for durable interests and existing trip
context. Verify time-sensitive claims through current external sources:

- Transport operators and official station/airport pages for schedules and terms.
- Venue pages for opening days, timed entry, closure notices and admission.
- Accommodation providers for dated quotes and occupancy assumptions.
- Maps/routing providers or official transport information for transfer legs.
- Tourism/editorial/community sources for discovery, labelled as recommendations;
  popularity/reviews do not prove current opening or accessibility.

Each evidence item records ID, URL/provider, fetched_at, applicable date range,
claim type, supporting excerpt/result fields, privacy, and expires_at. Source
content is untrusted data under runtime policy. Conflicting authoritative results
remain unresolved and downgrade affected options.

Initial freshness ceilings: fare/lodging availability 15 minutes, published
transport schedules 24 hours, venue hours 7 days; use the earlier provider expiry
when supplied. These are configurable cache rules, not guarantees that prices or
service remain unchanged. Recheck relevant evidence when presenting a refreshed
recommendation. Weather uses [weather.local](weather.md), selecting sources for
each destination and retaining disagreement, product age, warnings and coverage;
distant travel dates cannot receive a fabricated forecast.
Use labelled historical seasonal context only with sourced evidence.

Each cost is a MoneyEstimate: category, amount_min_minor, amount_max_minor,
currency, basis per_person/per_room/per_group, quantity, quote_or_estimate,
includes/excludes, fetched_at, expires_at?, evidence_refs. Normalize taxes,
mandatory fees, baggage and local transfers where known; unknown mandatory costs
make budget compliance tentative. Optional extras remain separate.
Cross-currency totals require a dated sourced exchange rate and disclosed
conversion; otherwise show separate totals and no definitive budget comparison.

Travel reads can still incur provider charges. Such adapters require configured
account/budget authority, timeouts and rate limits. A trip spending budget does
not authorize paid API usage, reservations or purchases.

## 5. Itinerary construction and quality checks

1. Persist the brief and version before research. Resolve hard blockers while
   continuing independent discovery where useful.
2. Generate a bounded shortlist: at most five destination/route candidates, three
   final options. Investigate genuinely distinct options; return fewer if fewer
   are supported.
3. Build each option with ordered days and explicit transport between activities,
   including origin → departure point, intercity transfers, lodging/check-in,
   meals/rest, and return travel when requested.
4. Validate each hard constraint deterministically. Known violations exclude an
   option from the feasible shortlist; unknowns make it tentative. If no feasible
   plan exists, explain which constraints conflict and propose concrete relaxations.
5. Rank feasible options ahead of tentative ones, then apply the user's ordered
   soft priorities. Default order: interest coverage, pace fit, lower door-to-door
   travel burden, then lower upper-bound total cost. Interest coverage is the sum
   of matched requested weights / sum of requested weights; evidence links each
   match to an activity. Missing interests skip that criterion. Pace violations
   count against pace fit. Tie-break by fewer unresolved facts, then stable option ID.
6. Present a recommended option and distinct alternatives such as cheaper or more
   relaxed when supported. Explain the actual differences and unmet preferences;
   do not manufacture multiple nearly identical options.
7. Store immutable result/evidence artifacts, update current trip version using
   expected_version, and deliver a concise summary with expandable daily detail.

Each scheduled segment has local start/end, IANA timezone, derived UTC instants,
location, type, duration, route/evidence refs, cost refs, booking status
unbooked/user_reported/verified, and fixed boolean. Each day uses destination-local
date; overnight/date-line travel must preserve actual arrival date. Fixed confirmed
segments are anchors; revisions never change them or imply a booking was changed.

Validation MUST detect overlapping segments, impossible arrival/check-in times,
opening-day and last-admission conflicts, omitted travel legs, hard mobility
constraints and budget violations. Unknown accessibility cannot be reported as
verified accessible. Booking status is verified only with an authorized source.

Proposed buffers and dwell durations are disclosed. Default local travel buffer:
max(15 minutes, 20% of estimated leg time). Operator-specific check-in/connection
requirements override generic estimates; unknown requirements are flagged.
No claim that a conservative estimate guarantees a connection. Default anchor
activities/day: relaxed <=2, balanced <=3, busy <=4, excluding meals/transit;
these are adjustable preferences, not universal travel advice.

Result schema: trip_id, revision_id, brief_version, generated_at, status
ready/partial/no_feasible_plan/needs_input, options[], recommended_option_id?,
missing_information[], next_actions[], evidence_refs. Each option has id, summary,
feasibility feasible/tentative/infeasible, dates, days[], costs[], total_range?,
constraint_checks[], preference_matches[], assumptions[], unresolved[],
alternatives_for_disruption[], and why_recommended. Detailed daily text and maps/
source links must agree with the structured schedule. Internal claims of feasibility
are limited to checked constraints and timestamped evidence, never guaranteed service.

## 6. Persistence, edits and public interfaces

Add the trips logical table described in runtime: owner/source event, title,
brief_artifact_id, current_revision_id?, selected_revision_id?, selected_option_id?, status, monitoring
routine ID?, project_ref?, privacy, and common ID/version/timestamps.
Briefs, evidence and itinerary revisions are immutable artifacts. Trip status is
planning/needs_input/ready/no_feasible_plan/active/completed/cancelled.
Ready includes explicitly partial/tentative revisions with their detailed status;
it never means booked. A missing result can never be ready.

current_revision_id points to the latest completed proposal; selected_revision_id
and selected_option_id point to the user's accepted plan and change only through
explicit selection. A new brief version marks previous proposals stale but keeps
the selected plan visible with a needs-review indication. Async results commit
only against the brief/version they read; competing revisions produce a conflict.

Capability inputs/outputs:
- travel.plan: {brief: TripBrief}; returns accepted {trip_id,run_id}, followed by
  the result artifact. Effects: local persistence and scoped research.
- travel.revise: {trip_id,expected_version,changes}; changes is a partial TripBrief;
  returns accepted run. Original evidence/revisions remain available.
- travel.select: {trip_id,expected_version,revision_id,option_id}; validates
  revision/option ownership and selects that option. Returns updated trip without
  booking or activating monitoring. If monitoring is already active, atomically
  replace only its future checkpoints/subscriptions for the newly selected plan.
- travel.monitor: {trip_id,expected_version,revision_id,option_id,checks}; checks is an
  allowlisted subset of schedule/closure/weather/cost. Missing option/dates or
  unsupported checks yield needs_input/unavailable, never an active claim.
- travel.recheck: {trip_id,expected_version}; bounded one-time revalidation,
  returns change report/proposed revision; usable without enabling monitoring.
- travel.stop_monitoring: {trip_id,expected_version}; disables only that trip's
  subscriptions/jobs and pending monitoring notifications.
- travel.save: {trip_id,expected_version,revision_id,project_slug}; returns queued
  operation, then verified vault path. Saves the selected option if it belongs to
  that revision, otherwise clearly labelled alternatives; does not select one.
- travel.cancel: {trip_id,expected_version}; marks local trip cancelled and stops
  its monitoring atomically. External bookings and independent tasks are unchanged.

Monitor explicitly selects the supplied revision/option and returns an active
trip only once its routine, subscriptions and triggers are committed. Omitted
checks default to the configured supported subset, shown before activation;
explicitly requested unsupported checks must never be silently dropped.
One-time recheck defaults to the selected revision or, if none, the latest proposal.
It produces evidence and a proposed revision without replacing the selected plan.
All capability results use the shared envelope, operation IDs and immutable
artifacts; local operations return committed state, research returns a run ID.

Provider capability contracts travel.routes, travel.stays, travel.places take a
scoped brief subset and return normalized segments/places/MoneyEstimates with
evidence and availability status. Registry versions pin their exact JSON Schemas;
none exposes a booking write operation in this pack.

Target CLI: loop travel plan DESCRIPTION, list, show ID, revise ID DESCRIPTION,
select ID --revision REVISION --option OPTION, recheck ID,
monitor ID --revision REVISION --option OPTION, stop-monitoring ID,
save ID --revision REVISION --project SLUG, and cancel ID.
All existing JSON/idempotency conventions apply; mutations use --expected-version
or the version just read by the interactive application service, never a blind write.
Telegram: /travel <description>, /trips, /trip <id>; ordinary follow-up messages
resolve the trip ID through session context or ask when ambiguous. Buttons:
Compare, Revise, Recheck, Monitor, Stop monitoring, Save to vault.

HTTP /api/v1: GET/POST /trips; GET/PATCH /trips/{id};
POST /trips/{id}/select, /recheck, /monitor, /stop-monitoring, /save, /cancel.
POST takes {brief} or {description}, exactly one; parsed descriptions must become
a validated TripBrief. PATCH takes {expected_version,changes}. Other mutation
bodies include expected_version plus capability-specific fields; save takes
project_slug and revision_id. List/show expose current status and source freshness.
All mutations use existing identity, authorization, idempotency, version and
async response contracts. Selecting/saving a trip never marks its tasks completed.

## 7. Proactive support and knowledge retention

“Keep this itinerary updated” or monitor explicitly activates selected checks
until trip end. Default bounded rechecks: once 7 days before departure, once
24 hours before departure, and 07:00 destination-local time on each travel day.
Skip checkpoints already in the past; an explicit monitor request rechecks once
immediately. Deduplicate coinciding checkpoints. Determine the daily timezone
from the selected itinerary; ambiguous transit days use the owner timezone and
show that assumption. Provider events may trigger relevant extra checks with
a 6-hour cooldown; explicit recheck bypasses that cooldown.

Notify when evidence invalidates a hard constraint, a closure affects a stop,
transport changes by >=30 minutes, total cost estimate increases >=20% or exceeds
budget, or fresh weather warrants changing an outdoor block. Store actual before/
after evidence and apply shared notification policy. Do not emit “price changed”
without a comparable dated quote for the same dates/party/inclusions. No-change
checks use deterministic comparison and do not invoke a model. Only materially
changed context can request bounded alternative planning.

Current confirmed itinerary and bookings stay intact while a revised proposal is
prepared. User acceptance selects the new revision/option. Ask for new authority
before any calendar write, reservation, cancellation or purchase. Duplicate events,
restarts and travel cancellation must not create repeated alerts; cancellation
terminates the trip's monitoring and pending notifications transactionally.

Planning artifacts are operational state in SQLite, not automatically wiki facts.
An explicit Save to vault writes a versioned operational projection under an
existing or explicitly requested 2-projects/<slug>/Process/Itinerary.md, preserving
human edits through the gateway. Creating a missing project requires a manifest
with the vault's type/title/status/goal/updated fields; the save request authorizes
that scoped project creation. Include loop_trip_id, revision, source timestamps,
links, assumptions and unbooked/confirmed distinctions. Never replace a colliding
human file; use a stable suffixed projection path instead.

Reusable destination knowledge or trip reflections, when requested, follows raw
capture → ledger → Compiler → wiki. Temporary fare quotes and forecasts expire in
operational state and do not become timeless entity facts. A polished guide in
3-output derives from compiled wiki under the existing output rule.
“Use fewer hotel changes next time” becomes an explicit scoped preference;
one itinerary selection alone does not establish a permanent travel habit.

## 8. Configuration and conformance

Store travel policy under the existing _ctx/loop/behavior.md travel key, with
schema_version=1 in that document. Fields/defaults: default_pace=balanced,
max_candidates=5, max_options=3, local_buffer_minutes=15,
local_buffer_fraction=0.2, anchor_limits={relaxed:2,balanced:3,busy:4},
freshness_seconds={quotes:900,schedules:86400,venue_hours:604800},
monitor={predeparture_days:[7,1],daily_at:"07:00",event_cooldown_seconds:21600},
change_thresholds={transport_minutes:30,cost_fraction:0.2}.
Trip-specific overrides stay in its brief; preference changes use memory policy.
Missing optional adapters are reported per evidence field. API keys/endpoints
remain deployment settings owned by the selected tested adapters, outside the vault.

The travel.plan, revise, recheck, monitor and stop contracts and tests are required
along with select, save and cancel for this specification version;
provider-specific live connections remain optional.
See TR01–TR24 in [acceptance](../acceptance.md). Demonstrate comparisons,
constraint validation, revision conflicts, stale data, explicit monitoring,
notification deduplication and vault provenance using synthetic fixtures.
