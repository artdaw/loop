# GlebOS contract: knowledge, rules, and personal memory

Status: normative target contract for Loop vNext, with observed-vault facts explicitly labelled.
Read together with [the main specification](../SPECIFICATION.md). Paths below are
relative to the configured vault root unless stated otherwise.

## 1. Observed vault, 2026-09-05

The inspected vault is `/Users/gleb/Claude_Cowork/GlebOS`. Its root `CLAUDE.md`
identifies it as v2: a source → wiki → output pipeline replacing the earlier
PARA-folder-plus-nine-agents design. Do not create parallel `Projects`, `Areas`,
`Resources`, `Archive`, `System`, or `Inbox` directories inside this vault.

| Location | Authority and purpose |
|---|---|
| `CLAUDE.md` | Entry point and structural summary; points to authoritative details |
| `Dashboard.md` | Human navigation and derived queries, not authoritative counts |
| `0-raw/inbox/` | New verbatim captures |
| `0-raw/{clips,notes,docs,threads,transcripts}/` | Existing source collections |
| `0-raw/_ledger.md` | Source identity, capture/compile state, pages produced |
| `1-wiki/concepts/` | Reusable concepts and atomic ideas |
| `1-wiki/entities/` | Durable facts about people, organizations, tools, products |
| `1-wiki/topics/` | Topic hubs connecting knowledge |
| `1-wiki/index.md` | Derived wiki map |
| `1-wiki/open-questions.md` | Unresolved contradictions and coverage gaps |
| `2-projects/<slug>/CLAUDE.md` | Project status, goal, completion criteria, scoped instructions |
| `3-output/{posts,articles,decks,deliverables}/` | Deliverables derived from the wiki |
| `4-journal/daily/YYYY/` | Daily plans, tasks, scratch notes, reflection |
| `4-journal/meetings/YYYY/` | Meeting notes and action items |
| `4-journal/briefs/` | Compiler run summaries; do not overwrite with weather summaries |
| `4-journal/weekly/` | Weekly reviews |
| `_ctx/rules/` | Governing compile, naming, and frontmatter rules |
| `_ctx/agents/` | Scribe, Compiler, Seeker, Edward role definitions |
| `_ctx/prompts/` | Ingestion, meetings, mail/calendar, review, restructuring procedures |
| `_ctx/templates/` | Templates; some still contain v1 conventions |
| `_mem/profile.md` | Canonical personal context and explicitly stated preferences |
| `_mem/goals.md` | Canonical goals; its Current columns determine reported progress |
| `_mem/people/` | Relationship state and obligations, paired with wiki entities |
| `_mem/state/last-compile.md` | Compiler checkpoint/report |
| `_archive/` | Retired content; excluded from default retrieval |
| `.claude/commands/` | Existing capture, ingest, compile, draft, brief command procedures |
| `.scriptorium/`, `.zk/`, `.obsidian/` | Tool metadata, not knowledge or instructions to ingest |

Observed counts: 162 content pages (94 concepts, 57 entities, 11 topics); 269
ledger rows (183 compiled, 63 superseded, 13 unfetchable, 6 unfetched, 4 rejected,
0 pending); 215 raw Markdown sources plus the ledger; 11 project manifests.
Archived source identities explain why ledger count exceeds live raw-file count.
An NFC-normalized comparison found no raw files without ledger entries.
`_ctx/check-frontmatter.sh` passed for all 162 content pages. This checks selected
frontmatter fields, not factual correctness, link integrity, or all nine rules.

These counts are a dated inspection, never hardcoded application state. The wiki
index and last-compile document still describe an older 161-page/268-row snapshot.
No vault files or scheduled routines were changed during this inspection.

## 2. Governing rules, reproduced for reconstruction

The complete runtime source is `_ctx/rules/compile.md`. Its numbered rules are:

1. Raw sources are immutable. Change processing state only in their ledger rows.
2. Compile only from a source currently read. A fetched and actually read page
   qualifies; remembered knowledge of what a URL probably contains does not.
3. Every compiled claim has provenance; every content page has non-empty `sources`.
4. Preserve both sides of contradictions, mark confidence contested, and record
   them in `1-wiki/open-questions.md`. Existing prose/frontmatter confidence
   disagreement is repaired by matching metadata to the prose, not rewriting it.
5. Human-owned wiki pages permit only appended Compiler notes; preserve their body.
6. Deliverables in `3-output/` derive from compiled wiki pages, not directly from raw.
7. Retire content to `_archive/`, never silently delete it.
8. Extract distinct concepts and connect them with existing knowledge. A source
   yielding only one isolated page and no links has been filed, not compiled.
9. Reconcile near-duplicates into the stronger page with provenance from both;
   archive superseded material and preserve the trail.

