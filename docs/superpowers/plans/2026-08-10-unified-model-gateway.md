# Unified Model Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every Agent Core model request through one production Gateway that owns model resolution, Provider resources, timeout/cancellation, retry/fallback, context-overflow classification, and usage/cost normalization.

**Architecture:** `ResolvedModelRuntime` owns one Provider/client/model/capability/pricing snapshot. `ProductionModelGateway` consumes immutable `ModelCall` values and returns typed `ModelCallOutcome` values or normalized stream events; callers retain prompt, Session, tool, and fail-open domain policy.

**Tech Stack:** Python 3.9+, standard library, existing NZ-Coder Provider adapters, pytest; no Agent framework and no new dependency.

## Global Constraints

- Preserve `AgentLoop(...)`, `agent.run(...)`, `run_subagent(...)`, and `auto_compact(...)` public signatures.
- Preserve current CLI, HTTP, SWE-bench, Session, trace, tool, and result formats.
- Provider adapters remain the only wire-format owners.
- Provider smoke tooling may continue calling adapters directly.
- Agent Core runtime modules must not call raw SDK clients after this phase.
- Do not add external dependencies or an Agent framework.
- Use provider fakes only; do not issue paid model requests or run SWE-bench.
- Do not create commits or require Git.

---

### Task 1: Typed model calls, outcomes, stream events, and usage

**Files:**
- Create: `nz_coder/runtime/model_gateway/models.py`
- Create: `nz_coder/runtime/model_gateway/usage.py`
- Create: `nz_coder/runtime/model_gateway/__init__.py`
- Modify: `nz_coder/runtime/core/contracts.py`
- Test: `tests/runtime/model_gateway/test_models.py`
- Test: `tests/runtime/model_gateway/test_usage.py`

**Interfaces:**
- Produces: `ModelCallPurpose`, `ModelCallStatus`, `ModelCall`, `ModelStreamEvent`, `ModelCallOutcome`, `NormalizedUsage`, `normalize_usage(value)`, and `resolve_usage_cost(usage, pricing, provider_reported_cost)`.
- Consumes: existing `calculate_usage_cost()` and Provider-neutral message dictionaries.

- [ ] **Step 1: Write failing immutable-call and status tests**

```python
def test_model_call_snapshots_messages_and_tools():
    messages = [{"role": "user", "content": "inspect"}]
    call = ModelCall(
        purpose=ModelCallPurpose.CODING,
        messages=messages,
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        max_output_tokens=1000,
    )
    messages[0]["content"] = "changed"
    assert call.messages[0]["content"] == "inspect"
    assert call.streaming is False

def test_context_overflow_outcome_is_not_client_error():
    outcome = ModelCallOutcome.context_overflow("maximum context length")
    assert outcome.status is ModelCallStatus.CONTEXT_OVERFLOW
    assert outcome.retryable is False
```

- [ ] **Step 2: Run model tests and observe missing-module failure**

Run: `pytest -q tests/runtime/model_gateway/test_models.py`

Expected: collection fails because `runtime.model_gateway` does not exist.

- [ ] **Step 3: Implement immutable call/result/event models**

`ModelCall` deep-copies messages, tools, tool choice, and metadata; validates a
positive output limit and timeout. `ModelCallOutcome` exposes named classmethods
for `completed`, `context_overflow`, `client_error`, `cancelled`, and `aborted`.
`ModelStreamEvent` accepts only `text`, `reasoning`, `tool_delta`, `usage`,
`provider_metadata`, and `finish` kinds.

- [ ] **Step 4: Write failing literal usage/cost tests**

Cover OpenAI prompt/completion fields, Anthropic input/output fields, reasoning,
cache read/write, malformed/negative values, provider-reported cost precedence,
registry fallback, and unknown cost. Expected token values are literal.

- [ ] **Step 5: Move usage normalization out of Loop and make existing Loop helper delegate**

Implement `NormalizedUsage` and pure normalization in `usage.py`. Keep
`loop._extract_usage_tokens()` and `_extract_provider_reported_cost()` as
compatibility wrappers until all old tests migrate.

- [ ] **Step 6: Run model and existing usage tests**

Run: `pytest -q tests/runtime/model_gateway tests/test_model_pricing.py tests/test_loop_fake.py -k 'usage or cost or model_call'`

Expected: PASS.

