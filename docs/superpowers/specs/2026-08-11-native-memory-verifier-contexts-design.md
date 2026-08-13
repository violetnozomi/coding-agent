# Native Memory and Verifier Contexts Design

## Goal

Remove broad Agent host parameters from the production MemoryService and
CompletionVerifier ports, while preserving AgentLoop through explicit legacy
adapters. This is the first service-by-service prerequisite for a truly Native
public SDK default.

## Design

`MemoryExecutionContext` carries only manager, session/model identity, optional
LLM client, tracer, lineage and a mutable recall cache. `VerificationExecutionContext`
carries an optional compatibility override plus one focused async stop-review
callback. Production services consume these types and cannot inspect AgentLoop.

Legacy adapters build the contexts at the compatibility boundary. Native SDK
composition can later construct the same contexts directly. No memory storage,
retrieval, review pipeline, reflection policy or result semantics are changed.

## Verification

Contract tests exercise recall caching, async/sync learning, lineage receipts,
verifier override/default behavior and static absence of broad host access.
Existing full tests must remain green.
