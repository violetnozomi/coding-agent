# Unified Model Gateway Design

## Goal

Remove direct Provider SDK ownership and duplicated call policy from NZ-Coder's
Agent Core. Main Agent turns, child Agent turns, planning/replanning,
compaction, memory reranking/extraction, stall/verification sidecars, and image
description must resolve and invoke models through one production Gateway.

This phase preserves existing public Agent, CLI, HTTP, SWE-bench, Session, and
tool interfaces. It does not introduce the final shared `AgentRunner`; it makes
the model boundary ready for that Runner without adding a third call path.

## Source findings

Provider adapters already normalize Anthropic, Gemini, OpenAI-compatible, and
Responses results to the OpenAI-shaped objects consumed by AgentLoop. The
remaining duplication is above those adapters:

- `AgentLoop` resolves provider/model/capabilities/pricing and implements
  streaming, buffered calls, retry, fallback, overflow, usage, and cost.
- `run_subagent()` independently creates a Provider/client, supplies its own
  timeout/cancellation worker, retry loop, and usage/cost accumulation.
- compaction accepts either a raw OpenAI client or Provider/client pair.
- memory calls `client.chat.completions.create()` directly and implements its
  own JSON-mode fallback.
- sidecar verifier, stall sidecar, planner/replanner, and image description
  issue direct Provider requests.

The lower Provider adapters are not replaced. They remain wire-format owners.

## Considered approaches

### Move the entire streaming loop into a Gateway immediately

This would remove the most code from AgentLoop, but the current stream loop also
persists Session parts and can execute tools at a settled stream boundary. A
direct move would make the Provider layer depend on SessionProcessor and tool
execution, recreating the God object under a new filename.

### Add a forwarding wrapper around `provider.create_completion()`

This removes direct calls but leaves selection, timeout, retry, overflow,
usage, cost, and lifecycle duplicated. It is module-level alignment without
behavioral ownership and is rejected.

### Two-level model boundary (selected)

`ResolvedModelRuntime` owns live provider resources and immutable model
metadata. `ProductionModelGateway` owns model-call policy and emits normalized
buffered results or stream events. AgentLoop remains the temporary owner of
Session/tool projection, but no longer decides Provider retry or usage policy.
Child and auxiliary callers consume the same Gateway contracts.

## Components

### ModelSelectionRequest

Immutable input for model resolution:

- workspace
- optional provider/model/variant override
- optional injected Provider/client for tests and hosts
- whether the Gateway owns and must close the client

### ResolvedModelRuntime

One resolved model identity and resource owner:

- logical provider and model IDs
- wire model ID
- Provider adapter and client
- immutable capabilities
- registry pricing
- selected reasoning variant
- ownership flag and idempotent `close()`

The composition root creates it. Main-child inheritance passes a selection
snapshot, not process-global mutable configuration.

### ModelCall

Immutable request envelope:

- messages and tools snapshots
- optional tool choice
- max output tokens
- streaming flag
- call purpose (`coding`, `planning`, `compaction`, `memory`, `verifier`,
  `stall_sidecar`, `vision`)
- timeout/deadline and cancellation source
- provider capability options
- JSON response preference where supported

### ModelCallOutcome

Provider-neutral terminal outcome:

- status: `completed`, `context_overflow`, `client_error`, `cancelled`, or
  `aborted`
- content, reasoning, tool calls, provider metadata, and finish reason
- normalized token usage and authoritative cost
- duration, first-token latency, and attempts
- diagnostic/error envelope

No caller extracts provider usage or calculates price independently.

### ModelStreamEvent

Streaming calls expose normalized events for text, reasoning, tool-call delta,
usage, finish, and provider metadata. `ProductionModelGateway` consumes the raw
Provider stream, applies idle/hard timeout, tracks whether a stable boundary was
published, and owns retry/non-streaming fallback decisions.

AgentLoop maps these events to `SessionProcessor` and its existing tool-ready
bridge. The Gateway does not import SessionProcessor, tools, renderer, or
AgentLoop.

### ProductionModelGateway

Responsibilities:

1. Validate and snapshot `ModelCall`.
2. Add Provider capability parameters.
3. execute buffered or streaming requests through the resolved runtime.
4. Settle cancellation before releasing the run gate.
5. Apply the shared transient classifier, Retry-After/backoff policy, maximum
   attempts, and single buffered fallback before a stable stream boundary.
