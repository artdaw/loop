# Agent stack and capability execution

Normative for specification 1.3. Read with [runtime](runtime.md),
[capabilities](capabilities/README.md) and [acceptance](acceptance.md).
This contract standardizes implementation while preserving all domain invariants.

## 1. Required components and ownership

Use compatible LangChain 1.x and LangGraph 1.x releases, Pydantic schemas,
`langchain-ollama`, `langchain-anthropic`, and `langgraph-checkpoint-sqlite`.
Resolve tested versions into `uv.lock`; do not retain unused legacy dependency
ranges as evidence of integration. No hosted orchestration service is required.

| Component | Responsibility |
|---|---|
| LangChain chat models | Standard messages, async calls, tool calling and structured responses |
| LangChain `create_agent` | Bounded model/tool loop for agent-mode capabilities |
| LangGraph `StateGraph` | Coordinator, workflow execution, agent checkpoints and interrupts |
| ModelGateway and middleware | Privacy, allowed model selection, deadlines, shared budgets, metadata audit |
| Capability registry and invoker | Discovery, typed contract validation, version resolution, runner selection |
| Domain repositories | Authoritative tasks, plans, work items, permissions, operations, artifacts and outcomes |
| Durable scheduler and outbox | Future wake-ups, job leases, delivery attempts and receipts |

