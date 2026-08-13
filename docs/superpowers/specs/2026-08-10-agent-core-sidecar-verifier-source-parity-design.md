# Agent Core Sidecar Verifier Source-Parity Design

## Goal

Move InfCodeX's FEATURE_184/196/200/215 Sidecar Verifier from its TypeScript
Runner chain into NZ-Coder's real Main Agent natural-stop path. The delivered
feature must preserve upstream ordering, three-state verdicts, bounded
reanimation, fail-open behavior, provider resolution, context isolation, and
fire-gate semantics. Existing Workflow verification is not a substitute.

## Scope and source map

The implementation is a semantic line-by-line translation of these upstream
owners:

| Upstream source | Contract translated into NZ-Coder |
|---|---|
| `packages/agent/src/runtime-middleware/llm-judge.ts` | Domain-neutral forced structured judge, fuzzy report-tool matching, timeout/cancellation, never-throw fail-open result |
| `packages/coding/src/agent-runtime/middleware/sidecar-verifier/verifier.ts` | `accept/revise/blocked` parser, malformed-result degradation, retrospective revise text, stop-hook mapping |
| `packages/coding/src/agent-runtime/middleware/sidecar-verifier/verifier-prompts.ts` | Third-person role separation, verifier report schema, bounded transcript rendering, verifier user-message sections |
| `packages/coding/src/agent-runtime/middleware/sidecar-verifier/verifier-context-builder.ts` | Current-turn real user queries, last 24 non-system messages, actual file-edit summary, exact final assistant text |
| `packages/coding/src/agent-runtime/middleware/sidecar-verifier/gate.ts` | Always-on escape hatch, work-scale gate, conversational skip, safe default fire |
| `packages/coding/src/agent-runtime/middleware/sidecar-verifier/verifier-provider-resolver.ts` | Explicit provider+model pair override or inherit Main Agent |
| `packages/coding/src/task-engine/runner-sidecar-verifier-adapter.ts` | Sidecar-first stop-hook composition, substantial-work gate, bounded revise, verdict observability |

InfCode-dev remains the stronger reference for its existing exact consecutive
doom-loop permission path. That path is independent and is not changed by this
subproject.

## Architecture

### Domain-neutral LLM judge

Create `nz_coder/runtime/llm_judge.py`. It owns edit distance, exact/fuzzy
report-tool selection, one structured Provider request, a 15-second default
deadline, caller cancellation propagation, and failure classification. It
never raises to the Main Agent: provider errors, timeouts, missing report calls,
and parser errors are converted through a caller-supplied default-verdict
factory.

The Provider request uses the existing NZ Provider abstraction. When a Provider
supports forced tool choice, the judge requests exactly the report tool. A
Provider response that exposes only JSON text is accepted through the same
strict parser as a compatibility boundary, not as a second verdict contract.

### Coding-specific verifier

Create `nz_coder/runtime/sidecar_verifier.py`. It owns the translated prompt,
report schema, context builder, fire gate, verdict parser, provider resolver,
and StopHook factory. It has no workspace write capability and receives a deep
snapshot instead of the live conversation list.

The verifier sees:

- every real user query in the current conversational unit, in full;
- the last 24 non-system transcript messages, rendered as third-person text;
- changed paths and bounded diff hints from `ChangeTracker` current state;
- the exact Main Agent final text;
- objective run metrics from `RuntimeState` and tool observations.

### Async stop-hook surface

Extend `AgentHooks` so a StopHook may return a decision or an awaitable
decision. Keep the existing synchronous entry point for compatibility and add
an async entry point consumed by `AgentLoop._run`. The async path awaits the
verifier without blocking the event loop; configured synchronous hooks retain
their current behavior.

Natural-stop ordering is fixed:

1. persist the Main Agent assistant text and finish the step;
2. build an immutable StopHook snapshot;
3. run the Sidecar Verifier fire gate;
4. if fired, await the independent judge;
5. map `accept` to the next stop consumer, `revise` to one synthetic user
   follow-up, and `blocked` to terminal abort;
6. apply the existing two-reanimation budget;
7. only after acceptance, let the strict generation consumer run.

This preserves InfCodeX's sidecar-first composition. The deterministic strict
consumer remains an NZ SWE adapter and cannot replace the verifier.