### Task 2: Resolved Provider runtime and resource ownership

**Files:**
- Create: `nz_coder/runtime/model_gateway/runtime.py`
- Modify: `nz_coder/runtime/model_gateway/__init__.py`
- Modify: `nz_coder/runtime/loop.py:473-533`
- Test: `tests/runtime/model_gateway/test_runtime.py`
- Test: `tests/test_runtime_composition.py`

**Interfaces:**
- Consumes: `active_model_selection`, `create_provider`, `capabilities_for_provider`, `registry_runtime_model`.
- Produces: `ModelSelectionRequest`, `ResolvedModelRuntime`, and `resolve_model_runtime(request)`.

- [ ] **Step 1: Write failing resolution and ownership tests**

Use fake Provider/client objects to prove logical ID versus wire ID, capability
and pricing snapshots, injected client non-ownership, created client ownership,
idempotent close, and exact provider override behavior.

- [ ] **Step 2: Run tests and observe missing runtime types**

Run: `pytest -q tests/runtime/model_gateway/test_runtime.py`

Expected: FAIL on missing imports.

- [ ] **Step 3: Implement `ResolvedModelRuntime`**

The runtime stores provider, client, provider ID, logical/wire model IDs,
capabilities, pricing, variant, and `owns_client`. `close()` calls the client at
most once and never closes an injected host client.

- [ ] **Step 4: Make AgentLoop construction consume the resolver**

Set `self.model_runtime` once, then retain `self.provider`, `self.client`,
`self.model_id`, `self.request_model_id`, `self.model_capabilities`, and
`self.model_pricing` as compatibility aliases. Per-Agent model switching caches
resolved runtimes rather than independent `(provider, client)` tuples.

- [ ] **Step 5: Run resolution and composition tests**

Run: `pytest -q tests/runtime/model_gateway/test_runtime.py tests/test_runtime_composition.py tests/test_model_registry.py tests/test_model_capabilities.py`

Expected: PASS.

### Task 3: Buffered production Gateway policy

**Files:**
- Create: `nz_coder/runtime/model_gateway/gateway.py`
- Create: `nz_coder/runtime/model_gateway/errors.py`
- Modify: `nz_coder/runtime/model_gateway/__init__.py`
- Modify: `nz_coder/runtime/core/contracts.py`
- Test: `tests/runtime/model_gateway/test_buffered_gateway.py`

**Interfaces:**
- Consumes: `ResolvedModelRuntime`, `ModelCall`, `ProviderAttemptController`, `RecoveryState`, and usage helpers.
- Produces: `ProductionModelGateway.complete_sync(call, cancel_event=None) -> ModelCallOutcome` and async `complete(call, cancel_event=None) -> ModelCallOutcome`.

- [ ] **Step 1: Write failing buffered success tests**

Use real normalized Provider response objects. Assert text, reasoning, tool calls,
finish reason, usage, cost, attempt count, and duration in the outcome.

- [ ] **Step 2: Write failing failure-policy tests**

Cover context overflow, ordinary 400, authentication, Retry-After 429 followed
by success, retry exhaustion, hard timeout, cancellation before dispatch, and
cancellation while a worker is running. Inject a wait callback so tests do not
sleep.

- [ ] **Step 3: Run tests and observe missing Gateway behavior**

Run: `pytest -q tests/runtime/model_gateway/test_buffered_gateway.py`

Expected: FAIL on missing Gateway.

- [ ] **Step 4: Implement one buffered attempt and normalization**

Call only `runtime.provider.create_completion(runtime.client, ...)`, attach the
capability snapshot when required, and normalize the Provider response without
Session or tool imports.

- [ ] **Step 5: Implement bounded timeout, cancellation, and retry ownership**

Use one worker per in-flight request, poll at 50 ms or less, honor a shared
cancellation event, classify errors through existing recovery functions, apply
Retry-After-aware delays through the injected wait callback, and reject late
worker publication after terminal settlement.

- [ ] **Step 6: Run buffered Gateway tests**

Run: `pytest -q tests/runtime/model_gateway/test_buffered_gateway.py tests/test_recovery.py tests/test_final_alignment_closures.py`

Expected: PASS.

### Task 4: Migrate buffered Agent Core consumers

