# Session-First Agent Runtime Phase 2 Design

## 1. Decision

NZ-Coder will continue with a session-first strangler migration. The previous
refactor established one production provider/tool turn loop, but it did not
finish state ownership: `AgentRunner` still receives an `AgentLoop` host and a
caller-owned mutable message list, while the production `RunState` and
`FileSessionRepository.load/save` APIs are not used by that loop.

This phase introduces a real Session Runtime and RunContext without replacing
the whole coding stack at once. Existing CLI, HTTP, evaluation, SDK, and
`run_subagent()` entry points remain compatible while their internal execution
is moved onto the new ownership model.

The design is informed by both references:

- InfCodeX supplies Agent-as-data, Runner, middleware, guardrail, verification,
  SDK, and shared child-execution principles.
- infcode-dev/OpenCode supplies the Session/Message/Part model, stream
  processor, compaction boundary, permission-scoped child sessions, and
  session-centric prompt loop.

It is a clean-room Python design. TypeScript implementation details and
framework-specific Effect/AI SDK mechanisms are not copied.

## 2. Verified current state

### 2.1 Git and source audit

The current worktree contains a large mixed set of uncommitted changes. The
tracked diff reports 62 modified files and no deleted files; 179 untracked
paths are also present. HEAD predates the runtime package, so Git cannot
reliably attribute current runtime files to individual previous phases.
Existing user changes must therefore be preserved and migration decisions must
be based on the current source, not commit history.

The previous refactor added the following important boundaries:

- `nz_coder/runtime/core/`: request, result, profile, state, event, and service
  contracts.
- `nz_coder/runtime/runner.py`: the only production turn loop.
- `nz_coder/runtime/model_gateway/`: provider-neutral request and result path.
- `nz_coder/runtime/tool_runtime/`: policy, scheduling, execution pipeline, and
  result projection.
- `nz_coder/runtime/context_manager.py`: context preparation and compaction
  entry.
- `nz_coder/runtime/session_processor.py`: stable message-part transitions.
- `nz_coder/runtime/session_repository.py`: storage adapter.
- `nz_coder/runtime/composition.py` and `services.py`: composition root and
  service graph.
- guardrail, transition, input-preflight, lifecycle, host, handoff, workflow,
  and child-contract modules.

No old production module has been deleted. Top-level `loop.py`, `subagent.py`,
and `tool_executor.py` are compatibility module-replacement wrappers.

### 2.2 Validation baseline

- Full test suite: 1549 passed in 96.68 seconds.
- Ruff: passed.
- Python compile and key-module import smoke: passed.
- Offline parallel evaluation smoke: passed; order was stable and observed
  peak concurrency was three.
- Static type check: unavailable. The project has no mypy/pyright configuration,
  so type-check success must not be claimed.

### 2.3 Actual production chain

```text
CLI / HTTP / Evaluation
  -> build_coding_agent
  -> AgentLoop.run
  -> AgentRunner.run
  -> ProductionRuntimeHost.run
  -> AgentRunner._run_turns
  -> ProductionContextManager.prepare_async
  -> ProductionTurnModelRuntime.complete_turn / ModelGateway
  -> SessionProcessor
  -> ProductionToolRuntime.execute_batch_async
  -> ToolExecutor
  -> SessionProcessor
  -> FileSessionRepository.checkpoint
  -> ProductionRunLifecycle.finalize

SDK
  -> AgentClient.run
  -> build_declared_agent
  -> the same AgentLoop / AgentRunner chain

Sub / Background
  -> run_subagent
  -> declared_runtime(...).build
  -> agent.runner.run
  -> the same AgentRunner._run_turns
```

There is one provider/tool turn loop, but not yet one complete Session Runtime
semantic.

## 3. Architectural defects to correct

### 3.1 Fake decoupling