## Fire gate

Decision order matches InfCodeX:

1. `KODAX_VERIFIER_ALWAYS=1` always fires.
2. Observable risky/unattributed shell mutation, a committed plan, more than
   10 rounds, two or more changed files, or more than 20 estimated changed
   lines fires.
3. A short grounded read-only lookup or one small single-file edit with no plan
   skips.
4. A real user message of at most 20 Unicode characters that begins with a
   greeting and contains no imperative verb skips.
5. Everything else fires, including a non-greeting completion claim with no
   tool evidence.

Synthetic retry, stop-hook, compaction, and diagnostic user messages do not
replace the real current-turn request.

## Verdict and failure semantics

- `accept`: no interception; continue to the next stop consumer.
- `revise`: inject the reason plus the translated retrospective guidance;
  reanimate the same Main Agent and attribute the trace source to
  `sidecar-verifier`.
- `blocked`: stop and surface the reason without another Provider turn.
- `revise` or `blocked` without a reason degrades to `accept` with
  `missing_reason`.
- Invalid verdicts, missing report calls, parser failures, Provider failures,
  timeout, and cancellation all fail open to `accept`, with distinct trace
  labels.
- Two revise cycles are allowed. A third revise request ends with the existing
  stop-hook budget-exhausted terminal status.

## Provider resolution and activation

Production resolution order matches InfCodeX:

1. If both `KODAX_VERIFIER_PROVIDER` and `KODAX_VERIFIER_MODEL` resolve, create
   that Provider/client and use the explicit model.
2. Otherwise inherit the Main Agent Provider, client, and effective wire model.

The verifier is installed by the Main Agent production constructor. Tests and
SDK embedders may inject a verifier or explicitly disable it; an injected Main
Agent client does not silently make a network verifier call unless the test or
embedder also opts into the verifier. CLI, HTTP, SWE, and normal product entry
points construct their own client and therefore receive the verifier by
default.

This test seam avoids turning provider-free unit tests into network tests while
keeping all first-party production paths aligned.

## State, cancellation, and lifecycle

- The verifier keeps no cross-run conversation state.
- Every invocation receives a deep immutable snapshot.
- The Main Agent's cancellation event propagates to the judge where the
  Provider supports it; timeout remains the hard upper bound.
- Explicit override clients are owned and closed by the verifier handle.
- Main-provider inheritance does not double-close the Main Agent client.
- Verifier output is never added as an assistant-authored Main Agent message;
  only a `revise` reason becomes a stamped synthetic user follow-up.

## Observability

Emit typed trace events for gate decision, verifier start, resolved verdict,
elapsed time, provider/model/source, reanimation, abort, timeout, malformed
output, and fail-open reason. The final `run_end.runtime` includes verifier fire
count, verdict counts, revise count, and last trace label. No prompt body,
credential, or unbounded transcript is written to trace.

## Compatibility and safety

- Existing tool names and schemas remain unchanged.
- The verifier receives no tools that can mutate the workspace.
- No Agent framework or new dependency is added.
- Existing `AgentHooks.handle_no_tool_response` remains available for legacy
  synchronous consumers.
- Ordinary completion latency changes only when the gate fires.
- No paid Provider or SWE run is part of implementation verification.

## Test and completion contract

Every new behavior is developed red-green. Completion requires:

1. translated unit contracts for edit distance, fuzzy matching, verdict
   parsing, prompts, context extraction, gate ordering, and failure defaults;
2. hook tests for accept, revise, blocked, exception, timeout, cancellation,
   and budget exhaustion;
3. assembly tests proving the production Main Agent owns the verifier and the
   ordering is sidecar first, strict consumer second;
4. Provider-free real Agent traces for accept, revise-then-accept, blocked, and
   fail-open paths;
5. focused tests, the full pytest suite, Ruff, `py_compile`, and diff checks;
6. an update to `docs/infcode-alignment-learning-log.md` and
   `docs/swebench-progress.md` that names this subproject `trace_verified` only
   after all evidence exists.

This completion closes the Sidecar Verifier gap only. Overall Agent Core remains
"not fully source-aligned" until later Runner/Observer, role/task engine,
Provider streaming, and context-lifecycle source maps independently reach the
same evidence level.
