# Adding capabilities to Loop

**Contract:** capability-pack/v1 · **Status:** normative target. The CLI/runtime
described here must be implemented; these docs do not install executable plugins.

Read [main](../../SPECIFICATION.md), [runtime](../runtime.md),
[interfaces](../interfaces.md), and the relevant domain contract:
[travel](travel.md) or [weather](weather.md). Start a new pack with the
[copyable template](template/README.md).

## 1. The extension promise

Adding a capability MUST NOT require changing Coordinator routing, database core,
Telegram handlers, web routes, or the scheduler. Those components use a shared
registry, generic invocation, typed artifacts, jobs, gates and notifications.
Domain shortcuts such as /travel are optional aliases for registered operations.

Three extension paths:
1. **Instructions only:** describe the task and input/output schemas; use the
   existing role, model router, and approved tools. No Python code needed.
2. **Composition:** define a bounded workflow of existing operations. No new
   provider or bespoke scheduler needed.
3. **New integration:** add a tested adapter with transport/normalization and
   capability contracts. The rest of the runtime stays the same.

“Any capability” means open-ended domains through these paths. A skill cannot
invent access to a website, device, private account, payment API or unavailable
tool. Missing dependencies are explicit. Deployment ceilings and execution
invariants still apply to every extension.

The normal authoring process is create → validate → test → enable. The user can
also describe a desired capability in chat; Loop produces the same reviewable pack
files, not hidden self-modifying runtime code. Drafting a new adapter may produce
code for review, but cannot execute or install that code automatically.

## 2. Pack layout and authority

Installed packs live outside the knowledge vault, in configured CAPABILITY_PATHS.
Each immutable version has this layout:

```text
<pack-id>/<version>/
  capability.yaml           # metadata, operations, bindings and declared effects
  instructions.md           # required for agent operations
  schemas/
    input.schema.json
    output.schema.json
  examples/
    cases.yaml               # sample inputs, mocked results and required assertions
  workflows/*.yaml           # optional declarative composition
  adapters/                  # optional reviewed implementation + tests
```

A single operation can reuse the template's filenames. Larger packs supply one
schema/instruction file per operation or shared schemas with local references.
Resolve relative paths against this pack version; refuse traversal/symlink escape.
Schema refs resolve only within the package; validation MUST NOT fetch remote
schemas. File size/dependency depth limits apply before loading.

Personal enablement, preferences and allowed scopes live in the proposed
_ctx/loop/capabilities/<pack-id>.md plus the existing behavior/permissions files.
Credentials stay in private deployment storage. The pack itself cannot grant
permissions or change global policy by declaring effects.

Pack IDs and operation names use lowercase letters, digits, underscore and dots,
begin with a letter, max 80 characters. Versions use major.minor.patch numeric
components; breaking public schemas require a major version. Existing operation
names, including weather.forecast, remain stable where compatibility is promised.
A new pack cannot replace another pack's operation name, even if disabled.
Built-in operations reserve their namespaces in the registry.
Weather/travel operations are owned by their feature packs even when bundled with
the application; do not register a duplicate built-in implementation beside them.

## 3. Manifest contract

capability.yaml is YAML with schema_version=1. Required fields:

| Field | Meaning |
|---|---|
| id, version, title, description | Unique identity, immutable release, user-visible purpose |
| owner_role, support_roles | Existing registered role IDs; additional roles optional and explicitly registered |
| policy_namespace | Namespace for personal behavior defaults |
| defaults | enabled=false and budget_class interactive/background/research |
| dependencies | required/optional operation names with explicit compatible version constraints |
| operations | Map of globally unique operation names to definitions |

Operation fields:
- description and intents: concise purpose plus example utterances for discovery.
- input_schema, output_schema: package-relative JSON Schema files.
- mode: agent/workflow/adapter.
- instructions: relative file for agent mode; workflow: relative YAML for workflow
  mode; handler: registered adapter binding ID for adapter mode. Exactly one applies.
- tools: operation allowlist; empty allowed. Listed tools must be declared dependencies.
- effects: set from local_artifact_write, local_domain_write, vault_write,
  network_read, owner_notification_proposal, remote_write, spend.
- destinations: allowed connector IDs/resource scopes required by those effects;
  empty for pure local work. These declare requirements, not grants.
- timeout_seconds, retry_policy (none/transient), idempotency
  (root_effect_slot/provider_key/reconcile), and result_artifact_kind.
- default_arguments: optional validated defaults, never credentials or authority.
- aliases: optional CLI/Telegram shortcut metadata; cannot shadow built-in commands.

Every operation gets a typed, sanitized success/error envelope from the runtime.
Server-owned privacy, approval, object versions and budget cannot be supplied as
model-controlled authority. JSON Schemas MUST constrain unexpected object fields
and distinguish optional values, null and missing. Agent output is validated before
it becomes an execution proposal. Validation errors get the shared bounded repair.

A document may show a shortened pack overview, but a runnable pack must include
every required manifest field, schema, instruction/workflow/handler binding and
example. The [template](template/capability.yaml) is a complete minimal manifest;
the weather/travel documents define their domain contracts for implementation.

## 4. Discovery, invocation and availability

The loader scans configured directories at startup, validates without importing
adapter code, and produces a registry snapshot keyed by operation + version +
package content hash. Metadata discovery requires no network, model or credential.
It accepts a root containing capability.yaml or versioned <pack-id>/<version>
children within the configured discovery depth; it does not crawl arbitrary home
directories. Scaffold output must be in a configured root before enable can find it.
Changed pack files create a proposed version; active bytes are never hot-swapped
mid-run. Exactly one enabled version of each operation is used for new work.
Changed content claiming the same immutable version is rejected; the author must
bump the version. Preserve a verified copy/hash for pending jobs before activation.

