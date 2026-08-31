# Strict Behavioral Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require a successful, non-empty targeted behavioral check after the latest source mutation in strict SWE mode while preserving static-only completion outside strict mode.

**Architecture:** `VerificationManager` remains the owner of completion state. The planner declares a stage-level targeted evidence requirement without promoting advisory commands, output parsing rejects zero-test successes, the strict stop hook turns the pending stage into actionable model guidance, and the SWE orchestrator maps only environment-blocked non-empty patches to `risky`.

**Tech Stack:** Python 3.9+, standard library, pytest; no new runtime dependencies or Agent frameworks.

**Spec:** `docs/superpowers/specs/2026-08-30-strict-behavioral-verification-design.md`

## Global Constraints

- Apply the new invariant only to `VerificationManager(require_targeted=True)`.
- Preserve ordinary terminal, HTTP, SDK, child-Agent, and non-strict evaluation behavior.
- Keep repository-inferred and filename-inferred related tests advisory and never auto-execute them.
- Do not weaken strict Bash policy or enable SWE Auto mode.
- Do not add external dependencies or an Agent framework.
- Keep Python 3.9 compatibility and existing public tool interfaces.
- Preserve all pre-existing workspace changes; do not reset, clean, or create a worktree that omits the current implementation.
- Do not commit modified production or test files because they already contain user changes; verify the scoped diff instead.

---

### Task 1: Strict Targeted Evidence State Machine

**Files:**
- Modify: `nz_coder/intelligence/verification_planner.py`
- Modify: `nz_coder/intelligence/verification.py`
- Test: `tests/test_verification_planner.py`
- Test: `tests/test_verification.py`

**Interfaces:**
- Consumes: `plan_verification_commands(..., require_targeted: bool = False) -> dict`, `VerificationManager.observe_bash(...)`, and the existing staged pipeline schema.
- Produces: `verification_output_has_no_tests(output: str) -> bool`; targeted stage field `evidence_required: bool`; strict completion based on passed targeted observed/command evidence.

- [ ] **Step 1: Write failing planner tests for stage-level evidence**

Add assertions to the strict planner test while preserving the advisory command contract:

```python
targeted = next(stage for stage in plan["stages"] if stage["name"] == "targeted")
assert targeted["required"] is True
assert targeted["evidence_required"] is True
assert targeted["commands"][0]["required"] is False
```

Add table-driven parsing coverage:

```python
@pytest.mark.parametrize("output", [
    "no tests ran in 0.01s",
    "collected 0 items",
    "Ran 0 tests in 0.000s",
    "Found 0 test(s).",
])
def test_verification_output_has_no_tests(output):
    from nz_coder.verification_planner import verification_output_has_no_tests

    assert verification_output_has_no_tests(output)
```

- [ ] **Step 2: Run planner tests and confirm RED**

Run: `pytest -q tests/test_verification_planner.py::test_strict_plan_keeps_inferred_related_test_advisory tests/test_verification_planner.py::test_verification_output_has_no_tests`

Expected: failure because strict targeted stages lack `evidence_required`/stage `required`, and the output helper does not exist.

- [ ] **Step 3: Implement planner metadata and zero-test parsing**

Add the parser near `verification_output_failed`:

```python
def verification_output_has_no_tests(output: str) -> bool:
    """Return True when a successful test command explicitly ran zero tests."""
    cleaned = str(output or "")
    return any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in (
        r"\bno tests ran\b",
        r"\bcollected\s+0\s+items?\b",
        r"\bran\s+0\s+tests?\b",
        r"\bfound\s+0\s+tests?\s*\(s\)",
    ))
```

Build stages with stage-level strict metadata while leaving command dictionaries unchanged:

```python
stages = []
for stage in VERIFICATION_STAGE_ORDER:
    command_required = any(bool(item.get("required")) for item in stage_commands[stage])
    evidence_required = bool(require_targeted and stage == "targeted")
    stages.append({
        "name": stage,
        "required": command_required or evidence_required,
        "evidence_required": evidence_required,
        "commands": stage_commands[stage],
    })
```

- [ ] **Step 4: Run planner tests and confirm GREEN**

Run: `pytest -q tests/test_verification_planner.py`

Expected: all planner tests pass; inferred commands remain `required=False`.

- [ ] **Step 5: Write failing manager tests for strict completion**

Replace the old strict static-only expectation and add focused cases:

```python
def test_strict_static_pass_keeps_targeted_evidence_pending():
    vm = make_vm(
        staged_plan(static=("python -m py_compile pkg/module.py",)),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert vm.should_gate()
    assert targeted["required"] is True
    assert targeted["evidence_required"] is True
    assert targeted["status"] == "pending"
    assert vm.status()["verification_pipeline"]["next_required_stage"] == "targeted"


def test_strict_model_selected_target_pass_clears_gate():
    vm = make_vm(
        staged_plan(static=("python -m py_compile pkg/module.py",)),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")
    vm.observe_bash(
        {"command": "pytest tests/test_module.py::test_fix"},
        "1 passed in 0.01s",
        False,
        False,
    )

    assert not vm.should_gate()


def test_strict_zero_test_target_does_not_clear_gate():
    vm = make_vm(
        staged_plan(static=("python -m py_compile pkg/module.py",)),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")
    vm.observe_bash(
        {"command": "pytest tests/test_module.py::test_missing"},
        "collected 0 items\n\nno tests ran in 0.01s",
        False,
        False,
    )

    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert vm.should_gate()
    assert targeted["status"] == "pending"
    assert targeted["observed"][0]["status"] == "skipped"
```

Retain a non-strict control proving the same static command still clears its gate.

- [ ] **Step 6: Run manager tests and confirm RED**

Run: `pytest -q tests/test_verification.py -k 'strict_static_pass_keeps_targeted_evidence_pending or strict_model_selected_target_pass_clears_gate or strict_zero_test_target_does_not_clear_gate or strict_manager_does_not_gate_on_inferred_related_target'`

Expected: strict static-only cases incorrectly complete, and zero-test output is recorded as passed.

- [ ] **Step 7: Implement minimal strict manager logic**

When loading stages, keep the stage invariant separate from exact command requirements:

```python
evidence_required = bool(stage.get("evidence_required"))
self._pipeline[name] = {
    "name": name,
    "evidence_required": evidence_required,
    "commands": commands,
    "observed": [],
}
```

Before recording targeted segments in `observe_bash`, convert explicit empty success to skipped:

```python
if (
    status == "passed"
    and segment_stage == "targeted"
    and verification_output_has_no_tests(output)
):
    segment_status = "skipped"
else:
    segment_status = status
self._record_verification_result(segment_stage, segment_status, segment_command)
```

Recompute completion using both exact required commands and stage-level evidence:

```python
targeted_state = self._pipeline.get("targeted") or {}
targeted_evidence_passed = any(
    item.get("status") == "passed"
    for item in targeted_state.get("commands", []) + targeted_state.get("observed", [])
)
strict_targeted_complete = (
    not targeted_state.get("evidence_required") or targeted_evidence_passed
)
complete = command_requirements_complete and strict_targeted_complete
```

Expose `evidence_required` in `_pipeline_snapshot()`. Make a targeted evidence stage `required=True/status="pending"` until positive evidence exists, even when all its commands are advisory. Update `_format_pipeline_status()` so the no-command strict case asks for one direct narrow behavioral test that runs at least one test; do not promote or execute a suggested command.

- [ ] **Step 8: Run manager tests and confirm GREEN**

Run: `pytest -q tests/test_verification.py tests/test_verification_planner.py`

Expected: all focused verification tests pass, including non-strict compatibility and inferred-advisory coverage.

### Task 2: Strict Stop-Hook Guidance

**Files:**
- Modify: `nz_coder/runtime/hooks.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `runtime_state["verification"]["verification_pipeline"]["stages"]` and targeted `evidence_required/status` from Task 1.
- Produces: a bounded `StopHookDecision(action="reanimate")` message that requests a direct non-empty targeted behavior check; preserves `complete_unverified` for environment blockers.

- [ ] **Step 1: Write the failing stop-hook test**

Add a strict state with no exact target command:

```python
def test_strict_generation_hook_requests_non_empty_targeted_evidence():
    context = StopHookContext(
        transcript=(),
        last_assistant_text="done",
        runtime_state={
            "mutation_generation": 2,
            "strict_generation_ready": False,
            "verification": {
                "verification_needed": True,
                "verification_pipeline": {"stages": [{
                    "name": "targeted",
                    "required": True,
                    "evidence_required": True,
                    "status": "pending",
                    "commands": [],
                }]},
            },
        },
    )

    with scoped_runtime_overrides(strict_local_tools=True):
        decision = strict_generation_stop_hook(context)

    assert decision.action == "reanimate"
    assert "direct narrow behavioral test" in decision.message
    assert "at least one test" in decision.message