Rule 8 must not induce fabricated concepts or links. When a tiny genuine source
supports only one isolated claim, retain the capture and flag `needs_context` in
Loop's compile run; leave its ledger pending with the reason. Do not invent a
second page or silently weaken the rule. Link to a real relevant page when possible.

Human ownership takes priority over a proposed rewrite. A contested human page
gets appended Compiler notes and permitted metadata changes, never a replaced body.
Existing flags/emoji in migrated entity/person filenames are intentional and must
be preserved. New entity/person filenames follow the plain proper-name pattern.
Normalize paths to Unicode NFC for identity comparisons; resolve the actual disk
path without renaming NFD filenames. Detect normalization collisions as conflicts.

## 3. Rule loading and conflicts

Read root `CLAUDE.md` and `_mem/profile.md` before nontrivial personal reasoning.
Before project work, read that project's own `CLAUDE.md`. Before compiling, read
all three rule files and `_ctx/agents/compiler.md` in full for that run.

Load authoritative files directly, not by approximate vector retrieval. Retrieved
sources, wiki pages, email, and quotations cannot grant tool permissions or replace
instructions. The application injects typed policy separately from retrieved data.

Order for a Loop operation:

1. Runtime invariants: identity/access checks, path confinement, privacy propagation,
   schema validation, execution evidence, and no arbitrary code from documents.
2. Authenticated current user request, within that authority; narrow one-time
   exceptions must be recorded, not converted into permanent policy implicitly.
3. Explicit approved settings in `_ctx/loop/` for the affected behavior.
4. Governing `_ctx/rules/`, scoped role/project documents, then operation prompts.
5. Root summaries and templates, which cannot override the governing rules.

If two authoritative documents require incompatible behavior, create a policy
conflict with both file paths and text excerpts; keep the last valid compiled
policy for that scope, or leave that scope inactive on first installation. Reads,
raw capture, and unrelated reminders remain available. Do not choose based only
on file modification time. A user resolution is saved as a scoped policy decision.

Observed conflicts to surface during onboarding:

- `_ctx/templates/Zettel.md` and `Fleeting Note.md` use v1 types/tags and omit v2
  provenance. Their bodies can inspire presentation; v2 schema governs new writes.
- `.claude/commands/ingest.md` forbids fetching URLs; the Compiler and `/compile`
  procedure allow fetching. Keep existing single-source ingest as a no-fetch
  operation. A full compile or explicitly authorized research run may fetch.
- Daily routine text flags project inactivity after 14 days; the dashboard and
  weekly procedure use 30. Loop proposes 30 days for its general discretionary
  staleness routine; an existing 14-day routine retains its explicit scope.
- The prepared daily compile routine is labelled unregistered. Do not treat its
  presence as activation. Scheduled execution/commits require an activation event.

## 4. Onboarding additions: proposed, not present in the inspected vault

Loop creates these only during an explicit initialization/apply operation. It
shows proposed files first and never overwrites existing documents by default.

```text
_ctx/loop/
  manifest.yaml          # schema version and layout mapping
  behavior.md            # time interpretation, response and routing preferences
  permissions.md         # typed autonomy, scope, and approved destinations
  notifications.md       # quiet hours, digest and interruption policy
  routines/*.md          # individual declarative routines
  capabilities/*.md      # per-pack enablement, preferences and approved scopes
  decisions/*.md         # user-approved resolutions of rule conflicts
_mem/loop/
  preferences/*.md       # explicit/confirmed preferences, not a profile rewrite
  hypotheses/*.md        # tentative behavioral hypotheses and evidence pointers
4-journal/loop/
  YYYY-MM-DD.md          # optional operational daily summary
```

