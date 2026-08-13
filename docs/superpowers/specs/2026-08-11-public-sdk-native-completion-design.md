# Public SDK Native Completion Design

## Goal

Make `AgentClient()` and `run_agent()` enter a host-neutral runtime by default.
The default path must not import, construct, or adapt `AgentLoop`.  The explicit
`agent_factory=` argument remains the only legacy compatibility entry.

## Design

`NativeSDKRunner` is the public composition root.  It creates one run-scoped
environment per immutable `RunRequest`: model selection/client ownership,
permission policy, transaction/change tracking, workspace/session scopes,
message projection, context budgeting, tool dispatch, guardrails, structured
output, and durable session checkpoints.  It then delegates the state machine
to the existing `AgentRunner`.

The native environment exposes focused `RunnerExecutionContext` owners instead
of a flat host-shaped object.  Coding tools are executed through the canonical
registry and `ToolExecutor`; writes are bound to a per-run transaction and
change tracker.  Model responses use `ProductionModelGateway` and the stable
`LLMResult` envelope.  Resource scopes and provider clients are settled in a
`finally` block.

Declared input/output/tool guardrails and structured-output repair are handled
inside the native policy owner.  Unsupported media is left untouched for a
provider that supports it and rejected with an explicit diagnostic otherwise.
Handoff declarations remain represented in the public contract; the native
transition owner rejects undeclared transitions and supports structured output
without depending on legacy lineage stores.

## Compatibility boundary

Passing `runner=` uses the caller's native runner.  Passing `agent_factory=` is
an explicit legacy escape hatch and retains the old normalization/close rules.
The zero-argument client always uses `NativeSDKRunner`.

## Acceptance criteria

- Default SDK execution never calls `build_declared_agent` or constructs
  `AgentLoop`.
- A real offline Model -> Tool -> Model run succeeds through the default client.
- Workspace and transaction bindings are run-scoped and released on failure.
- Provider clients are closed exactly once.
- Explicit legacy factories remain compatible.
- Focused and full test suites, compile checks, and import checks pass.
