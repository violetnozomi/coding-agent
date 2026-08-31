# Long-Horizon Deterministic Runtime Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make terminal settlement, emergency repair, shell outcomes, subprocess recovery, and bootstrap artifact evidence deterministic enough to pass local gates G1-G7 before another paid run.

**Architecture:** Preserve the native runner and existing contracts. Add small pure/deterministic helpers, route all runner terminal boundaries through one settler, and make tool policies consume structured state instead of rendered text.

**Tech Stack:** Python 3.9+, standard library, pytest, Ruff.

---

### Task 1: Lock bootstrap artifact and ledger semantics with failing tests

**Files:**
- Create: `tests/runtime/test_bootstrap_artifacts.py`
- Modify: `tests/runtime/test_task_contract.py`
- Create: `nz_coder/intelligence/bootstrap_artifacts.py`
- Modify: `nz_coder/runtime/task_contract.py`
- Modify: `nz_coder/runtime/loop.py`

1. Add G6 tests using a temporary nested `cron_engine` fixture.
2. Add G7 ledger test proving acceptance cannot satisfy unchanged docs/tests.
3. Run the new tests and confirm they fail for the intended missing behavior.
4. Implement the bounded resolver, split test requirements, and merge soft
   candidates into the implementation bundle.
5. Run the focused tests until green.

### Task 2: Make Bash outcome structural and pipelines reliable

**Files:**
- Modify: `tests/test_tools.py` or the existing Bash test module
- Modify: `tests/test_windows_platform_runtime.py`
- Modify: `nz_coder/runtime/platform_runtime.py`
- Modify: `nz_coder/runtime/tool_executor.py`
- Modify: `nz_coder/runtime/subagent.py`
- Modify: `nz_coder/tools/bash.py`

1. Add G4 tests for Bash pipefail, metadata-driven command failure, and `sh`
   verification-pipeline rejection.
2. Confirm the tests fail against current behavior.
3. Add Bash `-o pipefail`, preserve `sh -lc`, and centralize structured failure
   classification for parent and child executors.
4. Run focused shell and executor tests.

### Task 3: Add deterministic subprocess workspace diagnostics

**Files:**
- Create: `nz_coder/intelligence/subprocess_workspace.py`
- Modify: `nz_coder/recovery.py`
- Modify: `tests/test_recovery.py`

1. Add G5 fixture tests for stale constant and `Path(__file__)`-derived cwd.
2. Confirm current recovery does not classify them.
3. Implement limited AST resolution and connect it before generic test-failure
   advice.
4. Verify the diagnostic contains helper, old cwd, active cwd, and package, and
   contains no installation recommendation.

### Task 4: Enforce bounded emergency

**Files:**
- Modify: `tests/runtime/test_work_budget.py`
- Modify: `tests/test_runtime_state.py`
- Modify: `tests/runtime/tool_runtime/test_focused_policy.py`
- Modify: `nz_coder/runtime/work_budget.py`
- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: `nz_coder/runtime/loop.py`
- Modify: `nz_coder/runtime/tool_runtime/policy.py`

1. Add G3 phase, eligibility, known-path, broad-search, child-task, and package
   installation tests.
2. Confirm emergency currently reopens prohibited tools.
3. Rename the phase to `bounded_emergency`, add deterministic eligibility, apply
   strict visibility and execution guards, and expose counters.
4. Run all budget and policy tests.

### Task 5: Unify terminal-boundary settlement

**Files:**
- Modify: `tests/runtime/test_native_runner.py`
- Modify: `nz_coder/runtime/runner.py`
- Optionally create: `nz_coder/runtime/terminal_boundary.py`

1. Add G1 and G2 end-to-end runner tests for final streamed/buffered tool edits,
   exact acceptance attempt counts, ledger evidence, persistence, and failure.
2. Confirm the final-tool-call case bypasses exact acceptance.
3. Implement one settlement method and invoke it from natural completion,
   streamed tools, buffered tools, and loop exhaustion.
4. Produce a deterministic factual summary only when complete evidence exists
   and no model summary is available.
5. Run native-runner and completion-gate tests.

### Task 6: Integrate trace observability and documentation

**Files:**
- Modify: `nz_coder/runtime/loop.py`
- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: `docs/terminal-product-real-world-issues.md`
- Modify: `docs/infcode-alignment-learning-log.md`

1. Emit terminal decision, ledger, emergency, artifact/candidate, and policy
   counters without adding provider calls.
2. Preserve provider usage aggregation by call purpose.
3. Record the root causes, fixes, and gate evidence in both project documents.

### Task 7: Verification and fourth-run decision

1. Run G1-G7 focused tests.
2. Run the full pytest suite.
3. Run Ruff on changed Python files.
4. Inspect the complete diff for accidental API or user-work changes.
5. Only if all local evidence passes, launch the fourth isolated real cron task
   and evaluate every paid-test criterion from the design. Otherwise stop before
   the provider call and report the concrete failing gate.

No Git commit, branch, or worktree operation is part of this plan because this
workspace contains substantial user-owned in-progress changes and the project
workflow does not require Git for implementation.
