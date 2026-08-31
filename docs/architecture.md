# NZ-Coder Architecture

NZ-Coder is a terminal coding-agent runtime built around a small set of explicit subsystems.

## Runtime Flow

```text
user input
  -> AgentRuntimeAssembly (coding or explicit declared graph)
  -> optional admission audit for untrusted declared graphs
  -> ProductRunEnvironment capability host (AgentLoop only for deprecated compatibility callers)
  -> AgentRunner.run
  -> ProductionRuntimeHost (workspace/session/MCP/ContextVar scope)
  -> AgentRunner._run_turns (single Main/child/background/workflow kernel)
  -> Session-owned optional MCP stdio / Streamable HTTP / legacy SSE runtime
  -> configured provider (OpenAI-compatible / Responses / Anthropic / Gemini)
  -> tool calls
  -> consecutive identical-call guard
  -> permission check
  -> tool dispatch
  -> transaction/change tracking/trace logging/session events
  -> tool results back to model
  -> final answer
```

## Core Components

- `runtime/execution/runner.py`: owns the single production turn state machine used by
  Main, child, background and workflow execution.
- `runtime/execution/host.py`: binds workspace, Session, MCP, memory, skills, tool state,
  background manager and interaction callbacks around one Runner invocation.
- `runtime/execution/loop.py`: compatibility and coding-capability adapter; it retains
  mature prompt/materialization/verification/handoff policies but no longer
  owns a production turn loop.
- `runtime/model_gateway/`: owns Provider resolution, retry, timeout,
  cancellation, streaming fallback, normalized errors, usage and cost.
- `runtime/tool_runtime/`: owns read concurrency, side-effect barriers,
  transaction settlement and complete tool-batch post-processing.
- `runtime/conversation/context_manager.py` and `runtime/session/session_repository.py`: own
  model-window preflight/compaction triggering and durable checkpoints.
- `tools/`: exposes built-in tools plus context-local dynamic MCP tools through a function-calling registry.
- `mcp/`: owns layered local/remote configuration, project-command trust,
  stdio/Streamable HTTP/legacy SSE transports, OAuth, live reconcile, and
  context-local tools/prompts/resources.
- `http_service/`: optional authenticated loopback Session CRUD/run/SSE transport and a standard-library client.
- `permissions.py` and `command_policy.py`: classify shell commands and enforce deny/ask/allow behavior.
- `transaction.py`: snapshots edited files during multi-tool write rounds and rolls back if any write fails.
- `changes.py`: records agent-authored before/after file snapshots for `/diff` and `/revert-last`.
- `trace.py`: records JSONL events for each run so failures can be inspected after the fact.
- `session_events.py`: exposes ordered instance-local lifecycle events to clients and SSE transports.
- `sessions.py`: saves and restores conversation history for resume workflows.
- `benchmark.py`: evaluates the agent across coding-task categories and produces JSON/Markdown reports.

## Functional Module Map

The implementation is now organized into functional package directories under `nz_coder/`. Top-level modules such as `nz_coder.loop` and `nz_coder.eval_runner` remain as compatibility wrappers for existing imports and command usage.

### 1. Agent Runtime

Owns the ReAct/CodeAct loop and per-turn control flow.

- `runtime/execution/loop.py`: main agent loop, model calls, effect-aware tool scheduling, ordered side-effect barriers, verification gate, compaction, final status.
- `runtime/execution/composition.py`: canonical construction owner used by CLI, HTTP,
  local evaluation, Aider helpers, and SWE-bench. The default `coding` profile
  keeps the native coding loop as the sole control plane; an explicit
  `declared` profile installs one validated AgentGraph instead of silently
  mixing both orchestration styles.
- `runtime/agent/admission.py`: typed admission verdict/handle, system capability
  ceiling, declaration-time tool clamp, per-call Bash capability re-clamp,
  per-run mutation/evidence observation, and terminal invariant assertions for
  untrusted declared graphs. Trusted built-in coding and hand-authored declared
  graphs use their existing path unless the caller explicitly selects admission.
- `runtime/conversation/structured_output.py`: provider-neutral JSON extraction, a focused
  JSON-Schema validator, declaration-time unsupported-keyword rejection, and
  the prompt contract for one bounded no-tool repair turn.
- `runtime/agent/child_result.py`: canonical JSON-safe child terminal result shared by
  foreground `task`, background Agent Manager, change application, evidence,
  and declarative as-tool returns, with legacy metadata projection.
- `runtime/workflows/workflow_process.py`: append-only background task lifecycle journal,
  replay reducer, atomic revisioned snapshot, counts/progress/token projection,
  cross-process run-identity replay, and corruption/truncated-tail recovery.
- `runtime/workflows/workflow_runtime.py`: preflighted declarative workflow execution over
  the same background manager, including phases, bounded parallel lanes,
  barrier-free per-item pipelines, map-reduce, gated synthesis, and a private
  content-addressed successful-result resume cache, token budget, abort, and
  independent sidecar verification.
- `runtime/workflows/workflow_contracts.py`: versioned machine-readable workflow behavior
  contract consumed by results, SDK-facing metadata, and parity tests.
- `runtime/workflows/workflow_manifest.py`: strict workflow declarations and pattern IDs
  checked against the concrete plan before effect admission.
- `runtime/workflows/workflow_run_store.py`: private bounded JSON artifacts, terminal
  `run.json`, and typed usage/coverage efficiency reports.
- `runtime/workflows/workflow_capsule.py`: inert versioned reusable-plan envelopes and
  side-effect-free environment/capability requirement preflight.
- `runtime/workflows/workflow_library.py`: project/personal capsule persistence and
  discovery with project precedence, symlink exclusion, and atomic 0600 writes.
