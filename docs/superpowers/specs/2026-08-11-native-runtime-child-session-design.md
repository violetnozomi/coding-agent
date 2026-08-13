# Native Runtime De-hosting and Child Session Design

## Scope

This phase converts the already shared turn loop into a native runtime that can
execute without constructing `AgentLoop`. It then makes a child `Session` the
only durable owner of child conversation state. Existing coding capabilities
remain available through services and observers; Background Agent and Workflow
receive compatibility changes only.

The phase stops after the seven migration phases named in the user contract.
It does not remove `AgentLoop`, rewrite the global tool registry, or redesign
Background Agent, Workflow, Memory, MCP, LSP, repo intelligence, or project
creation.

## Verified Baseline

- `nz_coder/runtime/loop.py`: 3,773 lines.
- `AgentLoop`: 95 attributes assigned in `__init__`, 160 methods.
- `RunnerExecutionContext`: 47 fields, of which 45 are callbacks.
- `AgentRunner.run()` accepts `host` and `messages`; it creates a `RunRequest`
  through `_legacy_run_request()` and reaches native services through
  `runner_context_from_legacy_host()`.
- `runtime/adapters/*` contains 34 `host._xxx` references.
- `runtime/services.py` and `runtime/tool_runtime/*` contain 16 `host._xxx`
  references in the Tool path.
- Child runs already enter the shared `AgentRunner`, and native child Session
  persistence exists. Resume and post-processing still contain compatibility
  synchronization with task state.

## Chosen Approach

Use a vertical strangler migration. A real native entry is introduced first;
each subsequent phase replaces a coherent callback group with an owned service.
The legacy facade converts its configuration into native inputs once at the
edge. Native execution never calls a legacy adapter to recover `AgentLoop`
private methods.

Rejected alternatives:

1. A thin `run(request)` wrapper around the current callback bag would satisfy
   the signature but preserve the God Object ownership.
2. Removing `AgentLoop` in one rewrite would unnecessarily risk mature coding,
   Provider, MCP, Memory, Workflow, and verification behavior.

## Native API

`AgentRunner` exposes a native request boundary:

```python
result = await runner.run(request, options=RunOptions(...))
```

`RunRequest` remains immutable and carries Agent declaration, profile,
Session identity, workspace, exposed tools, model selection, stream mode, and
metadata. `RunOptions` carries only caller interaction concerns: tool/text/token
event callbacks and cancellation. `RuntimeServices` is injected into the
Runner constructor. `SessionRuntime.open(request)` creates the `RunContext` and
loads the sole durable transcript before the first turn.

The old `AgentRunner.run(host, messages, ...)` behavior moves to an explicitly
named compatibility method or adapter. `AgentLoop.run()` continues to work but
only builds a native request, native service graph, and `RunOptions`.

## Runtime Ownership

The 45 callbacks are not copied into another context. They are grouped by
state ownership:

- `SessionRuntime` owns open/checkpoint/finalize and durable transcript status.
- `SessionProcessor`/message runtime owns assistant/message-part mutation,
  model-result materialization, reconciliation, and Provider message projection.
- `RunContext` owns turn/retry/usage/compaction counters and cancellation.
- existing Model, Tool, Context, Lifecycle, Guardrail, Input, Transition, Memory,
  and Completion services own their current coherent responsibilities.
- a planning service owns initial plan and replan policy.
- a snapshot/change observer owns step snapshot and patch observations.
- event sinks and hook collections own presentation-neutral notifications.
- coding-only post-tool effects are injected as observers; generic ToolRuntime
  does not import LSP, code index, patch risk, or verification implementations.

Small state operations stay on `RunContext` or `Session`; no one-function
service is created merely to reduce a count.

## Execution Flow

```text
CLI / HTTP / SDK / Evaluation / Task
                  |
                  v
          RunRequest + RunOptions
                  |
                  v
        SessionRuntime.open(request)
                  |
                  v
              RunContext
                  |
                  v
             AgentRunner
       +----------+----------+
       |          |          |
   Context      Model      Tool
   Runtime      Runtime    Runtime
       |          |          |
       +----------+----------+
                  |
                  v
          SessionProcessor
                  |
                  v
               Session
```

`AgentLoop` is not part of this native graph. The legacy graph terminates at a
one-way adapter which produces the same request, options, and service inputs.

## Child Session Model

`run_subagent()` remains responsible for task admission, worktree isolation,
claimed paths, conflict detection, verification contract, result packaging,
and application state. It resolves the child Agent definition and calls the
same native Runner with a `RunRequest` whose Session identity has the current
parent Session ID.

Child execution state belongs only to the child Session:

- transcript and message parts;
- Session status;
- accumulated usage and run history;
- parent Session identity;
- resume transcript and model-turn history.

Task state keeps only orchestration facts. `TaskStatus` and `SessionStatus` are
separate domains: the former describes scheduling/application, the latter the
conversation lifecycle. Native child task records must never persist a complete
`messages` field. Legacy messages are accepted only once as a migration input
for an old task that has no native Session.

Resume loads the existing child Session, appends a new user activation, and
runs the same native Runner. It does not reconstruct conversation history from
task JSON or project Session state back into task JSON.

## Error and Compatibility Policy

- Existing Provider retry, timeout, cancellation, compaction, transaction, and
  terminal semantics remain characterized before each ownership move.
- Provider and tool failures continue to produce stable Session message parts;
  no exception is swallowed to make tests green.
- Legacy entry points retain return shapes and callbacks.
- Native errors finalize `RunContext` and Session exactly once.
- A failed phase is repaired before the next phase begins.

## Test Strategy

Every production change follows red-green-refactor. Required new behavioral
tests are:

1. Native Runner performs Model -> Tool -> Model -> Final without constructing
   `AgentLoop`.
2. Session transcript is the only durable conversation source.
3. A persisted Session resumes after a new Runtime/Store instance is created.
4. A child Session records its parent and uses the same Runner/services.
5. A child resume appends prompt B to the Session created by prompt A.
6. Main and child share Provider, tool, retry, stream, compaction, usage,
   cancellation, and message-processing implementation identities.
7. Native child task persistence rejects or removes complete `messages`.

Each phase runs its focused tests, Ruff, compile/import checks, and relevant
architecture guards. Final verification runs the complete test suite, static
import SCC analysis, runtime/CLI/SDK smoke, `git diff --check`, and quantitative
before/after metrics.

## Acceptance

The phase is complete only when:

- a full native run succeeds without `AgentLoop`;
- legacy Main delegates one-way into the native runtime;
- native Runner no longer consumes `RunnerExecutionContext` callback bag;
- the first Tool/Context/Model host-private dependencies are replaced by owned
  services or observers;
- child conversation and resume are Session-only;
- task persistence contains orchestration data only;
- all existing tests remain green and the requested metrics/matrix/report are
  recorded in the alignment learning log.
