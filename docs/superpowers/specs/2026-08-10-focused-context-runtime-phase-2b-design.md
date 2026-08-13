# Focused Context Runtime Phase 2B Design

## Decision

Phase 2B starts the approved host-free Runtime migration at the smallest safe
production boundary. `ProductionContextManager` will stop accepting the whole
`AgentLoop` host. It will instead consume an immutable
`ContextExecutionContext` containing only workspace identity, prompt budget,
token projection, compaction, compaction stamping, and tracing capabilities.

The production `AgentRunner` constructs this context once per run through a
temporary legacy-host adapter. Existing synchronous `AgentLoop` compatibility
entry points use the same adapter, so context budgeting and compaction retain a
single implementation.

The same phase moves asynchronous Tool Runtime checkpoint requests onto the
active `RunContext` and `SessionRuntime`. The rest of Tool Runtime remains
host-backed until its larger capability surface is migrated in a later phase;
this limitation must remain explicit.

## Boundaries

`ContextExecutionContext` owns no transcript and no Provider client. Session
continues to own the complete transcript; Context Runtime receives that live
list only to derive and compact the next request view.

The focused context exposes behavior rather than the legacy host:

- resolved workspace path;
- immutable prompt budget snapshot;
- projected request token counter;
- message compaction operation;
- automatic-compaction marker operation;
- structured trace publisher.

`ProductionContextManager` must contain no `host.` accesses after migration.
The adapter is the only place allowed to translate those operations to current
AgentLoop methods.

## Tool checkpoint ownership

The production async Tool Runtime receives an explicit async checkpoint
callback bound to `SessionRuntime.checkpoint(run_context, status)`. Stable tool
start, interruption, and finish boundaries use that callback. Direct legacy
callers may omit it and temporarily fall back to `_checkpoint_messages`; the
fallback is documented and covered by compatibility tests.

This phase does not claim Tool Runtime itself is host-free. It only removes the
production checkpoint ownership leak before the remaining tool capabilities
are converted to a focused ToolExecutionContext.

## Error and cancellation semantics

- Context compaction exceptions propagate unchanged to Runner settlement.
- Async tool cancellation settles the processor, checkpoints `interrupted`,
  drains/rolls back writes using the existing pipeline, and then re-raises.
- Missing production checkpoint injection is an error when an active
  `RunContext` exists; it must not silently persist through the old repository.
- Compatibility fallback is limited to callers without an active RunContext.

## Acceptance criteria

1. `ProductionContextManager` has no AgentLoop/host dependency.
2. Sync and async context preparation preserve soft/hard budget behavior.
3. Runner constructs and passes one focused context per run.
4. Production async Tool Runtime checkpoints only through SessionRuntime.
5. Legacy sync/direct Tool Runtime behavior remains compatible.
6. Full tests, Ruff, compile/import, architecture tests, and provider-free
   runtime smoke pass.
7. The learning log records both the improvement and remaining Tool/Runner
   host dependencies without claiming Phase E complete.