6. Return a typed context-overflow outcome rather than treating it as malformed
   client input.
7. Normalize response/tool/reasoning/usage/cost/timing.
8. Emit call lifecycle observations through injected callbacks/events.

It does not build prompts, compact history, append Session messages, execute
tools, or decide whether the Agent should continue.

## Data flow

```text
Composition root
  -> resolve ModelSelectionRequest
  -> ResolvedModelRuntime
  -> ProductionModelGateway

Agent/child/auxiliary caller
  -> ModelCall
  -> Gateway buffered/stream execution
  -> ModelCallOutcome + ModelStreamEvent
  -> caller-owned Session/Tool/Memory projection
```

The temporary legacy integration is:

```text
AgentLoop._call_llm_async
  -> ProductionModelGateway
  -> stream callbacks
  -> existing SessionProcessor/tool-ready bridge
  -> compatibility LLMResult projection
```

`run_subagent()` uses the same Gateway but requests a buffered call. Its local
Provider timeout/retry/usage helpers are removed.

## Error and cancellation contracts

- Context overflow always produces `context_overflow`; compaction remains a
  caller decision.
- 400/401/403/404/422 and authentication errors are non-retryable typed client
  errors.
- 408/409/425/429, 5xx, timeouts, and connection failures use the existing
  transient classifier and Retry-After-aware bounded backoff.
- A streaming failure may fall back to one buffered request only before text,
  reasoning, or tool calls form a stable externally visible boundary.
- Cancellation sets the call token, closes an owned request/stream when
  possible, drains the worker, and returns/raises the existing caller-compatible
  cancellation outcome. A cancelled worker cannot publish late results.
- Auxiliary best-effort consumers such as memory reranking and sidecar
  verification retain fail-open behavior by interpreting the typed Gateway
  outcome; they do not implement separate transport retries.

## Migration sequence

1. Extract provider-independent usage/cost and typed call models from Loop
   helpers with literal contract tests.
2. Add `ResolvedModelRuntime` and make AgentLoop construction consume it while
   retaining compatibility aliases `self.provider` and `self.client`.
3. Add buffered Gateway calls with timeout, cancellation, retry, overflow, and
   diagnostics; migrate child, planner/replanner, compaction, memory, verifier,
   stall sidecar, and vision.
4. Add normalized streaming event consumption and migrate the main coding turn,
   preserving Session/tool event order and buffered fallback safety.
5. Delete duplicated subagent timeout/retry/usage helpers and direct runtime
   model calls.
6. Add an architectural test that permits direct `create_completion()` only in
   Provider adapters and explicit provider-smoke tooling.

## Compatibility

- `AgentLoop` constructor accepts existing `client=` and `provider=` arguments.
- `self.provider`, `self.client`, `self.model_id`, `self.request_model_id`,
  `self.model_capabilities`, and `self.model_pricing` remain readable during
  migration.
- `run_subagent()` and `auto_compact()` public call signatures remain compatible;
  internal Gateway injection is optional until all callers migrate.
- Existing `LLMResult` and dictionary result shapes remain compatibility
  projections until the shared Runner consumes `ModelCallOutcome` directly.
- Provider smoke tests intentionally remain lower-level adapter tests and may
  call Provider adapters directly.

## Testing

Contract tests cover:

- exact model/provider/wire-ID/capabilities/pricing resolution
- injected client ownership and idempotent close
- buffered success and normalized tool/reasoning output
- usage and provider-reported versus registry cost precedence
- context overflow versus ordinary 400 diagnostics
- Retry-After, bounded retries, timeout, and cancellation settlement
- streaming text/reasoning/tool/usage ordering
- fallback before stable boundary and no replay after stable boundary
- main/child parity for identical fake Provider failure sequences
- planner, compaction, memory, verifier, stall, and vision use of the same port
- absence of direct runtime Provider/client SDK calls outside allowed adapters

Focused existing Loop, subagent, Provider, context, memory, sidecar, vision, and
Session tests run after each migration task. The complete suite and a
provider-free real Agent trace are required before this phase may be marked
`trace_verified`.

## Completion boundary

Phase 2 is complete only when Agent Core production callers no longer invoke
Provider clients directly, main and child call policy is shared, duplicated
child Provider helpers are removed, the architecture guard passes, and one
provider-free trace proves main and child use the Gateway. Merely adding the new
classes is `mechanism_only`, not completion.