**Files:**
- Modify: `nz_coder/runtime/loop.py`
- Modify: `nz_coder/runtime/subagent.py`
- Modify: `nz_coder/state/context.py`
- Modify: `nz_coder/state/memory.py`
- Modify: `nz_coder/runtime/sidecar_verifier.py`
- Modify: `nz_coder/vision.py`
- Test: `tests/runtime/model_gateway/test_consumer_wiring.py`
- Test: existing focused consumer suites

**Interfaces:**
- Consumes: `ProductionModelGateway.complete_sync()`.
- Produces: no direct Agent Core buffered Provider/client SDK calls.

- [ ] **Step 1: Add failing consumer-wiring tests**

Inject a recording Gateway and prove planner, replanner, compaction, memory
rerank/extraction, verifier, stall sidecar, vision, and child buffered turns send
the expected `ModelCallPurpose`. Tests assert consumer output, not mock call
existence alone.

- [ ] **Step 2: Migrate planner, replanner, stall, verifier, compaction, memory, and vision**

Keep their domain-level fail-open/parse behavior. Replace raw SDK/provider calls
with typed outcomes. JSON-mode fallback becomes one Gateway request-policy
option, not a memory-specific raw retry.

- [ ] **Step 3: Migrate child Agent buffered turns**

Construct/inherit a resolved runtime, call the Gateway, and project its typed
outcome into the existing child SessionProcessor. Remove child-local usage/cost
normalization after parity tests pass.

- [ ] **Step 4: Run focused buffered consumer tests**

Run: `pytest -q tests/runtime/model_gateway/test_consumer_wiring.py tests/test_subagent.py tests/test_context_budget.py tests/test_memory.py tests/test_sidecar_verifier.py tests/test_image_describe.py tests/test_loop_fake.py -k 'planning or replan or sidecar or non_streaming or compact or memory or subagent or image'`

Expected: PASS.

### Task 5: Streaming events, retry, and fallback

**Files:**
- Modify: `nz_coder/runtime/model_gateway/gateway.py`
- Create: `nz_coder/runtime/model_gateway/stream.py`
- Test: `tests/runtime/model_gateway/test_streaming_gateway.py`

**Interfaces:**
- Consumes: `ModelCall(streaming=True)` and optional `on_event(ModelStreamEvent)` callback.
- Produces: `ProductionModelGateway.complete_stream_sync(...) -> ModelCallOutcome` with shared timeout/retry/fallback policy.

- [ ] **Step 1: Write failing stream-order tests**

Assert literal event order for text, reasoning, partial tool name/arguments,
usage, Provider metadata, and finish across normalized chunks.

- [ ] **Step 2: Write failing resilience tests**

Cover idle timeout, hard timeout, context overflow, one buffered fallback before
any stable event, retry after a transient pre-boundary failure, no retry/fallback
after a text/tool stable boundary, and cancellation closing the iterator.

- [ ] **Step 3: Implement raw-stream consumption and tool-delta accumulation**

Keep Provider-specific parsing in adapters. The Gateway consumes normalized
chunks, emits immutable events, and returns a terminal outcome with complete
tool calls.

- [ ] **Step 4: Implement stream attempt policy**

Use `ProviderAttemptController` with a per-call stable-boundary flag. Buffered
fallback calls the same internal buffered-attempt function exactly once and
shares usage/cost normalization.

- [ ] **Step 5: Run streaming Gateway tests**

Run: `pytest -q tests/runtime/model_gateway/test_streaming_gateway.py tests/test_final_alignment_closures.py tests/test_providers.py`

Expected: PASS.

### Task 6: Migrate Main Agent streaming path

**Files:**
- Modify: `nz_coder/runtime/loop.py`
- Test: `tests/test_loop_fake.py`
- Test: `tests/test_session_processor.py`
- Test: `tests/runtime/model_gateway/test_main_child_parity.py`

**Interfaces:**
- Consumes: Gateway stream events and `ModelCallOutcome`.
- Produces: existing `LLMResult` compatibility projection and identical Session/tool ordering.

- [ ] **Step 1: Add failing main/child parity scenarios**

Parameterize buffered child and streaming/non-streaming main calls over success,
429-then-success, context overflow, malformed 400, timeout, cancellation, and
usage/cost. Assert normalized status and retry count match where call modes
permit equivalent behavior.

- [ ] **Step 2: Add event-order characterization**