```

- [ ] **Step 2: Run the hook test and confirm RED**

Run: `pytest -q tests/test_hooks.py::test_strict_generation_hook_requests_non_empty_targeted_evidence`

Expected: the current generic stop message lacks the targeted non-empty guidance.

- [ ] **Step 3: Implement targeted pending detection and guidance**

Detect stage-level evidence independently of exact pending commands:

```python
targeted_evidence_pending = any(
    stage.get("name") == "targeted"
    and stage.get("evidence_required") is True
    and stage.get("status") != "passed"
    for stage in pipeline.get("stages") or []
)
```

If pending, append:

```python
" Run one direct narrow behavioral test that exercises the changed behavior, "
"and confirm that it executes at least one test."
```

Keep the existing `diff_status` and `verify_changed_files` instructions and the `blocked_environment` branch unchanged.

- [ ] **Step 4: Run hook tests and confirm GREEN**

Run: `pytest -q tests/test_hooks.py`

Expected: all hook tests pass, including exact-command hints and `complete_unverified` behavior.

### Task 3: SWE Result Classification

**Files:**
- Modify: `nz_coder/swebench/orchestrator.py`
- Test: `tests/test_swebench_lite.py`

**Interfaces:**
- Consumes: `agent_status.verification_needed`, `agent_status.verification_state`, `model_patch`, `blocking_risk`, and current risk labels.
- Produces: `_benchmark_result_status(...) -> Literal["agent_failed", "empty_patch", "risky", "completed"]` behavior matching the design.

- [ ] **Step 1: Write failing status-classification tests**

Add the environment-blocked case and preserve the empty-patch precedence:

```python
def test_environment_blocked_non_empty_patch_is_risky():
    status = _benchmark_result_status(
        {
            "status": "completed_unverified",
            "verification_needed": True,
            "verification_state": "blocked_environment",
        },
        model_patch="diff --git a/x.py b/x.py\n",
        risk_reasons=["agent_status:completed_unverified", "verification_needed"],
    )
    assert status == "risky"


def test_environment_blocked_empty_patch_stays_empty_patch():
    status = _benchmark_result_status(
        {
            "status": "completed_unverified",
            "verification_needed": True,
            "verification_state": "blocked_environment",
        },
        model_patch="",
        risk_reasons=["verification_needed"],
    )
    assert status == "empty_patch"
```

Keep `test_unverified_patch_is_not_publishable` as the control for missing targeted evidence.

- [ ] **Step 2: Run status tests and confirm RED**

Run: `pytest -q tests/test_swebench_lite.py -k 'environment_blocked_non_empty_patch_is_risky or environment_blocked_empty_patch_stays_empty_patch or unverified_patch_is_not_publishable'`

Expected: environment-blocked cases currently return `agent_failed` because `verification_needed` is checked before patch emptiness and blocker semantics.

- [ ] **Step 3: Implement minimal classification ordering**

Preserve terminal failure priority, then classify the patch and blocker:

```python
if terminal in {"aborted", "error", "cancelled", "timeout", "exception", "max_turns"}:
    return "agent_failed"
if not str(model_patch or "").strip():
    return "empty_patch"
if blocking_risk:
    return "agent_failed"
if (agent_status or {}).get("verification_needed"):
    if (agent_status or {}).get("verification_state") == "blocked_environment":
        return "risky"
    return "agent_failed"
return "risky" if risk_reasons else "completed"
```

- [ ] **Step 4: Run SWE tests and confirm GREEN**

Run: `pytest -q tests/test_swebench_lite.py tests/test_swebench_strict.py`

Expected: all orchestrator and strict-policy tests pass.

### Task 4: Integrated Verification and Bounded SWE Recheck

**Files:**
- Verify only; no planned source edits.

**Interfaces:**
- Consumes: all behavior implemented in Tasks 1–3.
- Produces: fresh local regression evidence and fresh official-harness results for the two previously unresolved instances.

- [ ] **Step 1: Run focused cross-boundary regressions**

Run:

```bash
pytest -q tests/test_verification.py tests/test_verification_planner.py
pytest -q tests/test_hooks.py
pytest -q tests/test_swebench_lite.py tests/test_swebench_strict.py
```

Expected: zero failures.

- [ ] **Step 2: Run repository verification**

Run:

```bash
pytest -q
ruff check nz_coder tests
python -m compileall -q nz_coder tests
git diff --check
```

Expected: every command exits zero. If unrelated pre-existing failures occur, record their exact tests/files and separately prove the touched-file scope.

- [ ] **Step 3: Inspect the final scoped diff**

Run:

```bash
git diff -- nz_coder/intelligence/verification.py nz_coder/intelligence/verification_planner.py nz_coder/runtime/hooks.py nz_coder/swebench/orchestrator.py tests/test_verification.py tests/test_verification_planner.py tests/test_hooks.py tests/test_swebench_lite.py
git diff --check -- nz_coder/intelligence/verification.py nz_coder/intelligence/verification_planner.py nz_coder/runtime/hooks.py nz_coder/swebench/orchestrator.py tests/test_verification.py tests/test_verification_planner.py tests/test_hooks.py tests/test_swebench_lite.py
```

Expected: only the strict verification closure is newly added; pre-existing edits are preserved.

- [ ] **Step 4: Rerun the two representative SWE instances**

Use fresh run IDs/output directories and no resume state for:

```text
django__django-11283
pytest-dev__pytest-6116
```

Run the existing bounded real-provider SWE command discovered from project documentation/run artifacts, then generate a fresh predictions JSONL and run the official Docker harness with the repository's established command. Do not reuse old inference results.

Expected: each trace truthfully contains a passed non-empty targeted behavior check, or ends as `agent_failed`/environment-blocked `risky`; no static-only `completed` outcome is accepted. Official resolution is measured and reported but is not a required condition for accepting the runtime fix.