- `runtime/workflows/workflow_resolver.py`: built-in-first capsule resolution, bounded
  saved-capsule argument substitution, and one-level nested workflow expansion.
- `runtime/workflows/workflow_host.py`: safe run/saved/built-in identity resolution,
  command-only invocation policy, min-wins host ceilings, approval summaries,
  display aliases, resume targeting, and the scout-then-author prompt contract.
- `runtime/workflows/workflow_generation.py`: strict JSON decline/generate envelopes,
  Provider-backed authoring, inert Capsule validation, one shared timeout budget,
  and at most two repair calls.
- `runtime/workflows/workflow_sdk.py`: asynchronous managed-run handle with first-started
  and terminal futures plus pause/resume/stop controls for terminal/SDK hosts.
- `runtime/agent/agent_resilience.py`: conservative unique tool-name alias repair,
  tool-result error/cancellation/code classification, Provider Attempt decisions,
  single pre-boundary buffered fallback, and terminal promise-signal extraction.
- `runtime/workflows/workflow_builtins.py`: trusted JSON-only parallel-investigation and
  scoped-review capsules plus six bounded declarative workflow generators.
- `runtime/workflows/workflow_review.py`: immutable supplied-diff review packets and a
  deterministic quality gate that preserves unresolved evidence.
- `runtime/workflows/workflow_sweep.py`: fail-soft cleanup of clean terminal workflow
  worktrees while retaining any child with reported or observed changes.
- `runtime/workflows/workflow_features.py`: registered tools for built-in discovery,
  review-packet capture, and inert workflow generation.
- `runtime/workflows/workflow_lifecycle.py`: safe persisted run/artifact readers and
  confirmed recoverable archival into workflow-private trash.
- `runtime/agent/handoffs.py`: declarative Agent/model/tool/guardrail declarations,
  continuation and as-tool edges, and the execution-local handoff signal.
- `runtime/agent/guardrails.py`: typed input/output/tool verdict contracts.
- `runtime/agent/lineage.py`: append-only Session facts and atomic as-tool caller
  stack recovery.
- `tool_platform/execution.py`: shared tool-result, workspace-mutation, and command-failure contracts used below the orchestration layer.
- `runtime/execution/tool_executor.py`: parses tool arguments, applies permission checks, dispatches tools, and re-exports the shared execution contracts for compatibility.
- `runtime/conversation/prompt.py`: builds the system prompt and tool-use guidance.
- `runtime/execution/runtime_state.py`: state-as-message reminders for task mode, diff state, verification state, idle turns, and Greenfield flow.
- `runtime/verification/recovery.py`: API/tool error diagnostics, retry behavior, canonical identical-call tracking, and conservative recovery prompts.
- `runtime/agent/task_policy.py`: task-mode detection and policy helpers such as broad-test detection.
- `runtime/agent/subagent.py`: isolated child-agent execution with restricted tool sets and scratch output.
- `runtime/process/workdir.py`: context-local workspace and derived artifact-directory selection.
- `runtime/core/execution_context.py`: context-local max-turn, timeout, scheduler-limit overrides and the mutable broad-test guard.
- `providers/capabilities.py`: immutable model-family metadata, registry and exact local catalog overlays, reasoning variants, prompt-family guidance, and request normalization.
- `providers/models.py` and `providers/registry.py`: explicit provider discovery,
  bounded offline caches, workspace model selection, and models.dev-compatible
  capability synchronization.
- `providers/openai_compatible.py`, `providers/openai_responses.py`,
  `providers/anthropic.py`, `providers/gemini.py`: protocol adapters sharing the
  capability contract while preserving native wire formats.

`config.py` provides process defaults loaded from the environment; it is not a
per-run state store. `AgentLoop.run()` binds the agent workspace, session, file
transaction/change tracker, memory, skills, interaction callbacks, parent-agent
metadata, and broad-test guard through nested context managers. Evaluation,
HTTP Session service and SWE-bench entry points use the same context-local workspace/runtime
overrides instead of temporarily assigning module-level config attributes.
Scheduler workers explicitly copy the current context, and POSIX SWE-bench fork
attempts inherit the bound context at process creation.

Production entry points do not instantiate `AgentLoop` independently. They all
call `build_coding_agent()`, so provider/session/tool/memory ownership cannot
drift between terminal, HTTP, and evaluation products. Custom multi-role users
must call `build_declared_agent(graph)`. Externally supplied or model-generated
graphs first call `admit_agent_graph(graph, system_cap)` and pass the successful
typed handle to `build_admitted_agent(handle)`. The assembly rejects a coding profile
that also supplies a graph, and records `runtime_profile`, `control_plane`, and
`active_agent` in trace, lineage, and the returned runtime summary. Admission is
min-wins: it can remove tools and lower the run iteration ceiling, never widen
the host cap. The executor repeats the capability check on concrete calls, so a
generic admitted `bash` tool cannot turn into an undeclared network command.
Committed mutations and successful verification artifacts are observed in a
run-scoped invariant session; completion from a non-final owner or a mutating
deliverable without verification evidence is durably rejected as `blocked`.

A terminal declared Agent may add `output_schema`. The graph validates that the
schema uses the implemented subset and that the role is a terminal owner. The
runtime appends the schema contract to that role's prompt, parses the final
fenced JSON without changing Provider adapters, and performs at most one
transcript-seeded repair request with an empty tool list. Only schema-valid data
is exposed as `result["structured"]`; as-tool returns carry the same value in
their synthetic caller message. Invalid repaired candidates remain observable
in trace/runtime status but are never published as validated data.