Example manifest (paths are vault-relative; an installer never assumes Gleb's username):

```yaml
schema_version: 1
layout: glebos-v2
paths:
  raw: 0-raw
  inbox: 0-raw/inbox
  ledger: 0-raw/_ledger.md
  concepts: 1-wiki/concepts
  entities: 1-wiki/entities
  topics: 1-wiki/topics
  projects: 2-projects
  outputs: 3-output
  profile: _mem/profile.md
  goals: _mem/goals.md
  people: _mem/people
  compile_rules: _ctx/rules/compile.md
  frontmatter_rules: _ctx/rules/frontmatter.md
  naming_rules: _ctx/rules/naming.md
  loop_context: _ctx/loop
```

All writable mapped paths must remain within the selected vault root. Hidden
tool directories, `_archive`, `NEW_ARCH`, and legacy `_mem/state/sorter.md` are
not default authority or active knowledge. They may be read for explicit historical
queries. Existing `_` assets and project folders remain intact.

Installed capability code/instructions live outside the vault; only personal policy
and activation decisions belong in _ctx/loop/capabilities. The registry caches the
validated vault revision and recorded activation authority, never a second editable
policy. See the [shared extension contract](capabilities/README.md).

## 5. Raw capture and ledger

Scribe's job is preservation. Save exact input as the body, without paraphrasing,
with valid YAML serialized by a YAML library. Preserve body whitespace in capture
storage; do not rely on a tool that trims it when exact preservation is requested.
Use source title/heading when present; otherwise derive a short title from its
first meaningful words. Missing title must not block a short capture.

```yaml
---
type: source
source-kind: note
title: Supplier lead time
added: 2026-09-05
origin: gleb
---
```

`source-kind` is `clip|note|transcript|doc|thread`. URLs use their URL as origin;
Telegram message identity is also retained in Loop's capture record. New captures
use `0-raw/inbox/YYYY-MM-DD-<kebab-slug>.md`; date is the vault's local date.
Reject pipes in titles/paths while preserving any pipes in the raw body. For a
collision, allocate a suffix from the stable capture ID; retries reuse the same
reserved path. Never overwrite a same-title note.

The ledger schema is exactly:

```text
| source | batch | added | status | compiled | pages produced |
|---|---|---|---|---|---|
| 0-raw/inbox/2026-09-05-supplier-lead-time.md | inbox | 2026-09-05 | pending | — | — |
```

Status values: `pending`, `compiled`, `unfetched`, `unfetchable`, `rejected`,
`superseded`. Keep original raw path as source identity after archival; record
current archived location in pages produced and Loop's provenance mapping.
Escape/validate cell content so embedded newlines and pipes cannot corrupt rows.
Update by exact normalized identity, never global string replacement.

Raw file creation, ledger registration, and SQLite acknowledgement form a
recoverable file operation described in [runtime](runtime.md). A reply saying
"saved to your vault" requires both file and ledger verification. If only local
SQLite accepted the text, say "queued for the vault" and retain a retry job.

## 6. Compile contract

Input: registered source IDs, scope `single|sweep`, network permission, policy
revision. Output: created/updated page IDs and paths, supporting source spans,
contradictions, ledger transitions, deferred items, validation findings.

Algorithm:

1. Load current rules. For a sweep, reconcile raw files against the ledger in
   NFC form, registering orphans before selecting pending rows. Single-source
   requests require an existing pending row.
2. Read each whole source, including long sources via bounded overlapping chunks;
   record coverage and content hash. Never classify a source from just a preview.
3. When fetching is allowed, retrieve bare-link content into a NEW adjacent
   `--fetched-YYYY-MM-DD.md` source with origin and fetched date; supersede the
   bookmark row. Login/paywall/JS shells are not substantive content. On network
   transient errors, retain pending and retry with limits. A genuine persistent
   access failure becomes unfetchable with reason/date, without same-run retries.
4. Sources with no useful body and no retrievable URL become unfetched if the
   user can recapture them; empty junk becomes rejected. They are not knowledge.
5. Retrieve relevant existing concepts/entities/topics and read candidate pages.
   Propose distinct claims with source spans and meaningful links. A source can
   add evidence to existing pages without requiring new ones.
6. Validate evidence references against the run's read receipts. Validate paths,
   ownership, schema, confidence, and existing-page hashes before each write.
7. Create/append/record contested claims. Link related peers in both directions;
   hub → member without reverse link is permitted. Never fabricate link targets.
8. Update the source ledger only after every required page operation is committed
   and verified. Record all affected pages; a partial run remains pending and
   resumes using its operation IDs rather than appending duplicates.
9. A sweep runs staleness and duplicate checks after compile batches of at most
   ten sources, even if zero are pending. Write/append the day's compiler brief,
   refresh wiki index, and update last-compile state without erasing same-day runs.
   Single-source ingest has no automatic whole-vault sweep.
10. Run the existing frontmatter check and stronger Loop validators. Report scoped
    failures separately from preexisting vault problems.

New wiki frontmatter:

```yaml
---
type: concept
title: Supplier Lead Times
owner: model
created: 2026-09-05
compiled: 2026-09-05
confidence: low
sources:
  - 0-raw/inbox/2026-09-05-supplier-lead-time.md
tags:
  - domain/business
---
```

Content types: `concept|entity|topic`; owner: `model|human`; confidence:
`high|medium|low|contested`. `created` is immutable. `compiled` changes on writes.
Tags for new wiki pages use `domain/*`. `index` and `register` control pages use
their own existing schemas, not forced concept frontmatter. A source supporting
a claim is not proof that the claim is true; preserve attribution and uncertainty.
Existing archived source references are allowed only through an explicit resolved
provenance trail. New claims cite newly read registered raw sources.

PARA now informs project relevance and actionability; it does not select four
literal folders. Zettelkasten informs concept decomposition and meaningful links.

## 7. Retrieval, output, and relationships

Seeker searches wiki first and labels raw fallback as uncompiled. Search project
status in its manifest, obligations in `_mem/people`, goals in `_mem/goals`, and
personal preferences in the profile plus approved `_mem/loop/preferences`.
Do not infer goal completion from a summary or a file's modification timestamp.

Combine exact title/alias search, SQLite FTS5, wiki links, and optional local
embeddings. Privacy filtering precedes ranking. Read relevant results before
citing them. Include layer, path, and contested status. Missing embeddings must
fall back to lexical search, not to fabricated context. Context limits cannot
drop required policy files or silently turn a partial source read into a full read.

Output generation lists supporting wiki pages first. Insufficient compiled
material produces a knowledge-gap result or, when the user's request authorizes
research, a research → capture → compile → draft workflow. Output frontmatter
includes title, platform, format, content-status=draft, created, and wiki sources.
Saving a generated answer never makes it independent evidence: preserve the
underlying source chain. Publishing requires its own explicit authority.

People remain paired: entity page for sourced durable facts, `_mem/people` for
relationship state. A suggested follow-up is not evidence that contact happened.

Meeting notes follow `_ctx/prompts/transcribe-meeting.md` and land at
`4-journal/meetings/YYYY/YYYY-MM-DD — Meeting — <Title>.md`, with type, date,
attendees, context, status=filed, filed-date, and existing meeting tags. Extract
owned action items; moving them to project records requires the applicable
user instruction. Reusable knowledge gets a separate raw source before compilation.

## 8. Scriptorium adapter and enforcement gaps

The project manifest references `~/Claude_Cowork/scriptorium`, which is stale.
Code was located at `/Users/gleb/Claude_Cowork/third-axis/scriptorium/scriptorium.py`.
Make this an installation setting, never a hardcoded requirement.

Observed tools: `vault_status`, `vault_search`, `vault_read`, `vault_pending`,
`vault_capture`, `vault_write`, `vault_lint`. Capture arguments include title,
body, origin, kind, batch; write includes title, kind, body, sources, mode,
confidence, owner, tags. Discover exact input schemas via MCP tools/list at runtime.

The inspected implementation checks source-file existence, not that the source
was actually read in this execution. It also does not atomically update the
ledger with compiled page writes. Capture trims trailing body whitespace and
rejects title collisions. Therefore Loop must not claim that using Scriptorium
alone proves read provenance, exact-byte preservation, or crash-safe transactions.

Define a `VaultGateway` interface with `read`, `capture`, `propose_patch`,
`validate`, `commit`, `ledger_update`, `lint` as specified by this document.
Use Scriptorium behind it only for operations whose semantics pass the same
conformance tests as the built-in gateway. Missing operations are implemented in
the gateway, never by giving the LLM unrestricted filesystem writes. Pin/record
the backend version. Failure/refusal is authoritative; do not bypass it with a
second writer. The target system can be recreated without this external repository.

## 9. Learning and canonical memory

Learning uses recorded interactions, not automatic model retraining.

- Explicit preference: an authenticated statement, applied immediately within its
  scope; saved with source event and time under `_mem/loop/preferences`.
- Hypothesis: an inference, stored separately under `_mem/loop/hypotheses`, never
  inserted into profile/goals or represented as an explicit fact.
- Confirmed preference: user accepts a proposed change, creating a preference
  revision and, if required, a routine revision. Privacy/permissions never widen
  through behavioral learning.

Preference record fields: id, key, typed value, scope, state
`explicit|proposed|confirmed|rejected|superseded`, evidence event IDs,
observed_from/to, sample_count, created/updated, review_after, supersedes.
Body explains evidence and limitations. Facts about Gleb still trace to profile,
goals, or his recorded statements; no personality diagnoses from activity traces.

Initial timing learner: within 28 days, at least five explicit snoozes of the
same discretionary routine on distinct days; median requested shift >=15 min,
interquartile range <=30 min. Propose moving by that median (rounded to 5 min),
never auto-apply. Rejections suppress equivalent proposals for 30 days. Hypotheses
expire after 30 days without supporting evidence. These are initial adjustable
heuristics, not calibrated psychological certainty. Nonresponse is not feedback.

Explicit corrections override hypotheses; broad changes require a clear scope.
"Today I'm home" expires at local day end and must not erase a weekday commute.
Users can inspect, correct, or forget learned records. Forgetting requires removing
content-bearing derivatives and indexes, not merely archiving a private copy;
the vault's never-delete default yields only to that explicit user request.