`RuntimeServices` looks port-oriented, but its methods accept the complete
`host` and raw `messages`. `AgentRunner` calls more than twenty different
`host._private_method` families. `ProductionToolRuntime`, the model service,
memory service, context service, lifecycle, and repository also reach back into
the same host.

Consequently `AgentLoop` remains the actual service locator and state owner.
Its constructor currently assigns roughly 99 attributes, and the file remains
3716 lines long.

### 3.2 Dead or test-only abstractions

`RunState` is not constructed by the production Runner. It is used only by
tests and `FileSessionRepository.load/save`. Those load/save methods likewise
have no production caller; production persistence uses `checkpoint(host,
messages, status)` instead.

### 3.3 Session fragmentation

The main run mutates a caller-owned message list and stores session identity on
`AgentLoop`. Child execution reuses the shared Runner but independently owns
child JSON state, worktree lifecycle, result packaging, and verification.
Background scheduling owns another state surface in `AgentManager`.

### 3.4 Dependency cycles

AST import analysis found five strongly connected components. The largest has
21 modules spanning runtime composition, sessions, state, tools, subagents,
project creation, and Agent management. Provider modules, terminal interface
modules, and SWE-bench modules have smaller cycles.

### 3.5 Legacy tool registry

The execution pipeline is substantially unified, but definitions still live in
global `TOOL_SPECS` and `TOOL_HANDLERS` populated by import side effects. This
phase will place a registry port in front of them; it will not rewrite every
tool module at once.

## 4. Reference architecture decisions

| Capability | Primary reference | NZ-Coder decision |
|---|---|---|
| Agent Runner | InfCodeX | One Python `AgentRunner`; no profile-specific loop |
| Session | infcode-dev | Durable session identity, parent link, metadata, and complete message history |
| Message model | infcode-dev | Stable message and part identities with explicit tool-state transitions |
| Stream processor | infcode-dev | Sole writer for text, reasoning, tool, finish, and error parts |
| Context | Both | Derive model input from Session at each turn; never equate Session with model context |
| Compaction | infcode-dev + InfCodeX | Model-window-aware, pre-send, pairing-safe, resumable compaction |
| Tool runtime | Both | Definition/registry/executor/context/policy/scheduler/result projection as distinct boundaries |
| SubAgent | InfCodeX Runner + infcode-dev child Session | Child session plus the exact same Runner and services |
| Middleware | InfCodeX | Ordered typed hooks around model, tool, compaction, finalization, error, and stop |
| Guardrail | InfCodeX | Input/output/tool guardrails composed with permission policy |
| Permission | infcode-dev | Session rules narrowed by Agent profile and parent capability |
| Verification | InfCodeX | Bounded postcondition middleware; no second agent loop |
| Workflow | InfCodeX | Scheduling/orchestration outside the execution kernel |
| SDK | InfCodeX | Stable Agent, Session, Runner, Tool-facing surface backed by production implementations |
| Repo intelligence | Both | Coding-layer tools/services injected into Runtime; Runtime cannot import concrete LSP/index modules |

## 5. Target domain model

### 5.1 AgentConfig

`AgentConfig` is immutable data describing who executes:

```python
@dataclass(frozen=True)
class AgentConfig:
    name: str
    instructions: str
    model: ModelPolicy
    tools: tuple[str, ...]
    permissions: PermissionPolicy
    budget: AgentBudget
    middleware: tuple[str, ...]
    mode: RunMode
```

It does not own Provider clients, transcripts, retry counters, streaming state,
tool loops, Session storage, or mutable workspace services. Existing
`AgentDefinition`, `AgentSpec`, and `RunProfile` will be converged behind this
contract instead of introducing a fourth public agent representation.

### 5.2 Session

Session owns the full durable conversation and relationship state:

- session ID and optional parent session ID;
- workspace identity and title/metadata;
- complete stable messages and message parts;
- permission overrides;
- run status, timestamps, usage summary, compaction markers, snapshots, and
  resumable child correlation.

