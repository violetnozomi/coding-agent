# Agent Core Sidecar Verifier Source-Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put InfCodeX's complete Sidecar Verifier contract into NZ-Coder's real Main Agent natural-stop path.

**Architecture:** Add one domain-neutral structured LLM judge and one coding-specific verifier module. Extend the existing StopHook surface with an async consumer, bind the verifier after Main Agent Provider construction, and preserve the strict generation hook as the next consumer.

**Tech Stack:** Python 3.9+, standard library, existing NZ Provider abstraction, pytest; no Agent framework and no new runtime dependency.

## Global Constraints

- Preserve existing public tool names, schemas, and result strings.
- Do not issue a paid Provider request or run SWE-bench during implementation verification.
- Main Agent Provider errors, timeout, missing report output, cancellation, and parse failure must fail open.
- Verifier code has no workspace mutation capability.
- Every production behavior is implemented only after observing its focused test fail.
- Preserve the user's existing dirty worktree; do not create commits, branches, or worktrees.

---

### Task 1: Domain-neutral LLM judge kernel

**Files:**
- Create: `nz_coder/runtime/llm_judge.py`
- Create: `tests/test_llm_judge.py`

**Interfaces:**
- Consumes: a callable `invoke(request: JudgeRequest) -> JudgeResponse`, parser, default-verdict factory, timeout, and optional cancellation event.
- Produces: `edit_distance(a: str, b: str) -> int`, `find_fuzzy_tool_match(blocks, expected)`, `JudgeRequest`, `JudgeResponse`, and `invoke_llm_judge`.

- [ ] **Step 1: Write failing edit-distance and fuzzy-match tests**

```python
def test_fuzzy_match_prefers_exact_then_nearest_within_two_edits():
    blocks = [
        {"name": "emit_sidecar_verdct", "input": {"verdict": "revise"}},
        {"name": "other", "input": {}},
    ]
    match = find_fuzzy_tool_match(blocks, "emit_sidecar_verdict")
    assert match == (blocks[0], False)
    assert find_fuzzy_tool_match(
        [{"name": "emit_sidecar_verdict", "input": {}}],
        "emit_sidecar_verdict",
    )[1] is True
```

- [ ] **Step 2: Run the focused test and confirm the missing module/API failure**

Run: `python -m pytest -q tests/test_llm_judge.py`

- [ ] **Step 3: Implement the deterministic helpers and typed request/response dataclasses**

```python
@dataclass(frozen=True)
class JudgeRequest:
    system_prompt: str
    user_message: str
    report_tool: dict[str, Any]
    report_tool_name: str
    max_output_tokens: int = 1024
```

- [ ] **Step 4: Write failing never-throw tests for success, timeout, cancellation, Provider error, no report tool, and parser error**

```python
def test_provider_error_maps_through_default_verdict():
    result = invoke_llm_judge(
        request=REQUEST,
        invoke=lambda _request: (_ for _ in ()).throw(RuntimeError("offline")),
        parse_tool_call=lambda block, exact: block["input"],
        default_verdict=lambda reason: {"verdict": "accept", "trace": reason},
        timeout_seconds=0.1,
    )
    assert result == {"verdict": "accept", "trace": "provider_error"}
```

- [ ] **Step 5: Implement a daemon-backed deadline owner that ignores late results and returns caller defaults**

```python
def invoke_llm_judge(*, request, invoke, parse_tool_call, default_verdict,
                     timeout_seconds=15.0, cancel_event=None):
    outcomes = queue.Queue(maxsize=1)
    def worker():
        try:
            outcomes.put(("response", invoke(request)))
        except Exception as exc:
            outcomes.put(("error", exc))
    threading.Thread(target=worker, daemon=True).start()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return default_verdict("cancelled")
        try:
            kind, value = outcomes.get(timeout=min(0.02, deadline - time.monotonic()))
        except queue.Empty:
            continue
        if kind == "error":
            return default_verdict("provider_error")
        match = find_fuzzy_tool_match(value.tool_blocks, request.report_tool_name)
        if match is None:
            return default_verdict("no_tool_call")
        try:
            return parse_tool_call(*match)
        except Exception:
            return default_verdict("parse_failure")
    return default_verdict("timeout")
```

