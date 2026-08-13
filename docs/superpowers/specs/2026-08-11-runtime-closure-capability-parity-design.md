# Runtime Closure and Capability Parity Design

## Scope

This phase closes the existing runtime architecture without another broad
AgentLoop redesign. It freezes the native execution boundary, proves that
background and Workflow Agent nodes delegate to the shared child execution
chain, introduces one bounded middleware pipeline, makes runtime events a real
core port, and gives the Python SDK a direct AgentRunner path. It then rebuilds
the three-way capability matrix and implements at most two benchmark-justified
gaps.

No new Agent framework or runtime dependency is introduced. Existing CLI,
HTTP, evaluation, Provider, MCP, Memory, LSP, Worktree, verification, Workflow,
and legacy embedding behavior remains compatible. Existing dirty workspace
state is preserved and no Git integration action is performed.

## Verified Starting Point

- Native Runner already executes Model -> Tool -> Model -> Final without
  AgentLoop; the native method has no legacy adapter or RuntimeHost call.
- Main converts its legacy call into RunRequest/RunOptions, but its focused
  execution owners still project 95 AgentLoop attributes.
- foreground child and background thread/process execution call
  `run_subagent()`, which enters `agent.run()` and the same AgentRunner.
- Workflow Agent phases call `BackgroundAgentManager.start()`; Workflow owns
  DAG, cache, budget, retry/branching, artifacts, and scheduling, not an LLM or
  tool loop.
- Session is the native child transcript/usage owner. TaskRecord retains a
  distinct application TaskStatus and result projection.
- five legacy adapters remain. Direct `host._xxx` syntax is concentrated in
  context (4), lifecycle (21), and tool (3) adapters.
- AgentLoop is 3,826 LOC / 95 init attrs / 162 methods; AgentRunner is 1,005
  LOC; SubAgent is 2,254 LOC; runtime middleware count is zero.
- the loaded product registry exposes 57 tool schemas, about 33,245 JSON chars
  or 8,312 coarse tokens. This justifies a benchmark, not immediate filtering.

## Chosen Architecture

### Middleware

Add one stable `MiddlewarePipeline` in runtime core. It accepts ordered
middleware declarations and exposes run, model, and tool-batch boundaries.
Before hooks execute in declaration order; after hooks execute in reverse order.
The original execution error remains authoritative while error hooks observe it.
Middleware failures are not swallowed. Planning, verification, handoff, and
compaction remain stateful services rather than being mechanically converted.

The AgentRunner owns invocation of the pipeline around its existing
orchestration points. It does not learn coding-specific LSP, repo, snapshot, or
verification implementations. Existing ToolObservers remain the coding layer's
tool-specific extension point and execute inside the generic tool boundary.

### Events

`RuntimeEvent` becomes the single core event envelope. A stable event-name enum
and host-free sink signature are added. The production sink projects this
envelope to the already mature SessionEventBus bound through ContextVar, so CLI,
HTTP, SDK, and evaluation consume one public event protocol. A runtime event
middleware emits ordered run/model/tool facts. Existing presentation code is
not imported by core runtime.

### Public SDK

`AgentClient` gains a direct AgentRunner dependency. `run()` calls a typed
Runner result entry without constructing AgentLoop. Child execution creates a
parent-linked RunRequest and uses the same Runner; resume reuses the same child
session ID and sends only the new activation. The old `agent_factory` path stays
as an explicitly named compatibility path during this phase.

### Background and Workflow

Do not rewrite their schedulers. Strengthen the boundary instead:

```text
Workflow DAG -> BackgroundAgentManager -> run_subagent
Background thread/process -> run_subagent
Foreground task -> run_subagent
run_subagent -> Agent.run -> AgentRunner -> SessionRuntime
```

TaskRecord continues to own scheduling, cancellation handles, priority,
worktree, scope, conflict, artifacts, and result references. Session owns
conversation, usage, run lifecycle, and parent lineage. Standard child events
are projected from the manager's task lifecycle onto SessionEventBus.

### Capability Work

The phase will first benchmark dynamic tool exposure at 20/60/120 tools for
schema size, deterministic retrieval recall, and selection latency. Runtime
filtering is implemented only if the benchmark preserves the declared-tool
recall threshold; otherwise the deliverable is a design and recorded no-go.
Repo intelligence receives a source-level gap report only, as requested.

## Compatibility and Failure Policy

- Native middleware/event behavior is additive and provider-free.
- middleware exceptions fail the run and Session finalizes as error;
  cancellation remains cancellation.
- event sink failures are fail-open and cannot change Agent control flow.
- legacy SDK factory and AgentLoop APIs retain their return shapes.
- process-isolated background children retain their spawn boundary.
- Workflow never receives model/provider/tool execution code.

## Acceptance

The phase is complete when native/Main/child/background/workflow execution
boundaries have architecture tests; middleware ordering/failure and event order
are behaviorally tested; SDK-only run/child/resume works without AgentLoop; core
import guards prevent coding concrete imports; the legacy table and ownership
metrics are updated; the new capability matrix and three final reports are
written; focused and full tests, Ruff, compile/import smoke, SCC scan, and diff
check pass.