Every child execution surface publishes the same `ChildAgentResult` wire shape:
task/name/status/final text and identity are always present; structured output,
verification, usage/cost, changed files, conflicts, provider/model,
interruption, and limit state are typed optional facts. Foreground and
background task runners remain separate lifecycle owners because background
execution also owns slots, cancellation, and isolated change application; they
do not invent separate result contracts. Persisted legacy `child_*` metadata is
read through an adapter, while new tool results expose both the canonical
nested envelope and compatibility aliases. Display-only session/worktree
annotations never enter canonical `final_text`.

Child task declarations can additionally carry a semantic `model_hint`, bounded
`evidence_refs`, and a machine-checkable `verification` contract. Model tiers
resolve before Provider execution and persist route outcome/source/fallback,
model identity, iterations, usage, and duration. Evidence references accept only
`file:`, `diff:`, `finding:`, and prior `task_id:` sources; path refs remain inside
the parent workspace, prior tasks must exist, and the final briefing is explicitly
untrusted and budgeted. Verification observes durable ToolParts and changed-file
state for mutation, exact changed/read paths, final-text length, and preparatory
answers. A hard miss receives at most one same-Session repair attempt; warn misses
settle as `completed_unverified`, while a repeated hard miss settles as
`verification_failed`. Full final text remains the synthesis/audit source, and a
separate bounded `excerpt` is the terminal/background presentation summary.

The background Agent Manager also owns one revisioned workflow process for the
parent Session. Task queue/start/cancel-request/terminal transitions are fsynced
to a 0600 JSONL chain before the atomically materialized snapshot is replaced.
`agent_manager status` exposes that snapshot and `action=events` returns a replay
suffix after a caller revision. Snapshot loss is recoverable from the journal;
a truncated final record is treated as an interrupted append, while corruption
or a broken sequence/parent chain in a complete record rejects recovery. Child
state files remain authoritative for worktree/tool execution, so manager startup
reconciles their terminal state into the process log instead of inventing a
second mutable lifecycle source.

Workflow control uses the same owner. `action=wait` accepts one or more owned
task IDs, preserves caller order, and applies one shared deadline across the
whole batch. A timeout requests cooperative stop for every remaining live task
and performs one bounded settle wait. `action=stop` is idempotent, records the
reason once, and returns typed settled/unsettled task IDs plus the current
workflow snapshot. Fan-out admission separates the parent workflow's lifetime
`maxAgents` cap from its live `maxConcurrency` cap. The complete batch is
validated and published under one manager lock, so repeated or concurrent
`start` calls cannot oversubscribe lifetime capacity or race overlapping path
claims. Queued tasks acquire a bounded semaphore, ordinary child failures remain
isolated, and `wait` returns results in caller order. A terminal event is durably
published before its execution slot is released, keeping snapshot
`active_agents` and `peak_active_agents` consistent with actual capacity.
The legacy `cancel` action is an alias over the same stop transition. Explicit
terminal events are deduplicated per task. `AgentLoop.close()` requests stop for
every un-awaited process-local child and waits for settlement before closing
Session events and tracing, while still closing those resources if a child
violates the cleanup deadline.

`workflow_run` is the model-facing orchestration entry point. Its plan contains
named `parallel`, `pipeline`, `map_reduce`, or `synthesize` phases; it does not
execute arbitrary Python or shell. A static preflight validates phase order,
task shape, write scopes, concurrency, literal fan-out, remaining lifetime
capacity, and the final synthesis requirement before publishing any child.
Parallel and pipeline failures are isolated per item, while structural control
errors stop the workflow. Pipeline items advance to their next stage as soon as
their own prior stage settles, without a global stage barrier.

Final synthesis is not a hidden Provider call: it starts a read-only deep-tier
child through the same manager and therefore consumes maxAgents,
maxConcurrency, usage, lifecycle events, and canonical child-result contracts.
Workflow-launched children cannot invoke `workflow_run` or `agent_manager`
recursively; this implementation has no implicit nested-workflow authority.
Successful non-synthesis children are cached under a canonical SHA-256 input
key plus occurrence index. Resume reads only a workspace-owned prior run ID,
treats corrupt/non-success entries as misses, and copies hits into the new run;
terminal synthesis always runs fresh over the possibly replayed full results.
An optional fresh deep-tier sidecar then returns accept, revise, or blocked;
revision launches a new synthesis within a bounded retry count, blocked stops
delivery, and verifier transport/schema failure is explicitly recorded as a
fail-open result. Token usage accrues from canonical child results and is
checked before each subsequent spawn. Caller abort stops every active child.
Optional manifests are executable admission contracts rather than display
metadata: phase order, read-only claims, planned/hard Agent limits,
concurrency, token budget, and patterns are cross-checked against the plan and
current Session capacity. Sidecar worst-case revisions are included in the
preflight spawn count.

The manager also retains a bounded process-local run registry. Pause gates only
future Agent spawns, resume releases those gates, and stop cancels every active
child carrying that workflow run identity. Child settlement and final delivery
recheck stop state so cancellation cannot race into a completed result. The
same manager tool exposes run list/pause/resume/stop; no second scheduler owns
these transitions.

Tasks normally execute in context-isolated threads. A task may explicitly ask
for `isolation: process`, which uses a spawn boundary and lets cancellation
escalate from terminate to kill after a bounded grace period. The parent remains
the sole publisher of terminal workflow state. Completed workflow outcomes are
also appended idempotently to Session lineage as bounded digests; raw child
output is deliberately excluded. A versioned workflow contract travels with
the result so consumers can check these semantics without inferring them from
implementation details.
Phase outputs may be persisted as bounded JSON artifacts under the private run
directory, and structured workflow logs append to the existing fsynced event
journal. Every terminal run atomically writes `run.json` with timestamps,
status, artifact references, and an efficiency report aggregated only from
children carrying that run identity. Missing usage is explicit token-coverage
data rather than an invented zero-cost claim.