A domain request retrieves relevant registry descriptions first, then loads only
selected operation schemas/instructions. Explicit operation commands skip intent
selection. Coordinator may propose known dependencies; the executor still validates
scope, DAG, arguments and budgets. The catalogue in runtime is a documented
starting set, never a closed enum requiring core edits for every new skill.

Availability: discovered, invalid, disabled, missing_dependencies,
permission_required, ready, degraded. A ready manifest does not imply that every
optional live provider is configured. Unsupported optional subfeatures remain
visible in results. Missing a required tool blocks execution before the model
promises success. Validate cycles across packs; indirect dependencies cannot evade
parent limits. Tool permissions do not expand through chaining.

Generic target commands:

```text
loop capability init PACK_ID --role daily_life --mode agent --output PATH
loop capability list [--json]
loop capability inspect PACK_ID
loop capability validate PATH
loop capability test PATH --offline
loop capability enable PACK_ID --version VERSION
loop capability disable PACK_ID
loop capability run OPERATION --input-json PATH [--request-id UUID]
loop capability upgrade PACK_ID --version VERSION --dry-run
loop capability upgrade PACK_ID --version VERSION --apply
```

init copies the starter structure and substitutes declared IDs/role, writes only
the selected destination, and never overwrites existing files. validate reports
all schema/reference/name/dependency/effect problems without running code. test
uses fake tools/models and denies network, live credentials, vault and side effects
outside a temporary test workspace.

enable validates the exact version and shows its requested effects/dependencies.
An explicit enable command authorizes registration; execution still needs the
user request or standing scoped permission. No second permission prompt for already
authorized local/read work. New spending/remote recipients use existing scoped
approval, never a blanket “trust plugin” exemption.
disable stops new invocations and cancels queued dependent work/notifications;
in-flight work checks cancellation before effects. Already completed work remains.

HTTP /api/v1: GET /capabilities, GET /capabilities/{pack_id},
POST /capabilities/{operation}/invoke {arguments}, and
POST /capabilities/{pack_id}/enable or /disable {version,expected_registry_revision}.
Use the existing authentication, Idempotency-Key, async run and error envelopes.
Telegram /capabilities lists supported operations; /do <operation> <request> routes
to that operation's validated schema. Clarify missing fields through the normal
needs_input workflow. UI renders input schemas, result summaries and source links
without adding bespoke routes. Domain UIs may enrich this later.

## 5. Shared execution and persistence

The runtime supplies injected ports for models, Clock, labelled retrieval,
application services, provider clients, VaultGateway, operations/outbox and
artifact storage. Plugins cannot open arbitrary files, dispatch raw Telegram
messages, make direct model calls, or create private background loops.
A native adapter is reviewed installed code, not magically sandboxed Python:
run it with only injected credentials/ports, and isolate untrusted code in a
separate constrained process if ever supported. Never claim a schema sandbox
alone isolates malicious plugin code.

Workflow syntax v1: steps[] with unique id, operation, arguments, depends_on[];
output mapping. Values are literals or {$input: field_path} / {$step: id,
path: field_path}. Paths select existing typed fields only; no eval, shell, SQL,
arbitrary expressions or executable Jinja. Validate DAG and output schema before
running. No loops in v1; bounded child assignment/revision uses core work APIs.
Steps share parent privacy, authority, root effect slots, budgets and deadline.

Most new capabilities persist immutable typed artifacts plus existing task/routine/
observation objects. No new table is required. If a domain needs queryable durable
state, use capability_objects: pack_id, object_type, object_schema_version,
payload_json, privacy and common id/version/timestamps. Validate payload against
a registered schema and use expected_version. Existing dedicated trips state
remains supported. Derived indexes can be added without changing core interfaces.

The registry persists approved package path/hash/version, status, active revision,
policy reference and validation errors. Work items/artifacts pin operation_version,
package_hash and input/output schema hashes. Queued jobs resume with the same
version or pause visibly if unavailable; never silently run a newer contract.

Upgrades show schema/effects/dependency/policy differences. Compatible changes
activate for new runs only. Migrations are explicit, backed up and reversible
where possible; retain old versions required by pending work. Removing a tool
pauses dependent routines rather than deleting their commitments. Rollback selects
a validated previous version; it cannot undo external side effects.

## 6. Author tests and reproducibility

Each pack ships successful, invalid-input, missing-dependency, timeout,
privacy-propagation and effect-scope examples appropriate to its operations.
Side-effecting packs also test retry/replay and uncertain-effect reconciliation.
A new pure-local pack can reuse the base conformance suite instead of writing
six copies of boilerplate tests. examples/cases.yaml supplies input, mocked output,
expected status, required properties and allowed tool/effect trace.

The starter example is deliberately local: turn a request into a proposed checklist.
It demonstrates loading, schema validation, output artifacts and reusable base
tests without a live provider. Domain reasoning is not “tested” merely because
a mocked output matches a schema; add synthetic evaluable cases for that domain.

Acceptance EX01–EX14 in [acceptance](../acceptance.md) require installing two
unrelated fixture packs and running both through unchanged CLI/API/bot/core code.
Weather and travel MUST use the same registry and lifecycle. Their richer schemas
and adapters cannot become mandatory boilerplate for a simple new capability.
