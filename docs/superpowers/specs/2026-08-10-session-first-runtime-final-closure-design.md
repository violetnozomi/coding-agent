# Session-first Runtime Final Closure Design

## Status and authorization

This design continues the previously approved Phase 2 option B: Session is the
durable state owner, Runner orchestrates explicit ports, and AgentLoop remains a
temporary compatibility/coding-capability shell. The user's request to finish
the remaining alignment in one continuous run authorizes executing all slices
below without another architecture-choice pause. No Git mutation is required.

## Goal

Finish the current Session-first Runtime migration without claiming unrelated
product or benchmark parity. A production Main, child, or background run must
use one AgentRunner, one native Session/RunContext, and focused Model, Tool,
Context, and Lifecycle capabilities. Core production services must not receive
or discover AgentLoop.

## Scope boundaries

The closure includes:

1. a run-scoped ModelExecutionContext and host-free production model port;
2. a focused RunnerExecutionContext for turn hooks, message materialization,
   snapshots, planning, compaction, observation, and transition operations;
3. lifecycle initialization/finalization through a focused LifecycleContext;
4. parent-linked native Session ownership for foreground and background child
   runs, while task-control artifacts remain separate scheduler records;
5. removal of FileSessionRepository from the production RuntimeServices graph;
6. architecture gates, full regression verification, and an honest A237 record.

The closure does not include rewriting the global tool registry, deleting stable
public compatibility APIs without a deprecation window, building IDE clients,
or asserting SWE-bench/provider parity without external evidence.

## Architecture

```text
CLI / HTTP / SDK / evaluation / task / background
                         |
                  composition root
                         |
                 Agent compatibility shell
                         |
                     AgentRunner
                         |
       +-----------------+------------------+
       |                 |                  |
   RunContext     RunnerExecutionContext   RuntimeServices
       |                 |           +------+------+------+
    Session         explicit ops     |             |      |
       |                        ModelContext  ToolContext ContextContext
 SessionRuntime                    |             |      |
       |                         Gateway       Runtime  Builder
 SessionStore
```

AgentLoop is allowed only in composition adapters and stable legacy facades.
Production Runner and service implementations consume focused contexts. Context
objects contain either run-owned mutable state or narrow callable capabilities;
they never retain a generic `host` field.

## Component design

### ModelExecutionContext

The model context contains model capabilities, active model identity, active
tool specifications, prompt budget, Gateway construction/outcome projection,
stream/non-stream calls, message-part retirement, recovery observation, and
trace. ProductionTurnModelRuntime accepts this context in all production paths.
A single adapter translates AgentLoop once per run.

### RunnerExecutionContext

The Runner context groups operations by lifecycle rather than exposing dozens
of AgentLoop methods:

- turn control: queued follow-up, background drain, plan/replan, runtime persist;
- messages: identity, bindings, API projection, materialization, diagnostics;
- snapshots: start task, await/retire, finish capture, patch recording;
- hooks/events: turn start/pre-send/end and Session event publication;
- compaction and observation: compact, stamp, exhaustion, usage and trace;
- transitions and terminal helpers.

AgentRunner uses RunContext for transcript, counters, active agent, usage, and
terminal state. It uses RunnerExecutionContext only for capabilities that have
not become independent services.

### LifecycleContext

ProductionRunLifecycle receives a focused lifecycle context with explicit state
objects and callbacks. Initialization resets run-owned state through one method;
terminal settlement consumes explicit operations and never reads AgentLoop.
Legacy sync callers use an adapter.

### Native child Sessions

Task and background execution continue to use `run_subagent()` for worktree,
scope reservation, cancellation, and result packaging. Before entering Runner,
every child RunRequest must carry its own session ID plus the exact parent
Session ID. SessionRuntime creates/loads that native child Session. The existing
`state.json` remains a task-control record, not the transcript owner. Tests must
prove foreground/background children persist parent-linked Session snapshots and
resume through SessionRuntime rather than reconstructing conversation from the
task-control file.

### Compatibility retirement

`RuntimeServices.sessions` and FileSessionRepository are removed from the
production service graph after source consumers reach zero. The importable
FileSessionRepository and RunState compatibility types may remain temporarily
for external callers and their characterization tests; they must be explicitly
documented as non-production. Sync Tool Runtime remains a legacy facade until
its public callers have a deprecation path.

## Data and error flow

SessionRuntime opens RunContext before resource binding. Runner constructs all
focused contexts lazily from the composition adapter, then drives context build,
model invocation, tool batches, checkpoints, and terminal lifecycle. Provider
cancel retires the active message part and drains the worker. Tool cancel settles
the batch before transaction rollback. Checkpoint failures propagate; no service
falls back to a second persistence owner during an active RunContext.

Handoff updates RunContext and every policy context before the next turn. Child
session creation fails closed when the parent ID is missing or self-referential.
Task-control persistence failures cannot silently replace Session persistence.

## Verification and completion criteria

Each behavior change follows red-green-refactor. Completion requires:

- focused-context behavior tests and legacy facade characterization tests;
- AST gates showing zero direct `host.` access in production async Model,
  Tool, Context, Runner turn body, and Lifecycle implementations;
- one production Runner for main/read-child/write-child/background/workflow;
- parent-linked child Session persistence and resume tests;
- zero production references to RuntimeServices.sessions;
- Ruff, compile/import smoke, full pytest, architecture suites, offline runtime
  and concurrency smoke, and `git diff --check`;
- A237 records exact evidence and remaining non-production compatibility debt;
- no paid Provider calls and no SWE-bench run.

## Self-review

The design contains no placeholders. Session and task-control state have distinct
owners. The zero-host criterion applies to production implementations, not the
explicit adapter layer. Public compatibility deletion is intentionally excluded
because it would contradict the existing-interface constraint.
