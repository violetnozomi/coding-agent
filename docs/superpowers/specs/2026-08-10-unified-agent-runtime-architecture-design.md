# Unified Coding Agent Runtime Architecture Design

## 1. Executive summary

NZ-Coder already contains most of the mechanisms expected from a modern coding
agent, but production ownership is split across two execution kernels. The main
agent runs through `AgentLoop._run()`, while child agents run through a separate
loop inside `run_subagent()`. Provider policy, context management, tool
execution, cancellation, persistence, verification, and terminal behavior can
therefore drift even when similarly named modules exist.

The approved direction is a compatibility-preserving strangler migration. A
single Python `AgentRunner` will eventually own the execution state machine.
Main, read-child, write-child, background, workflow, HTTP, terminal, and
SWE-bench invocations will differ only through declarative profiles and injected
services. Existing `AgentLoop(...)`, `agent.run(...)`, and `run_subagent(...)`
entry points remain facades until their callers have migrated.

## 2. Source findings

InfCodeX exposes a declarative `Agent` and a `Runner` frame. Its default coding
agent attaches a coding substrate executor, and both the public SDK path and
child executor ultimately enter `runKodaX()` and `runSubstrate()`. The child
executor owns isolation, scope, profile, and result packaging rather than a
second provider/tool loop.

NZ-Coder has a canonical construction owner in `runtime/composition.py`, but
`AgentLoop` still constructs and owns provider clients, MCP, memory, skills,
sessions, background agents, transactions, tracing, context orchestration,
tool scheduling, verification, and terminal policy. `run_subagent()` repeats a
large part of that execution lifecycle independently.

The relevant source anchors are:

- InfCodeX `Runner`: `references/InfCodeX/packages/agent/src/primitives/runner.ts`
- InfCodeX coding substrate: `references/InfCodeX/packages/coding/src/agent-runtime/run-substrate.ts`
- InfCodeX child orchestration: `references/InfCodeX/packages/coding/src/child-executor.ts`
- NZ-Coder main kernel: `nz_coder/runtime/loop.py`
- NZ-Coder child kernel: `nz_coder/runtime/subagent.py`
- NZ-Coder composition owner: `nz_coder/runtime/composition.py`

InfCodeX itself retains a generic Runner path and a coding-substrate path. The
goal is therefore not a mechanical TypeScript translation. NZ-Coder will reuse
the sound boundaries while avoiding a second substrate/runtime split.

## 3. Critical issues

### P0: duplicated execution kernels

Main and child agents independently implement provider turns, tool loops,
retry, persistence, cancellation, verification, and finalization. Any fix must
currently be made twice and parity cannot be guaranteed by construction.

### P0: runtime God object

`AgentLoop` owns both orchestration and infrastructure. Its constructor creates
services and its methods implement lifecycle policy, making the state machine
difficult to test without the complete production object graph.

### P0: reversed and cyclic dependencies

Runtime, state, tools, providers, sessions, workflows, subagents, and interface
modules contain cycles. In particular, runtime code imports terminal UI
behavior and session cleanup imports runtime managers. Core execution cannot be
embedded cleanly while those directions remain.

### P1: helper modules do not own complete capabilities

`ToolExecutor`, context helpers, hooks, and session processors exist, but the
loop retains scheduling, transaction, post-processing, persistence, and event
ordering. The modules reduce method size without establishing enforceable
runtime boundaries.

### P1: mutable state has no single lifecycle owner

Agent fields, `RuntimeState`, `SessionProcessor`, ContextVars, tool scopes,
lineage, background state, and child JSON state overlap. Run-scoped, session-
scoped, workspace-scoped, and process-scoped values are not consistently
distinguished.

## 4. Considered approaches

### A. Compatibility facade plus extracted Runner (selected)

Keep public constructors and call signatures while extracting contracts and
services behind them. Route both main and child execution into the shared
Runner only after contract tests prove each migrated lifecycle slice.

### B. Route children directly through the current AgentLoop

This removes one loop quickly but promotes the current God object to the final
kernel and initializes services children do not need. It is acceptable only as
a temporary experiment, not the target design.

### C. Replace everything with Runtime V2 in one cutover

This offers a clean model but has the highest regression risk across terminal,
HTTP, SWE-bench, MCP, sessions, and recovery. It also makes behavioral losses
hard to distinguish from intentional changes.

## 5. Target architecture

The dependency direction is:

```text
terminal / HTTP / SWE / SDK hosts
                |
                v
       RuntimeCompositionRoot
                |
                v
           AgentRunner
      +---------+----------+
      |                    |
      v                    v
 AgentDefinition        RunState
      |                    |
      +---------+----------+
                |
                v
         RuntimeServices
   +------------+-------------+-------------+
   |            |             |             |
   v            v             v             v
ModelGateway  ToolRuntime  ContextManager  SessionRepository
                            |                |
                            v                v
                     CompactionService   LineageStore
```

Hosts own presentation, interactive questions, Ctrl+C interpretation, and
transport. The Runner owns lifecycle ordering. Services own provider, tool,
context, session, memory, extension, and verification mechanisms. Core modules
depend only on Protocol contracts; adapters implement those contracts.

The following dependencies are forbidden in the target state:

- `runtime.core -> interface`
- `state -> runtime` implementation modules
- `tools -> AgentLoop`
- `providers -> AgentLoop`
- `sessions -> AgentManager`

## 6. Core contracts