Reusable workflows are JSON-only capsules. A capsule binds a strict manifest
to a declarative plan plus optional intent, requirements, and provenance; it
cannot contain executable source. Project `.nz-coder/workflows` definitions
override personal definitions, and discovery ignores symlinks and non-capsule
extensions. Execution resolves the capsule, checks version/environment/tool/
MCP/Skill/model-tier requirements, then feeds its plan through the same static
admission and WorkflowRuntime. Capsule provenance is retained in outcome,
lineage digest, and terminal `run.json`.

Persisted history is readable only through validated run identities and
declared artifact references. Retention never recursively deletes a caller
path: after rejecting active runs and preflighting every target, confirmed
archive operations move exact terminal run directories to private `.trash`
and report their recovery locations.

Trusted built-ins resolve before project/personal capsules, so a saved file
cannot shadow security-reviewed orchestration. A plan may invoke one nested
workflow level; expansion and linting happen before any child effect, and the
nested phases execute inside the parent's runtime so Agent count, concurrency,
token budget, abort state, event journal, cache, and run record remain shared.
The built-in parallel investigation fans out read-only structured findings and
uses the normal fresh synthesis gate. Scoped review consumes immutable packets
captured from caller-supplied diff bytes, runs primary and verifier stages,
then applies a deterministic quality gate: refuted findings are removed while
confirmed, unresolved, and unverified requirements survive. Missing structured
review output can never imply approval.

Terminal workflow cleanup is conservative. Clean read-only worktrees can be
removed, but reported changes, observed Git changes, or an unverifiable writable
copy retain the worktree and emit warnings. Cleanup failure cannot rewrite the
workflow terminal result. Workflow generation remains data-only: six declared
patterns produce bounded JSON capsules and never execute generated source.

The host launch boundary is distinct from execution. Natural-language input is
never intercepted by a context-blind pre-Agent generator; only an explicit
command produces a host suggestion, while an Agent may scout first and then
call the existing workflow tool. Manifest, host, and system resource ceilings
use minimum-wins semantics and are enforced by admission/runtime, not merely
shown in UI metadata. The same effective values form an approval summary with
write risk. Historical runs may carry a printable display alias, but identity
resolution fails closed when an alias collides with another run, a saved
capsule, or a built-in. Resume accepts only a unique historical run identity.

Approval decisions bind to the SHA-256 digest of the canonical effective
summary, so a UI cannot approve one limit/write-risk snapshot and execute a
different one. Explicit deny/cancel creates no run; headless execution records
an auto-approval receipt instead of pretending a user approved it. Lifecycle
mutation stays recoverable: saved delete moves to private trash, replace keeps
the prior validated revision, and retention supports a no-write preview.
Active and persisted histories are projected as one deduplicated list, while a
bounded result summary lives in the terminal record.

Generated workflows cross a JSON-only decline/generate boundary. Fenced or
surrounded JSON is extracted, then converted to an inert validated Capsule;
generated executable source remains forbidden. Generation timeout and repair
budgets are bounded. Before any tool consumer sees a model tool call, the main
and child loops repair only a unique case/separator-equivalent name—never fuzzy
matching. Tool outcomes share one error/cancellation/code classifier, and retry
and terminal-signal diagnostics are emitted by the existing trace owner.

Every durable workflow journal append is also projected onto the owning
`SessionEventBus` as `workflow.*`, carrying both the immutable workflow event and
its revisioned snapshot. This is a presentation bridge, not a second fact
source: publish failure cannot undo an fsynced journal record. Because the HTTP
service already exposes that same bus over SSE, phase/task/replay/synthesis
updates require no parallel HTTP protocol.

Model selection is also split into two layers. Provider adapters own transport
and response normalization; `ModelCapabilities` describes the selected model's
window/output limits, tool/stream/reasoning/temperature support, prompt family,
reasoning-history round trip, and token-limit request field. `AgentLoop` binds
one immutable record at construction, uses it for context budgeting and request
policy, and records the resolved values in `run_start`. Known families have
conservative built-in metadata. An explicitly synced workspace registry may
supply exact public metadata; `MODEL_CATALOG_JSON` or workspace-bounded
`MODEL_CATALOG_PATH` records take precedence over registry and family rules.
Workspace selection chooses provider/model/variant without mutating global
config; `MODEL_VARIANT` remains the environment default.
Explicit `MAX_CONTEXT_TOKENS`, `MAX_OUTPUT_TOKENS`, and
`MODEL_CAPABILITIES_JSON` values then take precedence. Built-in adapters receive
the Agent-owned immutable capability snapshot, so a catalog edit cannot change
request semantics halfway through a Session.

Physical context capacity and repeated-input cost are separate budgets.
`PromptBudget.replay_compaction_tokens` defaults to 24K provider-visible history
tokens (`NZ_CONTEXT_REPLAY_COMPACTION_TOKENS`; `0` disables it). It excludes the
fixed system prompt and tool schemas, so a large catalog cannot cause a useless
summary on a short Session. Above the replay boundary, Context Runtime performs
the existing semantic compaction, archives the full transcript, and preserves a
recent provider-visible atomic suffix. Cost-triggered markers record
`trigger=replay_cost` and `overflow=false`; physical/provider overflow retains
its existing recovery meaning.

### 2. Tool Platform

Owns tool registration, permission boundaries, and concrete tool implementations.

