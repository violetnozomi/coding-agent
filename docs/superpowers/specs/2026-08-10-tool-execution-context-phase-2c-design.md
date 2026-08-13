# ToolExecutionContext Phase 2C Design

## Decision

Production asynchronous Tool Runtime will stop receiving the complete
`AgentLoop`. A run-scoped `ToolExecutionContext` will compose three cohesive
capability groups:

- policy state: active Agent identity, allowed tools, admission ceiling,
  permissions, recovery/stall state, strict-progress state, input parsing,
  observability, and trace;
- execution lifecycle: executor, transaction state, guardrails, media
  preflight, transitions, Session checkpoint, snapshots, post-write refresh,
  plan application, and Agent transition notification;
- result projection: result accounting, stable tool message projection,
  transition signal extraction, stall observation, and post-result hooks.

The legacy adapter may call AgentLoop private methods, but Tool Runtime,
ToolPolicy, and ToolResultProjector may not. This is the strangler boundary:
compatibility is localized rather than renamed and spread through production
services.

## Scope

The asynchronous path used by the shared Runner is migrated in this phase.
The synchronous direct compatibility path remains host-backed because it is not
used by the production async Runner and has different cancellation semantics.
It will be removed only after its consumers are identified and migrated.

`ToolExecutionContext` is created once per Runner invocation and reused across
tool batches. Batch sequence and observability therefore become run-scoped
state rather than fields silently mutated on a long-lived AgentLoop.

## Execution flow

```text
AgentRunner
  -> tool_context_from_legacy_host(host, run_context, services)
  -> ProductionToolRuntime.execute_batch_async(context, calls, transcript)
       -> ProductionToolPolicy(context.policy, ...)
       -> context.lifecycle dispatch/transaction/guardrail operations
       -> ProductionToolResultProjector(context.projection, ...)
       -> context.checkpoint(status) -> SessionRuntime
```

Only the adapter knows the compatibility host. The contexts expose named
operations and state required by one tool batch; they do not offer a generic
`getattr`, `host`, or arbitrary callback lookup escape hatch.

## Failure semantics

- An active production run without SessionRuntime checkpoint capability fails
  before tool execution.
- Cancellation drains executing workers, settles the SessionProcessor,
  checkpoints interrupted state, then rolls back an active write transaction.
- Policy, permission, guardrail, persistence, and transaction failures remain
  fail-closed.
- Observational tracing callbacks preserve their existing best-effort behavior.
- Adapter construction validates required production capabilities up front.

## Acceptance criteria

1. Async `ProductionToolRuntime`, `ProductionToolPolicy`, and
   `ProductionToolResultProjector` contain no direct AgentLoop private access.
2. Runner creates one ToolExecutionContext per run and passes it to stream and
   buffered tool paths.
3. Tool batch sequence and observability live in run-scoped context state.
4. Tool start/finish/cancel checkpoints remain SessionRuntime-owned.
5. Read concurrency, write barriers, transaction rollback, permission denial,
   doom-loop protection, handoff, hooks, parts, and attachments retain parity.
6. Sync compatibility is explicitly isolated and recorded as debt.
7. Full tests, architecture gates, Ruff, compile/import, and offline runtime
   smoke pass without paid Provider or SWE-bench calls.