`AgentDefinition` is immutable agent-as-data: identity, instructions, allowed
tools, provider/model overrides, reasoning policy, guardrails, handoffs, and an
optional output schema.

`RunProfile` declares host-independent capability policy for `main`,
`read_child`, `write_child`, `background`, and `workflow` runs. It must not hold
live clients or mutable session data.

`RunRequest` is the complete immutable input to one Runner frame: agent,
profile, messages, workspace, session identity, streaming preference, and
optional model overrides.

`RunState` is the sole owner of mutable execution state for one frame. It owns
the transcript, turn counters, terminal state, usage, active agent, tool
observations, compaction counters, and parent correlation. It does not own
process-global registries.

`RuntimeServices` groups Protocol-typed services created by the composition
root. The Runner receives this object; it never imports a concrete provider,
terminal renderer, filesystem session store, or global Agent manager.

`RunResult` is the stable terminal envelope returned to every host. Compatibility
facades may translate it to the current dictionary/string shapes during
migration.

## 7. Runner lifecycle

The canonical ordering is:

1. Validate `RunRequest` and construct `RunState`.
2. Load or initialize the session transcript.
3. Run `before_run` middleware.
4. At each settled turn boundary, drain queued host/child messages.
5. Build a complete model-window-aware context budget.
6. Compact through the shared compaction service when required.
7. Resolve provider/model/reasoning policy for the turn.
8. Call the model gateway and persist the normalized assistant step.
9. Repair and validate tool calls before dispatch.
10. Run permission and pre-tool guardrails.
11. Schedule read-only calls concurrently and mutation calls safely.
12. Run result classification, transaction, reflection, recovery, visibility,
    persistence, trace, and verification post-processors in a fixed order.
13. Evaluate handoff, terminal tool signals, stop hooks, and bounded
    reanimation only after the full tool batch settles.
14. Persist terminal state and return `RunResult`.
15. Run cleanup in `finally`, including cancellation settlement.

## 8. Unified main/child/background model

All execution modes call the same Runner. Differences are configuration:

| Capability | Main | Read child | Write child | Background |
|---|---|---|---|---|
| Shared Runner | yes | yes | yes | yes |
| Durable session | full | child | child | task |
| Workspace | primary | scoped/worktree | worktree | configured |
| Mutation tools | permission policy | disabled | scope-limited | profile |
| Child spawning | enabled | disabled by default | disabled by default | profile |
| Questions | host callback | parent relay | parent relay | parent relay |
| Context/compaction | shared | shared | shared | shared |
| Trace schema | root | parent-linked | parent-linked | task-linked |

After migration, `run_subagent()` only prepares isolation and a child
`RunRequest`, invokes `AgentRunner`, packages the result, and cleans resources.
It contains no provider loop.

## 9. Incremental roadmap

### Phase 0: characterization and parity contracts

Record existing main/child behavior for terminal states, Provider retry,
compaction, tool errors, cancellation, persistence, and trace ordering using
fake providers. No production behavior changes.

### Phase 1: core contracts

Add immutable definitions for Agent, profile, request, result, state, events,
and Protocol-typed services. Keep all existing production entry points.

### Phase 2: Provider runtime

Extract model resolution, request normalization, timeout, retry, overflow
recovery, reasoning fallback, streaming fallback, and usage accounting. Both
legacy kernels temporarily consume the same service.

### Phase 3: tool runtime

Promote the existing executor into a complete pipeline containing scheduling,
permission, cancellation, transaction, output policy, recovery, persistence,
trace, and verification ordering.

### Phase 4: context and session runtime

Separate pure budgeting from persistence and Provider-backed semantic
compaction. Introduce SessionRepository and Transcript abstractions while
retaining existing disk formats.

### Phase 5: shared AgentRunner

Implement the state machine and make `AgentLoop.run()` a compatibility facade.
Run legacy and new contract suites against the same fake scenarios.

### Phase 6: child migration

Replace the child provider/tool loop with a child profile and the shared
Runner. Preserve current `run_subagent()` arguments and result string.

### Phase 7: background and workflow migration

Managers retain scheduling, messaging, worktree, and aggregation ownership but
delegate every Agent execution to the shared Runner.

### Phase 8: dependency cleanup

Remove legacy loops and reverse imports only after production entry-point,
contract, and provider-free trace acceptance passes.

## 10. Test strategy

Every shared lifecycle contract is parameterized across main, read-child,
write-child, and background profiles, with streaming and non-streaming variants.
Required contracts include Provider retry, context overflow/compaction, tool
error classification, permission denial, cancellation, session resume, terminal
status, and trace ordering.

Each migration phase must pass focused tests and the existing full test suite.
No real Provider request is needed for architectural acceptance. A real terminal
smoke test is required after a production entry point first delegates to the
new Runner.

Alignment claims use four levels: `mechanism_only`, `wired`,
`contract_verified`, and `trace_verified`. Source-level completion requires the
same shared production chain and `trace_verified` evidence; matching filenames
or isolated unit tests are insufficient.

## 11. Planned file boundaries

The first two phases introduce:

```text
nz_coder/runtime/core/
    __init__.py       public contract exports
    contracts.py      service Protocols and RuntimeServices
    events.py         runtime event envelope and sink Protocol
    profiles.py       immutable execution capability profiles
    request.py        AgentDefinition and RunRequest
    result.py         terminal status and RunResult
    state.py          per-frame mutable RunState
```

Later phases add focused provider, tool, context, and session service packages.
No new external dependency or Agent framework is permitted.
