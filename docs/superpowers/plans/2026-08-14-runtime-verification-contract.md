# Runtime-Owned Verification Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically execute an explicitly requested workspace-local pytest command once per mutation generation during Agent convergence.

**Architecture:** A focused verification-contract module parses and tracks run-local contracts. RuntimeState persists its JSON-safe state, and Runner invokes it through the existing tool execution boundary when WorkBudget crosses yellow/orange/red or immediately before an earlier natural completion.

**Tech Stack:** Python 3.9+, standard library, existing NZ-Coder Runtime/tool registry, pytest.

## Global Constraints

- No new dependency or Agent framework.
- All commands remain subject to workspace safety, permission, cancellation, timeout, trace, and Bash policy.
- Do not auto-run pathless or inferred full-repository suites.
- One automatic attempt per mutation generation.
- Preserve existing public tool names and result formats.

---

### Task 1: Contract parsing and state

**Files:**
- Create: `nz_coder/runtime/verification_contract.py`
- Test: `tests/runtime/test_verification_contract.py`

**Interfaces:**
- Produces: `extract_verification_contract(text) -> VerificationContract | None`
- Produces: `VerificationContract.is_due(*, zone, has_diff, mutation_generation) -> bool`
- Produces: `VerificationContract.record_attempt(generation, passed, output) -> None`

- [x] Write tests for safe pytest extraction, punctuation boundaries, unsafe/pathless rejection, once-per-generation attempts, and re-arming after mutation.
- [x] Run the tests and confirm they fail because the module does not exist.
- [x] Implement the minimal dataclass/parser/state behavior.
- [x] Run the tests and confirm they pass.

### Task 2: RuntimeState persistence

**Files:**
- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: `nz_coder/runtime/run_lifecycle.py`
- Test: `tests/test_runtime_context.py`

**Interfaces:**
- Consumes: `VerificationContract.to_dict()` / `from_dict()`.
- Produces: `RuntimeState.verification_contract` as JSON-safe state.

- [x] Write a failing lifecycle test proving a real user command is captured and survives RuntimeState serialization/restore.
- [x] Bind contract initialization without mutating global configuration.
- [x] Run lifecycle/runtime-state tests.

### Task 3: Runner convergence execution

**Files:**
- Modify: `nz_coder/runtime/runner.py`
- Test: `tests/runtime/test_native_runner.py`

**Interfaces:**
- Consumes: current work-budget zone, diff state, mutation generation, and normal tool execution services.
- Produces: synthetic `verification-contract` evidence before the following model call.

- [x] Write a failing native Runner test with a diff and explicit contract at yellow pressure.
- [x] Assert automatic Bash execution occurs once, output reaches the next model request, and unchanged generations do not rerun.
- [x] Implement the smallest Runner hook through the existing tool execution boundary.
- [x] Add early-completion/re-edit coverage and run native Runner tests.

### Task 4: Regression and real-product acceptance

**Files:**
- Modify: `docs/terminal-product-real-world-issues.md`
- Modify: `docs/infcode-alignment-learning-log.md`

- [x] Run focused verification-contract, runtime-state, Bash-policy, and native Runner tests.
- [x] Run the complete pytest suite and repair the cardinality-only architecture guard exposed by the third valid tool path.
- [x] Run a fresh isolated `nz-coder run` edit task with an explicit Chinese pytest clause.
- [x] Inspect trace for model/tool counts, automatic contract events, command result, and terminal status.
- [x] Fix DeepSeek V4 replay for synthetic assistant turns at the provider projection boundary.
- [x] Record honest evidence and leave TP-025 at verify because this bounded smoke is not the original long-task numerical gate.