LangChain supplies the agent harness and typed response interface; Loop supplies
its policy middleware and domain ports. [LangChain agents](https://docs.langchain.com/oss/python/langchain/agents).

Do not build a second general-purpose agent loop or DAG execution engine in
`Orchestrator.ask`, `LLMRouter`, or a new wrapper. Deterministic command handlers
may call application services directly: listing tasks, completing a task and
firing a reminder must keep working without a model or a graph invocation.

## 2. Stable coordinator, extensible capabilities

The coordinator has generic stages: load authorized context, discover operations,
produce a typed plan, invoke ready assignments, validate results, resolve required
approval, and compose a response grounded in persisted outcomes. It can revisit
stages within the root budget. No weather/travel/pizza-specific edges are allowed.

The invoker resolves a pinned operation and dispatches by the existing three modes:

| Mode | Runner | What a capability author supplies |
|---|---|---|
| agent | `create_agent` with shared middleware and typed tool wrappers | Manifest, instructions, input/output schemas, allowed tools and examples |
| workflow | Declarative DAG compiled to `StateGraph` | Validated steps invoking registered operations and output mapping |
| adapter | Registered trusted handler | Provider integration or optional compiled LangGraph subgraph behind the same contract |

A new agent or workflow pack MUST require zero changes to coordinator, channel
handlers, settings classes or core domain schema. A new external provider may
require adapter code and credential configuration. Native subgraphs are optional
for complex behavior; they do not introduce a fourth mandatory pack mode.

Discover metadata before loading instructions. Filter by enabled version, role,
privacy and available dependencies before model disclosure. Shortlist relevant
operations; do not inject the entire catalog into every prompt. Build typed
LangChain tool wrappers from a validated registry snapshot when constructing the
agent, then filter that snapshot per call. Map operation IDs to unique
provider-compatible tool names and retain the reverse mapping. Runtime context
(owner, authority, privacy, root budget, cancellation and operation IDs) is injected
by code and MUST NOT be accepted as model-controlled tool arguments.

Every wrapper goes through the same invoker and executor, including read tools.
Authorized reads may run during planning; proposed mutations are validated and
gated before execution. Output schema validation does not establish factual truth:
source receipts, freshness and effect evidence retain their existing requirements.
Unavailable tools return a typed limitation; the task is still retained.

Use isolated per-invocation subgraph state. Parent checkpointing must cover child
execution; tests must prove restart and concurrent-call isolation. Dynamic graphs
called through tools are not necessarily visible to static graph inspection, so
record explicit child run IDs and checkpoint references for status and cancellation.
[LangGraph subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs).

## 3. ModelGateway and policy enforcement

Replace direct provider SDK routing with LangChain `ChatOllama` and `ChatAnthropic`
adapters. Keep ModelGateway small: it enforces policy around standard model calls,
not a competing model API. Apply the same policy middleware to every agent call,
summary, schema repair and child run. Local embeddings retain local-only policy.

Merge privacy from the complete context before every model invocation. A caller's
`local_only: false` cannot lower a private label. Private local-model failure is
terminal or visibly deferred; it never falls through to cloud. Cloud requires
all configured authorization and budget conditions. Short answers and latency
are not confidence measurements and MUST NOT automatically trigger escalation.

Use Pydantic structured output through supported LangChain interfaces. Validate
the configured model's actual tool/structured-output support with fake protocol
tests and optional separately configured provider smoke tests. Unsupported model
features produce a clear configuration limitation, never implicit cloud use.
Any bounded repair attempt counts against the original budget.

One shared accounting service reserves budget before calls and records usage
afterward, including parallel children and failures. Disable hidden provider
retries; explicit graph/job retry policy must not multiply retry allowances.
Privacy denials and invalid authority are non-retryable. Recursion limits supplement
the runtime's time, token, tool and assignment limits; they do not replace them.

## 4. Persistence and recovery

Use `AsyncSqliteSaver` for the initial single-instance local deployment, stored
at `data/graph-checkpoints.sqlite`. Domain SQLAlchemy storage remains separate.
The SQLite checkpointer is a local deployment choice; a future multi-worker
deployment must explicitly select and test an appropriate backend.
[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence).

Persist a domain run mapping containing root run ID, graph thread ID, graph and
state-schema versions, registry snapshot/hash, policy revision, checkpoint
reference, privacy label and lifecycle status. A graph thread belongs to one
root run, not the entire Telegram conversation. Domain plans/work items are
authoritative business records; graph checkpoints contain execution position and
references to them. Do not maintain two independently advancing work schedulers.

Only one leased worker may advance a thread at once. The durable job dispatcher
wakes/resumes graphs; graph nodes execute ready assignments. Graph state contains
typed identifiers and minimal necessary messages/artifacts, not open clients or
credentials. Reload current domain state before effects and on resume.

There is no atomic transaction spanning the domain DB and checkpointer. Each
effect receives a stable operation key derived from persisted run/work/step IDs.
On replay, consult the operation ledger, reuse completed results, reconcile
incomplete work, and preserve `unknown` remote outcomes. Never infer successful
delivery from a graph checkpoint. Crash tests cover both commit orders.

Persist the bound approval request idempotently before calling `interrupt`.
Waiting releases worker leases and consumes no model calls. An authenticated
decision creates a durable resume job; use `Command(resume=...)` for the same
thread. Resume rechecks actor, payload hash, version, expiry, cancellation and
current permissions before executing. Interrupt nodes restart from their beginning,
so pre-interrupt code must be replay-safe; do not catch an interrupt as an ordinary
failure. [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts).

Pin graph, state-schema and capability versions for active runs. New versions
apply to new runs. If an old graph/package is unavailable, pause with an actionable
status; never deserialize into an incompatible graph silently. Migration requires
an explicit backed-up transformation with restart tests. Disable/revoke/cancel
signals are checked again before every tool call and effect, including on resume.

Checkpoints can contain private content. Keep them local with restricted file
permissions, exclude secrets, and propagate labels to derived state. LangSmith
and other remote content tracing are disabled by default; enabling telemetry
must not bypass destination policy. Audit logs remain metadata-only. Forgetting
must invalidate affected active runs and purge retained checkpoint content under
the same retention policy as its source. Backups pause both writers and capture
both databases with a version/hash manifest; restore checks their consistency.

## 5. Migration and proof

Preserve Stage A domain work and the legacy compatibility surface while replacing
model transport and orchestration incrementally. Add `loop/ai/model_gateway.py`,
`loop/agents/coordinator.py`, and generic runners under `loop/capabilities/`.
Introduce the gateway before Stage B's model-driven Compiler; integrate the full
coordinator and runners in Stage C. Do not postpone privacy enforcement until C.

Tests use real LangChain/LangGraph execution with injected fake chat models,
temporary SQLite checkpointers and fake domain adapters. Replacing the entire
graph with a mock does not prove conformance. The LG scenarios in acceptance.md
are mandatory in addition to all existing domain scenarios. Demonstrate adding
two unrelated capabilities, checkpoint/restart, pending approval/resume, private
model failure, and duplicate-effect prevention through the actual framework path.