- `tools/__init__.py`: function-tool registry, context-local dynamic-tool overlays, internal read/serial/write execution metadata, OpenAI tool specs, and dispatch.
- `tools/files.py`: read/write/edit/list tools, batch writes, path checks, transaction/change-tracker integration.
- `tools/bash.py`: shell execution tool with command classification.
- `tools/search.py`: grep/glob search tools.
- `tools/repo_intel.py`: repository intelligence tools such as `smart_search`, `read_symbol`, `find_symbol_callers`, `diff_status`, and changed-file verification.
- `tools/repo_map.py`: incremental multi-language structure map, shared cache/ranking, and optional bounded LSP enrichment.
- `tools/repo_languages.py`: conservative standard-library declaration extraction for non-Python source languages.
- `tools/repo_ranking.py`: deterministic exact/prefix/contains/path/fuzzy ranking for Repo Map candidates.
- `tools/python_ast.py`: Python AST structural checks and edits.
- `tools/lsp.py`: optional semantic navigation and diagnostics tool backed by an installed language server.
- `tools/todo.py`: durable workspace/session-scoped task checklist with status and priority.
- `tools/question.py`: structured 1-4 question schema, validation, and context-local interaction callback.
- `tools/scratchpad.py`: durable workspace/session-scoped plan and failure working memory.
- `tool_platform/permissions.py`: deny/ask/allow policy pipeline for tools and shell commands.
- `tool_platform/command_policy.py`: shared command safety classification.

Structured questions use a UI adapter rather than reading stdin inside the
tool handler. `interface/questions.py` owns terminal rendering and answer
parsing; `runtime/execution/loop.py` binds the adapter for one run through a `ContextVar`.
The tool is a serial barrier, is unavailable to child agents, and fails fast in
headless runs that do not provide an adapter.

### 2a. Language Server Runtime

Owns optional, workspace-scoped LSP subprocesses without adding an Agent
framework or an LSP client dependency.

- `lsp/servers.py`: shared language/extension mapping, project-root detection, executable discovery, and per-language command overrides.
- `lsp/client.py`: stdio Content-Length JSON-RPC, initialize/shutdown, document synchronization, server requests, and diagnostics.
- `lsp/manager.py`: workspace/root-scoped client caching, startup-failure isolation, and process cleanup.
- `lsp/write_diagnostics.py`: post-commit changed-file synchronization, compact diagnostic formatting, and silent fallback.
- `lsp/workspace_symbols.py`: filtered, path-scoped workspace symbols used as a best-effort Repo Map semantic supplement.

### 2b. Tool Scheduling

Tool execution effects are explicit and internal; provider schemas are unchanged.
Before scheduling, the loop canonicalizes tool arguments and updates a per-run
consecutive-call streak. The call that reaches the configured threshold
(default: the third identical call) is converted to a denied result before
permission checks or dispatch. Unknown tools default to `serial`.
Consecutive `read` calls are grouped into bounded parallel segments, while
`serial` and `write` calls form ordered barriers. A mixed batch therefore
preserves write/read causality without forcing unrelated reads on either side
of the barrier to run one by one.

```text
ordered model tool calls
  -> canonical identical-call guard
  -> explicit effect classification
  -> [parallel read segment]
  -> serial/write barrier
  -> [parallel read segment]
  -> restore original result indexes
  -> trace/hooks/messages/transaction finish
```

Pre-tool hooks force the whole batch back to serial because they may inspect or
modify shared message state. Write effects feed permission decisions and
read-only subagent filtering; transactional local writes additionally feed the
TransactionManager lifecycle. Bash is considered parallel-safe only when the
shared command policy recognizes every segment as read-only.

### 2c. Session-Owned MCP Runtime

MCP is opt-in and loads user, project, then environment configuration. Local
servers use argv arrays rather than shell strings; project-local commands must
be trusted by fingerprint before execution. Remote servers use Streamable HTTP
with same-origin legacy endpoint/message SSE fallback and optional OAuth. One
Agent owns the runtime generation, discovered tools/prompts/resources, live
configuration reconcile, and client retirement.

```text
layered config + project command trust
  -> stdio subprocess or authenticated remote transport
  -> initialize / notifications/initialized
  -> tools/prompts/resources discovery and list-changed refresh
  -> mcp_<server>_<tool> context-local bindings
  -> existing permission and effect-aware scheduler
  -> tools/call
  -> explicitly marked untrusted result
  -> Session close / disconnect / reconcile retirement
```

- `mcp/config.py`: layered source merge, strict validation, command fingerprint, timeouts, enable flags, and effects.
- `mcp/trust.py`: user-owned 0600 project-command trust records.
- `mcp/client.py`: stdio request correlation, deadlines, stderr draining, and process-group termination.
- `mcp/http_client.py` and `mcp/sse_client.py`: bounded Streamable HTTP and legacy SSE transports without proxy or redirect credential forwarding.
- `mcp/oauth.py` and `mcp/auth_store.py`: discovery, PKCE/state flow, refresh, and 0600 credential storage.
- `mcp/runtime.py`: startup isolation, live reconcile, transport fallback, cache refresh, collision-safe names, and lifecycle ownership.

MCP tools do not enter the module-level built-in registry. Declared `read` tools
may use the parallel scheduler; undeclared tools default to `serial`. External
`serial` and `write` effects retain permission checks, and MCP `write` remains a
non-transactional external side effect. Child agents do not inherit parent MCP
clients. The runtime has local CLI auth/trust/status controls but no MCP
management routes in the frozen loopback HTTP API. Public third-party
interoperability remains unverified until explicitly tested with user-owned
credentials.

### 2d. Native Session Event Protocol