Session does not own an active Provider client or the current request's
cancellation object.

### 5.3 Context

Context is a derived, disposable model input. `ContextBuilder` selects and
projects Session messages, tool definitions, system instructions, memory,
workspace facts, and ephemeral reminders into a provider-neutral request. It
uses `ContextBudget` and may request persistent compaction, but it never replaces
the complete Session history with a lossy tail slice.

### 5.4 RunContext

`RunContext` is the mutable owner for exactly one Runner invocation:

```python
@dataclass
class RunContext:
    session: Session
    agent: AgentConfig
    workspace: WorkspaceContext
    permissions: PermissionContext
    cancellation: CancellationToken
    usage: UsageTracker
    turn: TurnState
    retry: RetryState
    handoff: HandoffState
    verification: VerificationState
```

It replaces the live-state subset currently spread over `AgentLoop`, raw
messages, `RuntimeState`, Runner locals, and child state dictionaries.

## 6. State ownership

| State | Current owner | Target owner | Lifetime |
|---|---|---|---|
| Complete messages | Caller list and AgentLoop | Session | Session |
| Message parts/tool status | SessionProcessor plus dictionaries | Session through SessionProcessor | Session/Turn |
| Active agent | AgentLoop | RunContext | Run |
| Model selection | AgentLoop/provider runtime | AgentConfig default, RunContext effective value | Agent/Run |
| Tool calls | Runner locals and SessionProcessor | TurnState/SessionProcessor | Turn/Tool call |
| Token usage/cost | AgentLoop runtime state | UsageTracker, summarized into Session | Run/Session |
| Context budget | AgentLoop/context manager | ContextRuntime | Turn |
| Iteration/retry counts | Runner locals and host state | RunContext | Run |
| Session ID/parent ID | AgentLoop and child JSON | Session | Session |
| Permission decisions | AgentLoop PermissionManager | PermissionContext | Session/Run |
| Cancellation | thread events and host callbacks | RunContext cancellation token | Run |
| Tool registry | module globals | Application ToolRegistry port | Application |
| Memory | AgentLoop | injected MemoryService with Session key | Application/Session |
| Snapshot | AgentLoop helpers | Session SnapshotService | Turn/Session |
| Plan/reflection/verification | AgentLoop fields | middleware state in RunContext | Run |
| Handoff | AgentLoop fields | HandoffState; Session records transition | Run/Session |
| Workspace/transaction | ContextVars and AgentLoop | WorkspaceContext/ToolExecutionContext | Run/Tool batch |

## 7. Lifetimes

- Application: configuration, Provider registry/factories, ToolRegistry,
  extension registry, durable SessionStore, event bus.
- Session: Session entity, complete transcript, parent/child relationship,
  accumulated usage, compaction records, snapshots.
- Agent: immutable `AgentConfig`; may be reused across Sessions.
- Run: RunContext, effective model, cancellation, retries, usage tracker,
  handoff and verification state.
- Turn: projected context, assistant message, SessionProcessor handle,
  compaction attempt, model stream.
- Tool call/batch: ToolExecutionContext, permission decision, cancellation,
  transaction, normalized result.

Resource acquisition and cleanup occur at their owning lifetime. Long-lived
Agent objects may not retain run-scoped mutable state.

## 8. Canonical execution semantic

```text
Host receives user input
  -> SessionService create/load and append user message
  -> resolve AgentConfig
  -> AgentRunner.run(agent, session, options)
  -> create RunContext
  -> before-run middleware
  -> ContextRuntime.prepare(session, run_context)
  -> before-model middleware
  -> LLMRuntime.stream(model_input, run_context)
  -> SessionProcessor converts deltas into durable parts
  -> after-model middleware
  -> if tool calls:
       ToolRuntime executes one policy-governed batch
       SessionProcessor settles every tool part
       after-tool middleware
       next turn
  -> before-final middleware and bounded verification
  -> persist terminal Session state
  -> return RunResult
```