Assert assistant StepStart, reasoning/text/tool parts, tool execution, StepFinish,
retry parts, and terminal state retain their current order for a fake stream.

- [ ] **Step 3: Replace `_call_streaming` Provider access with Gateway events**

Map events to the existing SessionProcessor and stream tool bridge. Remove
Provider retry, timeout, fallback, overflow, and usage extraction branches from
AgentLoop only after Gateway parity tests are green.

- [ ] **Step 4: Make `_call_non_streaming` a typed Gateway projection**

Retain the private method for compatibility tests, but remove independent call
policy. Planning and other consumers already use the Gateway from Task 4.

- [ ] **Step 5: Run Main/child/Session tests**

Run: `pytest -q tests/runtime/model_gateway/test_main_child_parity.py tests/test_loop_fake.py tests/test_session_processor.py tests/test_subagent.py`

Expected: PASS.

### Task 7: Remove duplicate paths and enforce architecture

**Files:**
- Modify: `nz_coder/runtime/subagent.py`
- Modify: `nz_coder/runtime/loop.py`
- Create: `tests/runtime/model_gateway/test_architecture.py`
- Modify: `docs/infcode-alignment-learning-log.md`

**Interfaces:**
- Consumes: all migrated Gateway consumers.
- Produces: an enforceable single Agent Core model boundary.

- [ ] **Step 1: Delete obsolete child and Loop helpers**

Remove `_completion_with_timeout`, `_record_subagent_usage`, duplicated retry
loops, Loop usage normalization bodies, and direct runtime Provider call sites
whose consumers migrated. Keep compatibility wrappers only when an existing
external/test import requires them, and make each wrapper delegate to Gateway
code.

- [ ] **Step 2: Add an AST architecture test**

Reject `.create_completion()`, `.chat.completions.create()`, and
`.responses.create()` in `nz_coder/runtime`, `nz_coder/state`, and `nz_coder/vision.py`
outside `runtime/model_gateway`. Permit Provider adapter modules and
`evaluation/provider_smoke.py` only.

- [ ] **Step 3: Run direct-call and focused architecture tests**

Run: `pytest -q tests/runtime/model_gateway/test_architecture.py tests/runtime/model_gateway`

Expected: PASS with zero forbidden call sites.

- [ ] **Step 4: Run a provider-free Main and child trace**

Use the same fake Provider sequence for one Main and one child turn. Assert
Gateway call start/retry/finish facts, parent-child identity, terminal Session
state, normalized usage, and no late event after cancellation.

- [ ] **Step 5: Update the learning log truthfully**

Record exact producer→Gateway→consumer chains and mark Phase 2
`trace_verified` only if both production paths and the architecture guard pass.

### Task 8: Final verification

**Files:**
- Verify every Phase 2 file.

**Interfaces:**
- Consumes: all tasks.
- Produces: the safe boundary for Provider-independent shared AgentRunner work.

- [ ] **Step 1: Run model-gateway and affected suites**

Run: `pytest -q tests/runtime/model_gateway tests/test_providers.py tests/test_recovery.py tests/test_model_capabilities.py tests/test_model_registry.py tests/test_loop_fake.py tests/test_subagent.py tests/test_context_budget.py tests/test_memory.py tests/test_sidecar_verifier.py tests/test_image_describe.py tests/test_session_processor.py`

Expected: PASS.

- [ ] **Step 2: Run static checks**

Run: `python -m compileall -q nz_coder/runtime/model_gateway nz_coder/runtime nz_coder/state nz_coder/vision.py tests/runtime/model_gateway`

Run: `ruff check nz_coder/runtime/model_gateway nz_coder/runtime/loop.py nz_coder/runtime/subagent.py nz_coder/state/context.py nz_coder/state/memory.py nz_coder/runtime/sidecar_verifier.py nz_coder/vision.py tests/runtime/model_gateway`

Expected: both commands exit 0.

- [ ] **Step 3: Run the complete suite**

Run: `pytest -q`

Expected: PASS with no new failure.

- [ ] **Step 4: Review completion boundary**

Confirm every Agent Core Provider call is Gateway-owned, main and child share
policy, no compatibility facade changed shape, and the production trace proves
the new chain. If any consumer still bypasses the Gateway, report Phase 2 as
`wired` or `contract_verified`, not `trace_verified`.