`session_events.py` is the client-facing event boundary owned by core NZ-Coder.
Each `AgentLoop` owns one `SessionEventBus`; it is never stored in a module-level
mutable registry. Events use a stable envelope with `type`, `properties`, and a
`meta` object containing schema version, event ID, sequence, timestamp, session
ID, run ID, and agent ID.

```text
AgentLoop lifecycle / model response / tool result
  -> SessionEventBus.publish()
  -> bounded instance-local subscriptions
  -> optional type filter and recent replay
  -> local client, adapter, or iter_sse()
  -> server.connected / event frames / server.heartbeat
```

The bus is thread-safe because read tools may complete in worker threads. Each
subscriber has its own bounded queue; a slow client drops its oldest queued
event instead of blocking the Agent. A `ContextVar` binds the current bus while
one run is active so tools and future adapters can publish without sharing
session state. `AgentLoop.close()` emits `session.disposed`; CLI exit and session
replacement close the previous Agent.

The first protocol phase publishes run started/completed/failed/cancelled,
model-message completed, tool completed, and MCP status events. Trace JSONL is
still a separate diagnostic store: traces may contain implementation details,
while session events are the normalized live client contract. `encode_sse()` and
`iter_sse()` provide the transport primitives consumed by the optional local
service below.

Assistant text streaming uses an event-side part identity. The first provider
text chunk publishes `message.part.updated` with an empty text part, every chunk
publishes `message.part.delta`, and successful completion publishes the final
part snapshot. A failed partial attempt publishes `message.part.removed` before
retrying with a new part ID. This does not alter the terminal renderer's policy
of suppressing tool-turn preambles. A host reducer deduplicates by event ID,
replaces snapshots on `updated`, appends deltas only to a live part, tombstones
on `removed`, and treats the final snapshot as provisional until the matching
`session.message.completed`. Failure before the first text chunk creates no
part lifecycle at all. Cancellation retires the shared attempt under a lock,
removes a started part exactly once, and makes its provider worker ignore later
chunks. The run gate is retained until that worker exits, so abort may remain
in progress for an uncooperative provider but a new run cannot overlap it.
Repeated abort requests are rejected after the first request, and the worker
drain also absorbs repeated task cancellation until the executor future ends.

Each ordinary SSE event carries its EventBus ID in the SSE `id:` field. A
subscription may replay strictly after one retained ID; unknown or truncated
cursors fail explicitly. HTTP Agent buses optionally append the bounded replay
tail to a per-Session JSONL journal. At four times the in-memory capacity, the
journal compacts to the current tail; this bounds retained record count between
compactions, not file bytes. It can restore retained event identity after a
service restart when the records were written and validate successfully. The
loader exposes only the final contiguous valid suffix, so a cursor before a
detected corrupt or missing record expires instead of silently crossing a gap.
CLI-only buses remain memory-only. The journal is best-effort, subscriber queues
may drop events, and NZ-Coder still has no remote broker, end-to-end
at-least-once/exactly-once delivery, running-state snapshot, or IDE bridge.

HTTP conversation storage adds private message/part metadata to the existing
saved message list. The legacy `/messages` projection strips those fields;
`/snapshot` returns additive `{info, parts}` records. For an idle Session, the
manager holds its run lock while `SessionEventBus.checkpoint()` copies state and
publishes `session.snapshot.created` under the event lock. The returned event ID
is therefore a replay watermark: a new run cannot publish before the checkpoint,
and all supported later events fall strictly after it. Running snapshots return
409 instead of claiming a false atomic boundary.

### 2e. Optional Loopback Session HTTP Service

`http_service/` exposes a small local process boundary without changing the
default terminal workflow. `nz-coder` starts the REPL; `nz-coder serve` starts a
standard-library `ThreadingHTTPServer` bound only to `127.0.0.1` or `localhost`.
It is an Agent backend for scripts and future hosts, not a GUI or remote service.

```text
authenticated local client
  -> GET /workspace (operator-authorized roots and opaque IDs)
  -> POST /session (bind permanently to workspace_id)
  -> POST /session/{id}/run (202; one active run per selected workspace)
  -> GET /event?session_id={id} (connected + replay + live + heartbeat)
     -> optional Last-Event-ID (strictly-after replay or HTTP 410)
  -> GET /session/{id}/permission or /question (pending requests)
  -> POST /session/{id}/permission/{request}/reply
  -> POST /session/{id}/question/{request}/reply or /reject
  -> GET /session/{id} or /messages
  -> GET /session/{id}/snapshot (idle WithParts state + event cursor)
  -> POST /session/{id}/abort
  -> DELETE /session/{id} after the run is idle
```

- `http_service/workspaces.py`: resolves startup-registered local roots,
  rejects overlapping roots, assigns stable selector IDs, and rejects unknown
  client selections.
- `http_service/manager.py`: owns live and dormant Session records, committed
  history, one run gate per workspace, lazy restart recovery, one background
  asyncio loop for each accepted run, cancellation, persistence, and disposal.
  Agent terminal events describe execution; `session.run.settled` is emitted
  only after manager state is committed, persistence is attempted, and the
  selected workspace gate is released.
- `http_service/interactions.py`: owns pending permission/question records,
  timeout, reply validation, causal event ordering, and abort/dispose wake-up.
- `http_service/server.py`: strict JSON routes, 1 MiB body limit, Bearer auth,
  loopback enforcement, SSE disconnect cleanup, and bounded replay.
- `http_service/client.py`: dependency-free client that explicitly bypasses
  environment proxies for localhost requests, parses SSE IDs, and performs
  bounded reconnect attempts with the newest complete event cursor.
- `http_service/cli.py`: `nz-coder serve` argument parsing, token generation,
  repeated operator-only `--workspace` registration, startup output, and
  shutdown cleanup.