Only `AgentRunner` decides whether another model turn occurs. SessionProcessor
stabilizes events but does not own the turn loop. ToolRuntime executes batches
but does not decide run termination. ContextRuntime builds model input but does
not mutate arbitrary host state.

## 9. Main, child, and background behavior

Main, child, and background runs share `AgentRunner`, SessionProcessor,
ContextRuntime, LLMRuntime, ToolRuntime, retry, usage, cancellation, and terminal
semantics.

- Main: load or create a root Session and use an interactive profile.
- Sub: TaskRuntime resolves AgentConfig, creates or resumes a child Session with
  `parent_session_id`, narrows permissions/tools/model, calls the same Runner,
  then returns the child result and session ID.
- Background: scheduler creates a child/task Session and starts the same Runner;
  only scheduling, observation, and parent-result delivery differ.

Worktree setup, isolation cleanup, child briefing, cost propagation, and result
packaging remain orchestration responsibilities outside the Runner.

## 10. Middleware model

The runtime exposes ordered hook points:

- `before_run` / `after_run`
- `before_context` / `after_context`
- `before_compaction` / `after_compaction`
- `before_model` / `after_model`
- `before_tool` / `after_tool`
- `before_final`
- `on_error` / `on_stop`

Guardrails and permissions remain explicit policy services rather than generic
best-effort hooks. Reflection, memory extraction, sidecar verification,
completion verification, evidence recording, and handoff observation can use
middleware when they do not alter the fundamental turn state machine.

Middleware ordering is declared once in the composition root. Hook failures
are fail-open only when the hook contract explicitly marks them observational;
policy, persistence, and state-integrity hooks fail closed.

## 11. Tool Runtime migration

The target tool stack is:

```text
ToolDefinition -> ToolRegistry -> ToolPolicy
               -> ToolScheduler -> ToolExecutor
               -> ToolResultNormalizer -> SessionProcessor
```

`ToolExecutionContext` supplies workspace, permission, cancellation,
transaction, trace, session/message IDs, and injected coding services. Tool
handlers do not receive AgentLoop.

Incremental migration:

1. Wrap current `TOOL_SPECS`/`TOOL_HANDLERS` in a ToolRegistry adapter.
2. Change ToolExecutor and ToolRuntime contracts from `host` to
   `ToolExecutionContext`.
3. Move transaction, LSP refresh, snapshot, and result projection behind
   injected services.
4. Migrate tool modules from side-effect registration only when all consumers
   use the registry port.

## 12. Dependency boundaries

Allowed direction:

```text
Interface / Evaluation / Coding / Workflow
                  -> Runtime Core
Runtime adapters  -> Runtime Core
Coding services   -> Runtime protocols
```

Forbidden direction:

- Runtime Core importing CLI/HTTP/SWE-bench.
- Runtime Core importing concrete LSP, repo index, repo map, or search modules.
- Tool or Provider packages importing AgentLoop.
- Session storage importing AgentManager.
- Core contracts importing production adapters.

Repo intelligence is exposed as tools or injected protocols owned by the
Coding layer.

## 13. Public SDK direction

The eventual stable surface is conceptually:

```python
agent = Agent(...)
session = await sessions.create(...)
result = await runner.run(agent, session)
```

The exact names will follow existing `AgentDefinition`, `AgentClient`, and
`RunRequest` compatibility needs. A new cosmetic API will not be exported until
the objects are backed by the production Session Runtime; otherwise it would be
another facade over AgentLoop.

## 14. Strangler migration phases

### Phase A: characterization gates

Add provider-free behavioral matrices for text completion, one/multiple tools,
parallel tools, tool failure, Provider retry/error, cancellation, timeout,
resume, message parts, child session, snapshots, and compaction pairing.

### Phase B: Session domain and store

Create the Session model, message operations, store protocol, and legacy JSON
adapter. Wire production load/checkpoint/save through these objects while
preserving the current disk format.

