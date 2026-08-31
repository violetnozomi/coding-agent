# Real-Provider Contract Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate contract-led execution by default without adding a Provider call, survive invalid planner JSON, provide correct nested project facts, and account for all Provider calls.

**Architecture:** Bootstrap a conservative Runtime-owned contract before optional planning, then let valid planner output enrich it. Treat contract structure as complexity evidence, improve nested Python project facts, and aggregate ModelGateway terminal observations by purpose while preserving execution-turn metrics.

**Tech Stack:** Python 3.9+, standard library, pytest, existing ModelGateway/RuntimeState/Runner abstractions.

## Global Constraints

- No new Provider call in the default path.
- No external dependency or Agent framework.
- No public tool-interface change.
- Invalid planner output must retain the deterministic contract.
- Provider usage must be counted exactly once.
- No Git mutation in the shared workspace.

---

### Task 1: Contract bootstrap and planner fallback

**Files:**
- Modify: `nz_coder/runtime/task_contract.py`
- Modify: `nz_coder/runtime/loop.py`
- Test: `tests/runtime/test_task_contract.py`
- Test: `tests/test_loop_fake.py`

**Interfaces:**
- Produces: `derive_task_contract(task_text, acceptance_command, workspace) -> TaskContract`.
- Consumes: initial user text, exact VerificationContract command, and optional planner output.

- [x] Write failing tests for default zero-call bootstrap and malformed planner retention.
- [x] Run the exact tests and confirm behavioral failures.
- [x] Implement bounded intent-derived requirements and initialize them before the planning flag check.
- [x] Request JSON object from supported providers and retain bootstrap contract on every planner fallback.
- [x] Run the focused contract/loop tests to green.

### Task 2: Bundle activation and nested project facts

**Files:**
- Modify: `nz_coder/intelligence/implementation_bundle.py`
- Modify: `nz_coder/intelligence/project_profile.py`
- Test: `tests/test_implementation_bundle.py`
- Test: `tests/test_project_profile.py`

**Interfaces:**
- Produces: a first-turn bundle for contracts with at least three requirements and nested project facts with correct module cwd/test roots.
- Consumes: the TaskContract bootstrap and active workspace.

- [x] Write failing tests using the real `workspace/cron_engine/{pyproject.toml,__init__.py,tests}` layout.
- [x] Confirm simple text complexity currently suppresses the bundle and nested test facts are missing.
- [x] Make contract structure an activation signal and detect the single nested Python project without escaping workspace.
- [x] Run focused bundle/profile tests to green.

### Task 3: Complete Provider accounting

**Files:**
- Modify: `nz_coder/runtime/model_gateway/gateway.py`
- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: `nz_coder/runtime/loop.py`
- Modify: `nz_coder/runtime/runner.py`
- Test: `tests/test_model_gateway.py`
- Test: `tests/test_runtime_state.py`
- Test: `tests/runtime/test_native_runner.py`

**Interfaces:**
- Produces: purpose-classified Provider call/usage metadata and RunResult usage including control-plane calls once.
- Consumes: every settled `ModelCallOutcome` emitted by ProductionModelGateway.

- [x] Write failing tests for finish observations and planning/coding aggregation.
- [x] Emit normalized outcome facts with the original call purpose.
- [x] Persist per-purpose accounting in RuntimeState and expose it in run metadata.
- [x] Add only non-coding usage to RunContext because Runner already adds coding usage.
- [x] Run accounting/headless/native tests to green.

### Task 4: Verification and records

**Files:**
- Modify: `docs/terminal-product-real-world-issues.md`
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: this plan.

- [x] Run all focused tests for Tasks 1–3 (`215 passed`).
- [x] Run the complete pytest suite (`2224 passed, 21 skipped`).
- [x] Run Ruff on every changed Python file and the whole repository.
- [x] Run `git diff --check`.
- [x] Record exact evidence. The third paid run activated the default contract/bundle
  path but failed independent acceptance, so TP-028/TP-029 remain open/verify.
