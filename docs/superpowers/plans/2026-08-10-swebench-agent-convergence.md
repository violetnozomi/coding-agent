# SWE-bench Agent Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate false SWE-bench timeouts and make strict Agent investigation, shell use, and successful verification converge deterministically.

**Architecture:** Keep full traces on disk and pass only bounded typed results across the process boundary. Add strict-only execution controls through the existing runtime execution context and RuntimeState, leaving the interactive product behavior unchanged.

**Tech Stack:** Python 3.9+, standard library multiprocessing/asyncio/pathlib, pytest.

## Global Constraints

- Do not introduce an Agent framework or a new runtime dependency.
- Preserve existing tool names and required parameters; new parameters are optional.
- Tool handler failures return strings prefixed with `Error: `.
- Every resolved shell working directory must remain inside `current_workdir()`.
- Do not launch a paid SWE-bench run while implementing this plan.
- Work inline in the current workspace; do not require Git commits or a new worktree.

---

### Task 1: Deadlock-free subprocess protocol

**Files:**
- Modify: `tests/test_swebench_lite.py`
- Modify: `nz_coder/swebench/orchestrator.py`

**Interfaces:**
- Consumes: `_run_agent_attempt(..., timeout: int) -> dict`
- Produces: `_receive_agent_payload(process, result_queue, timeout) -> dict`

- [ ] Add `_LargeToolOutputAgent` whose callback payload exceeds 64KB and assert `_run_agent_attempt` completes within a short timeout while replaying every event.
- [ ] Run that single test and confirm the current join-before-drain implementation raises `AgentRunTimeout`.
- [ ] Replace `join -> empty -> get` with deadline-bound Queue receive, then join the already-drained child; bound child event output and close Queue resources in `finally`.
- [ ] Add tests for child exception and abrupt no-payload exit, keeping the existing real-timeout test.
- [ ] Run all orchestrator helper tests.

### Task 2: Explicit strict Bash protocol

**Files:**
- Modify: `tests/test_swebench_lite.py`
- Modify: `tests/test_bash.py` or the existing Bash test module
- Modify: `nz_coder/tools/bash.py`
- Modify: `nz_coder/swebench/policy.py`
- Modify: `nz_coder/swebench/orchestrator.py`

**Interfaces:**
- Produces: `run_bash(command: str, read_only: bool = False, timeout: int | None = None, workdir: str | None = None) -> str`
- Produces: `strict_bash_guidance(command: str, violation: str) -> str`

- [ ] Add failing tests for an in-workspace `workdir`, `../` escape rejection, and actionable strict guidance for `cd`, Git history, and arbitrary Python.
- [ ] Add optional `workdir` to the Bash schema and resolve it under the active workspace before `Popen`.
- [ ] Add deterministic rewrite guidance while preserving the existing fail-closed grammar.
- [ ] Extend the strict system prompt with the exact grammar and structured navigation triggers.
- [ ] Separate strict policy rejections from semantic patch risk in result accounting, retaining them as process diagnostics.
- [ ] Run Bash, policy, and orchestrator prompt/risk tests.

### Task 3: Strict no-progress convergence

**Files:**
- Modify: `tests/test_runtime_state.py`
- Modify: `tests/test_loop_fake.py`
- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: `nz_coder/runtime/loop.py`

**Interfaces:**
- Produces: `RuntimeState.investigation_calls_since_edit: int`
- Produces: `RuntimeState.strict_progress_action(tool_name: str) -> str`

- [ ] Add failing state tests for soft nudge, hard gate, and successful write reset.
- [ ] Count successful read/search/navigation calls by mutation generation and render one model-visible strict nudge at the soft threshold.
- [ ] Add a strict-only before-dispatch hard gate for further investigation calls after the hard threshold; edits, diff, verification and text finalization remain allowed.
- [ ] Trace the nudge/gate with counters so later offline analysis can distinguish model search from runtime intervention.
- [ ] Run RuntimeState and fake-loop tests.

### Task 4: Strict verification as a terminal result

**Files:**
- Modify: `tests/test_loop_fake.py`
- Modify: `nz_coder/runtime/loop.py`

**Interfaces:**
- Consumes: existing `_consume_dispatched_tools(...) -> dict`
- Produces: batch state `terminal=True` after accepted strict verification over a non-empty diff.

- [ ] Add a failing fake-provider test where a successful `verify_changed_files` would otherwise be followed by another model/tool turn.
- [ ] Mark the settled batch terminal only when strict mode is active, verification succeeded, and RuntimeState confirms a non-empty diff.
- [ ] Add negative tests for failed verification, empty diff, and ordinary interactive mode.
- [ ] Run loop, verification, and Session lifecycle tests.

### Task 5: Documentation and regression closure

**Files:**
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: `docs/swebench-progress.md`

**Interfaces:**
- Consumes: test outputs and preserved r3 artifact counts.
- Produces: A224 implementation record with exact verification evidence and remaining gaps.

- [ ] Run focused tests for all four behavior changes.
- [ ] Run broader runtime/SWE regression suites and `py_compile` on modified production modules.
- [ ] Record completed behavior, design differences from InfCode/InfCodeX, test counts, and the fact that no paid evaluation was run.
- [ ] Recheck that r3 remains stopped and its archived artifacts remain present.