- [ ] **Step 6: Run `tests/test_llm_judge.py` and keep every translated contract green**

---

### Task 2: Coding verifier prompt, context, gate, and verdict mapping

**Files:**
- Create: `nz_coder/runtime/sidecar_verifier.py`
- Create: `tests/test_sidecar_verifier.py`

**Interfaces:**
- Consumes: `StopHookContext`, `ChangeTracker`, objective `VerifierGateMetrics`, and `invoke_llm_judge`.
- Produces: `VerifierVerdict`, `VerifierGateMetrics`, `build_verifier_context`, `build_verifier_user_message`, `compose_gate_decision`, `parse_verifier_report`, and `map_verdict_to_stop_decision`.

- [ ] **Step 1: Write failing context tests for real-user filtering, 24-message rolling window, file evidence, and exact final text**

```python
def test_context_ignores_synthetic_followups_and_bounds_recent_transcript():
    transcript = [{"role": "user", "content": "fix parser"}]
    transcript += [{"role": "assistant", "content": str(i)} for i in range(30)]
    transcript += [{"role": "user", "content": "internal", "_nz_synthetic": True}]
    context = build_verifier_context(transcript, "done", file_edits=[])
    assert context.current_turn_user_queries == ("fix parser",)
    assert len(context.recent_transcript) == 24
    assert context.last_assistant_text == "done"
```

- [ ] **Step 2: Confirm the context tests fail because the module is absent**

Run: `python -m pytest -q tests/test_sidecar_verifier.py -k context`

- [ ] **Step 3: Implement prompt assets, transcript rendering, truncation, and immutable context dataclasses**

```python
@dataclass(frozen=True)
class VerifierContext:
    current_turn_user_queries: tuple[str, ...]
    recent_transcript: tuple[dict[str, Any], ...]
    file_edit_summary: tuple[dict[str, str], ...]
    last_assistant_text: str
    additional_criteria: str = ""
```

- [ ] **Step 4: Write failing gate tests for escape hatch, risky work, plan, rounds, multi-file, large edit, trivial observed work, greeting, and no-action claim**

```python
def test_gate_defaults_to_fire_for_completion_claim_without_tool_evidence():
    decision = compose_gate_decision(
        transcript=({"role": "user", "content": "implement endpoint"},),
        metrics=VerifierGateMetrics(),
        env={},
    )
    assert decision.fire is True
    assert decision.reason.startswith("default:")
```

- [ ] **Step 5: Implement the gate in exact first-match order with thresholds 10 rounds and 20 changed lines**

- [ ] **Step 6: Write failing verdict tests for accept/revise/blocked, missing reason, invalid value, and retrospective text**

```python
def test_revise_maps_to_attributed_reanimation():
    decision = map_verdict_to_stop_decision(
        VerifierVerdict("revise", "Add the missing import.", trace="verifier_ok")
    )
    assert decision.action == "reanimate"
    assert decision.source == "sidecar-verifier"
    assert "Add the missing import" in decision.message
    assert "failed Sidecar Verifier review" in decision.message
```

- [ ] **Step 7: Implement strict parsing and the three-state StopHook mapping, then run the complete verifier unit file**

Run: `python -m pytest -q tests/test_sidecar_verifier.py`

---

### Task 3: Async StopHook lifecycle

**Files:**
- Modify: `nz_coder/runtime/hooks.py`
- Modify: `nz_coder/runtime/loop.py`
- Test: `tests/test_hooks.py`
- Test: `tests/test_loop_fake.py`

**Interfaces:**
- Consumes: synchronous or awaitable `StopHook` results.
- Produces: `AgentHooks.handle_no_tool_response_async(loop, messages, message="")` and `_run_stop_hooks_async(loop, messages, message="")` with the same complete/reanimate/abort normalization and budget as the synchronous path.

- [ ] **Step 1: Write a failing async-hook test proving the assistant text is already in the immutable transcript and an awaited revise injects one stamped user follow-up**

```python
async def verifier(ctx):
    assert ctx.transcript[-1]["role"] == "assistant"
    return StopHookDecision("reanimate", "fix it", "sidecar-verifier")
```

- [ ] **Step 2: Run the hook test and confirm the async API is missing**