### Phase C: RunContext enters production

Replace production use of raw `messages` and unused `RunState` with a
RunContext constructed by AgentRunner. Merge useful RunState fields rather
than maintaining parallel mutable state models.

### Phase D: SessionProcessor ownership

Make SessionProcessor the only path that creates and transitions assistant,
reasoning, tool, finish, and error parts. Remove duplicate message mutation
from AgentLoop only after characterization parity.

### Phase E: host-free runtime ports

Change Context, Model, Tool, Lifecycle, Memory, Guardrail, and Transition ports
to accept focused context objects. LegacyHostAdapter temporarily translates
between RunContext and remaining AgentLoop helpers.

### Phase F: Main and SDK cutover

CLI, HTTP, evaluation, and SDK create/load Session objects and call the same
Runner entry. `AgentLoop.run()` becomes a compatibility facade with no loop.

### Phase G: Child and background cutover

Task/SubAgent creates child Sessions and delegates to the same Runner.
AgentManager retains scheduling only. Remove child-specific transcript state
after resume and cancellation parity tests pass.

### Phase H: dependency and legacy deletion

Remove wrappers, compatibility overrides, dead state, and reverse imports only
after consumer searches are empty and the full acceptance suite passes.

## 15. Deletion gates

- `RunState`: merge into RunContext, then delete when no production or test
  consumer remains.
- `FileSessionRepository.checkpoint(host, messages, ...)`: delete after every
  entry point uses SessionStore save/checkpoint with Session identity.
- AgentRunner `_run` compatibility override: delete after test doubles and all
  hosts use the service graph.
- Model `_call_llm` override: delete after provider fakes implement LLMRuntime.
- Tool dispatch overrides: delete after all test/production consumers use
  ToolRuntime and ToolExecutionContext.
- Top-level module replacement wrappers: delete only in a documented breaking
  release or after an import deprecation window.
- AgentLoop loop/state methods: delete individually after their focused service
  owns behavior and characterization tests prove parity.
- Global tool registries: delete after every built-in, optional, MCP, and plugin
  tool registers through the application registry instance.

## 16. Acceptance criteria

The phase is accepted only when:

1. Main, Sub, and Background use the exact same Runner turn loop.
2. Runner receives AgentConfig, Session, and RunContext rather than AgentLoop as
   its semantic contract.
3. Session owns complete history; Context is rebuilt per model request.
4. SessionProcessor is the sole stable message-part transition owner.
5. Child execution creates/resumes a real parent-linked Session.
6. Provider, tool, context, retry, cancellation, streaming, and usage parity are
   verified across agent profiles.
7. Runtime Core has no concrete repo-intelligence or interface dependency.
8. Full tests, lint, compile/import, type check once configured, and runtime
   smoke tests pass.
9. Compatibility code is either deleted under its gate or explicitly recorded
   as remaining debt.

Passing tests alone is not evidence that the refactor is complete. Completion
also requires source-level ownership and dependency checks.

## 17. Known debt after design approval

- P0: production Session/RunContext are not wired.
- P0: Runtime service contracts depend on AgentLoop host and private methods.
- P0: child/background Session ownership is fragmented.
- P1: SessionProcessor and AgentLoop still overlap message mutation.
- P1: context/compaction owns behavior but not independent state.
- P1: public SDK remains an AgentLoop construction adapter.
- P1: large import cycle crosses runtime, tools, sessions, and subagents.
- P2: global side-effect tool registry remains.
- P2: compatibility overrides and wrappers remain active.
- P2: no configured static type checker.
- P3: package organization can be simplified after behavioral migration.

The next implementation plan must start with the five highest-priority items:

1. Session characterization and domain model.
2. Production RunContext wiring.
3. SessionProcessor single-writer enforcement.
4. Host-free Context/LLM/Tool ports through focused contexts.
5. Parent-linked Sub/Background Session migration.