Except for `/health`, routes require a random or configured bearer token.
Browser `Origin` requests to those authenticated routes are rejected in this
non-browser phase; the data-free health probe remains available. HTTP agents
bind permission and question callbacks to a Session-local broker. The broker
publishes asked/replied/rejected events and blocks only the Agent worker until
an authenticated reply, explicit abort, disposal, or the configurable timeout.
It publishes the terminal interaction event before waking the Agent so clients
observe causal order. On startup, the manager scans only authorized roots,
validates each saved payload's Session ID, message shape, mode, and exact
workspace path, exposes valid records as `dormant`, and lazily creates an Agent
on first use. Session ownership determines routing for every later request;
clients cannot override it per run. The service still has no CORS, TLS, remote
bind, persistent interaction registry, generated SDK, or App UI. Event cursor
state is a bounded best-effort JSONL tail, not the source of truth for pending
permission/question state.

This registry limits which working directory the HTTP control plane may select;
it is not a chroot or OS sandbox. The file tools retain their workspace path
checks, but Bash and child processes still have the service account's filesystem
permissions. Resolved roots may not overlap because they would otherwise use
different run locks while touching shared files. Symlink aliases are normalized;
bind mounts and other filesystem aliases remain outside this check. Same-root
run conflicts fail immediately with HTTP 409. Permission, question, event, and
abort handlers stay responsive and do not acquire the workspace run gate.

### 3. State, Memory, And Safety

Owns persisted state, context management, edit safety, and observability.

- `state/context.py`: large-output persistence, micro-compaction, anchored summaries, recent-turn preservation, and token estimation.
- `state/memory.py`: persistent long-term memory, generic injected store/sync
  adapters, and an instance lock for concurrent Agents sharing one workspace cache.
- `state/sessions.py`: conversation save/resume helpers and per-session working-state paths.
- `state/skills.py`: local skill loading and path-triggered skill activation.
- `state/changes.py`: agent-authored before/after snapshots for review and rollback.
- `state/transaction.py`: atomic multi-file edit transactions with backup and rollback.
- `state/trace.py`: structured JSONL tracing.
- `state/workspace.py`: workspace and git status helpers.

### 4. Repository Understanding And Verification

Owns project profiling, patch risk analysis, and targeted verification planning.

- `intelligence/project_profile.py`: detects languages, package managers, roots, and common project commands.
- `intelligence/verification.py`: runtime completion gate with command-level
  `static` / `targeted` / `regression` state, changed-file snapshots, failed-test
  carry-over, and additive pipeline status reporting.
- `intelligence/verification_planner.py`: recommends low-noise verification
  commands and exposes an ordered machine-readable stage plan while preserving
  the legacy recommended/fallback interface. It never executes commands.
- `intelligence/impact_analyzer.py`: estimates patch risk, affected files, likely tests, and review notes.

The gate requires every inferable static command and every exact previously
failing target. Related-test guesses and broad regression remain optional by
default. An optional verification failure is still sticky until the same check
passes or a subsequent edit rebuilds the plan. `verify_changed_files` is the
aggregate static fast path; `python_symbol_check` is path-scoped when its input
includes a file path, and deleted Python files are skipped. Broad regression is intentionally not auto-run or
listed as a required gate action because repository policy may defer it to a
later harness. Gate prompts omit unrun optional checks, but may repeat an
optional check that was already run and failed. Verification command
classification does not turn arbitrary Bash writes into transactional file
edits and does not implement a general formatter/fixer blocker.

### 5. Greenfield Project Creation

Owns new-project generation and the post-generation quality loop.

- `project_creation/requirement_analyzer.py`: turns a natural-language project request into a structured spec.
- `project_creation/blueprint.py`: creates a file plan, milestones, verification commands, and notes.
- `project_creation/templates.py`: stable scaffolds for FastAPI, Python CLI/package, RAG demo, and agent demo projects.
- `project_creation/inspector.py`: inspects generated files for concrete implementation signals.
- `project_creation/completeness.py`: compares requested features against the generated project and blueprint.
- `project_creation/acceptance_planner.py`: produces acceptance criteria, demo commands, and expected outputs.
- `project_creation/verifier.py`: runs safe, low-noise verification for generated projects.

### 6. Evaluation And Benchmarks

Owns local evals, benchmark helpers, and SWE-bench integration.

- `evaluation/eval_runner.py`: local JSON-task eval harness for repo repair and project creation tasks.
- `evaluation/benchmark.py`: broader local benchmark runner.
- `evaluation/aider_benchmark.py`: Aider benchmark readiness helper.
- `evaluation/parallel_benchmark.py`: no-model serial/parallel scheduler timing and ordering benchmark.
- `evaluation/swebench_lite.py`: compatibility CLI shim.
- `swebench/models.py`: SWE-bench data structures.
- `swebench/adapter.py`: benchmark adapter.
- `swebench/guardrail.py`: static patch-risk guardrail.
- `swebench/orchestrator.py`: retry orchestration around the agent loop.
- `swebench/cli.py` and `swebench/__main__.py`: SWE-bench command entry points.
- `swebench/profiles.py`: Verified 500 main profile and Lite 300 smoke profile.
- `swebench/policy.py`: strict pass@1 local-tool and no-answer-search boundary.
- `swebench/artifacts.py`: exact-once attempt journal and public trajectories.
- `swebench/submission.py`: fail-closed official submission bundle validation.

### 7. CLI And Configuration

Owns the user-facing terminal entry point and process-wide settings.

- `interface/cli.py`: streaming terminal REPL, slash commands, and explicit
  `serve` dispatch without changing the no-argument REPL default.