- [ ] **Step 3: Add awaitable detection and a shared decision-application helper so sync/async paths cannot drift**

- [ ] **Step 4: Write failing loop tests for async accept, revise-then-accept, blocked, thrown exception fail-open, and two-revise exhaustion**

- [ ] **Step 5: Change the real async Agent loop natural-stop branch to await `handle_no_tool_response_async` after step persistence**

- [ ] **Step 6: Run hook and loop tests**

Run: `python -m pytest -q tests/test_hooks.py tests/test_loop_fake.py -k 'stop_hook or sidecar'`

---

### Task 4: Provider adapter, resolver, and production assembly

**Files:**
- Modify: `nz_coder/runtime/sidecar_verifier.py`
- Modify: `nz_coder/runtime/loop.py`
- Modify: `nz_coder/runtime/composition.py`
- Test: `tests/test_sidecar_verifier.py`
- Test: `tests/test_runtime_composition.py`
- Test: `tests/test_loop_fake.py`

**Interfaces:**
- Consumes: Main Agent Provider/client/model or both `KODAX_VERIFIER_PROVIDER` and `KODAX_VERIFIER_MODEL`.
- Produces: `resolve_verifier_provider`, `create_sidecar_verifier_stop_hook`, and an Agent-owned verifier handle with `close()`.

- [ ] **Step 1: Write failing resolver tests for paired explicit override, partial override fallback, unknown Provider fallback, and inherit-main**

- [ ] **Step 2: Implement resolver records containing provider, client, model, provider name, source, and ownership**

- [ ] **Step 3: Write a failing Provider adapter test asserting one forced `emit_sidecar_verdict` report tool, 1024 output-token cap, isolated two-message context, and no coding tools**

- [ ] **Step 4: Implement Provider response normalization for tool-call and strict JSON compatibility responses**

- [ ] **Step 5: Write a failing assembly test proving first-party Agent construction installs verifier before `strict_generation_stop_hook`, while explicit injected-client tests remain network-inert unless opted in**

- [ ] **Step 6: Bind the verifier after Provider initialization, track metrics, emit typed trace events, and close only owned override clients**

- [ ] **Step 7: Add verifier counters and last trace to `_runtime_summary()` and `run_end.runtime`**

- [ ] **Step 8: Run provider, composition, hook, and loop tests**

---

### Task 5: Provider-free production traces and documentation

**Files:**
- Create: `tests/test_sidecar_verifier_trace.py`
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: `docs/swebench-progress.md`

**Interfaces:**
- Consumes: assembled Agent Core with an injected deterministic verifier Provider.
- Produces: public trace evidence for accept, revise-then-accept, blocked, and fail-open paths.

- [ ] **Step 1: Write four end-to-end trace tests that instantiate the real Agent, persist assistant/tool parts, and assert terminal ordering**

```python
assert [row["event"] for row in rows if row["event"].startswith("sidecar_")] == [
    "sidecar_gate_decision", "sidecar_started", "sidecar_finished"
]
```

- [ ] **Step 2: Run those tests and correct only production-order defects they expose**

- [ ] **Step 3: Run focused Core verification**

Run: `python -m pytest -q tests/test_llm_judge.py tests/test_sidecar_verifier.py tests/test_sidecar_verifier_trace.py tests/test_hooks.py tests/test_runtime_composition.py tests/test_loop_fake.py`

- [ ] **Step 4: Run full verification**

Run: `python -m pytest -q`

- [ ] **Step 5: Run static verification**

Run: `python -m ruff check nz_coder/runtime/llm_judge.py nz_coder/runtime/sidecar_verifier.py nz_coder/runtime/hooks.py nz_coder/runtime/loop.py tests/test_llm_judge.py tests/test_sidecar_verifier.py tests/test_sidecar_verifier_trace.py`

Run: `python -m py_compile nz_coder/runtime/llm_judge.py nz_coder/runtime/sidecar_verifier.py nz_coder/runtime/hooks.py nz_coder/runtime/loop.py`

- [ ] **Step 6: Update both development memories with exact upstream map, production consumer, test counts, trace evidence, and remaining overall Agent Core gaps**

- [ ] **Step 7: Mark this subproject `trace_verified` only if all preceding commands exit zero**