- `interface/terminal_input.py`: one async `prompt_toolkit` input owner with
  multiline editing, private persistent history, slash/session/model/file
  completion, key bindings, a width-bounded inline status composer, bounded
  workspace scanning, an awaited fuzzy keyboard selector with single-Enter
  selection, and a non-TTY fallback. It reads the current Agent snapshot and
  does not own a second model or Session state. Its persistent surface exposes
  only a queue-presence predicate to `AgentLoop`, which interrupts a superseded
  turn between settled steps rather than cancelling an active Provider/tool.
- `interface/selector.py`: value/label option contract, deterministic fuzzy
  ranking, bounded list projection, keyboard navigation, and awaited full-screen
  application. It returns a value but never mutates model or Session state.
- `interface/interactions.py`: thread-safe bridge from blocking permission and
  question tool adapters to the single CLI event loop. It provides
  once/always/reject, single/multiple/custom answers, cancellation, and balanced
  renderer pause/resume without changing the HTTP pending/reply protocol.
- `interface/run_renderer.py`: bounded Rich projection of authoritative
  `session.run.*` and `session.tool.*` events, including sanitized tool cards,
  run settlement, changed-path summary, and permission/question cards. It owns
  no execution state and falls back to the legacy callback only when no event
  bus is available.
- `interface/timeline.py`: read-only grouping of persisted messages into visible
  user turns, Rich timeline/session metadata tables, and deep-copy history
  slicing for same-workspace conversation forks. Synthetic Agent diagnostics
  are retained in fork context but hidden as user turns.
- `interface/commands/`: registered slash-command metadata and handlers,
  including sync/async dispatch, transactional in-REPL model replacement, and
  keyboard-selected Session/model/fork transitions. The original synchronous
  dispatch remains available to non-interactive callers.
- `__main__.py`: `python -m nz_coder` entry point.
- `config.py`: environment-backed process defaults shared by many modules. Per-execution overrides come from `runtime/core/execution_context.py` and `runtime/process/workdir.py`; production task execution reads but does not mutate `config` state.
- `__init__.py`: package marker and project identity.

### 8. Unified Extension Metadata

`extensions/registry.py` projects the existing extension owners into immutable,
secret-free descriptors. Skills, hooks, lazy tool packs, and MCP servers retain
their own execution and lifecycle implementations; the shared contract only
normalizes identity, source, scope, trust, status, capabilities, effects,
permissions, and lifecycle class. `extensions/cli.py` exposes read-only
`list/status` inspection and never starts MCP processes or imports lazy packs.

### 9. Removed Dodo Parallel Architecture

The borrowed Dodo control plane and its PySide client were physically removed in
A034 after their production caller graph became empty. NZ-Coder does not retain
a compatibility import for that independent product. Reusable behavior now has
one project-owned implementation:

- Agent execution and headless hosting: `runtime/execution/loop.py` plus
  `http_service/`;
- session routing and lifecycle: `http_service/manager.py` and the persistent
  Session API;
- ordered updates and reconnect: `session_events.py` plus HTTP SSE journals;
- background isolated coding tasks: `runtime/agent/agent_manager.py`;
- local trace and observability: `state/trace.py` and Session events;
- memory injection and persistence: `state/memory.py` contracts.

No core event is automatically mirrored to an external Dodo data-report sink.
Adding any external sink remains a new opt-in feature requiring authentication,
redaction, bounded delivery, and explicit user authorization.

### 10. Completed Structural Cleanup

- The unreferenced `test.py` scratch module has been removed from the product
  package.

## Current Directory Layout

```text
nz_coder/
  runtime/              # agent, conversation, execution, verification, workflow, process, and Session domains
  providers/            # adapters, normalized responses, model capability registry
  lsp/                  # optional language-server discovery, JSON-RPC client, lifecycle
  tool_platform/        # tool contracts, permissions, exposure, catalog, and command policy
  tools/                # concrete tool implementations and registry
  extensions/           # unified secret-free extension metadata and CLI
  state/                # context, memory, sessions, skills, trace, changes, transaction, workspace
  intelligence/         # profile, verification planner, verification manager, impact analyzer
  project_creation/     # Greenfield mode
  evaluation/           # local eval and benchmark runners
  swebench/             # SWE-bench integration
  interface/            # terminal, headless, setup, and interaction surfaces
  http_service/         # optional authenticated loopback Session API and client
```

The intentionally retained top-level modules—such as `nz_coder.loop`,
`nz_coder.permissions`, and `nz_coder.eval_runner`—are formal public façades.
Internal modules such as memory use their canonical package paths directly;
removed internal root and flat-runtime paths are not compatibility wrappers.

## Safety Model

NZ-Coder uses layered safety rather than a single prompt instruction:

- Workspace path checks prevent file tools from escaping `WORKDIR`.
- Shell commands are classified as read-only, mutating, or dangerous.
- Plan mode blocks writes and unknown/mutating shell commands.
- File writes return unified diffs.
- The call reaching the consecutive identical-call threshold (default: third) is denied before dispatch and receives conservative recovery guidance.
- Explicit read tools may run concurrently; serial/write effects are ordered barriers.
- Multi-file write rounds run inside a transaction. Dynamic `execution="write"` tools enter that boundary, but rollback coverage requires transaction-aware file APIs.
- Agent-authored changes are tracked and can be reverted if the current file still matches the tracked after-state.

## Observability

Trace events are stored as JSONL. They include model request/response summaries, tool calls, `doom_loop_blocked` decisions, errors, compaction events, and run termination status. The CLI exposes `/trace` for quick inspection and `/status` for current workspace/runtime state.
