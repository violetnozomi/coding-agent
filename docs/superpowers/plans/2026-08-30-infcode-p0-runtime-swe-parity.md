# InfCodeX P0 Runtime and SWE Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the remaining post-nominal runtime hard gates, resume parent runs after background-task wakes, and make SWE-bench prediction/submission publication fail closed.

**Architecture:** Keep the existing `AgentRunner`, `BackgroundAgentManager`, and `AttemptJournal` ownership boundaries. Work-budget zones become advisory state only until the configured hard cap; idle-yield is exposed through the existing Runner control adapter; SWE provenance is validated from append-only attempts, the inference manifest, and exact official-harness artifacts.

**Tech Stack:** Python 3.9+, asyncio, threading, pathlib, pytest; no new dependencies.

**Spec:** User-approved P0 ordering from the 2026-08-29 InfCodeX parity audit and the in-chat design approved by “继续对齐”.

## Global Constraints

- Preserve all existing user changes in the dirty worktree; do not reset or commit unrelated files.
- Do not add an Agent framework or external dependency.
- Keep public tool names and schemas stable.
- Add behavior tests before each production change and observe the expected failure.
- Treat the InfCodeX `0.7.71 / d3a81237` source snapshot under `references/InfCodeX` as the parity reference.

---

### Task 1: Advisory work budget through the hard cap

**Files:**
- Modify: `tests/runtime/test_work_budget.py`
- Modify: `tests/runtime/test_native_runner.py`
- Modify: `tests/runtime/tool_runtime/test_focused_policy.py`
- Modify: `tests/test_runtime_state.py`
- Modify: `nz_coder/runtime/work_budget.py`
- Modify: `nz_coder/runtime/runner.py`
- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: `nz_coder/runtime/tool_runtime/policy.py`
- Modify: `nz_coder/runtime/loop.py`
- Modify: `nz_coder/config.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `WorkBudgetController.phase(int) -> str`, `RuntimeState.closure_phase_decision(...)`.
- Produces: advisory zones at 70/85/95 percent, `soft_extension` after the nominal budget, and hard termination only at `max_turns`.

- [ ] Write tests proving post-nominal exploration remains callable and an ineligible emergency snapshot cannot stop Runner before the hard cap.
- [ ] Run the focused tests and confirm failures caused by `bounded_emergency` behavior.
- [ ] Retire bounded-emergency termination, schema pruning, and policy rejection; update defaults to nominal 200, hard cap 500, child 200.
- [ ] Run the focused runtime tests and confirm they pass.

### Task 2: Background idle-yield wake and resume

**Files:**
- Modify: `tests/test_agent_manager.py`
- Modify: `tests/runtime/test_native_runner.py`
- Modify: `nz_coder/runtime/agent_manager.py`
- Modify: `nz_coder/runtime/loop.py`
- Modify: `nz_coder/runtime/adapters/runner.py`
- Modify: `nz_coder/runtime/runner.py`

**Interfaces:**
- Consumes: `BackgroundAgentManager.drain_messages("worker")`, Runner natural no-tool completion.
- Produces: `BackgroundAgentManager.has_worker_wake_source() -> bool` and `_TurnControlService.idle_yield(messages) -> Awaitable[bool]`.

- [ ] Write tests proving fast child completion leaves a durable worker wake and natural completion resumes after that wake.
- [ ] Run the focused tests and confirm they fail because no wake notification/resume exists.
- [ ] Enqueue bounded `<task-completed>` notifications and add cancellation-safe async polling at the natural completion boundary.
- [ ] Run focused agent-manager and Runner tests and confirm they pass.

### Task 3: Crash-resumable pass@1 journal and fail-closed patch publication

**Files:**
- Modify: `tests/test_swebench_strict.py`
- Modify: `tests/test_swebench_lite.py`
- Modify: `nz_coder/swebench/artifacts.py`
- Modify: `nz_coder/swebench/orchestrator.py`

**Interfaces:**
- Consumes: `AttemptJournal.completed_ids()`, `_benchmark_result_status(...)`.
- Produces: idempotent recovery of a claim-only row and `agent_failed` for `max_turns` or a blocking patch-quality report.

- [ ] Write tests proving claim-only rows rerun while committed results skip, and unsafe/max-turn patches serialize as empty predictions.
- [ ] Run focused SWE tests and confirm the old skip/publication behavior fails.
- [ ] Use committed result IDs for resume, make claim-only claims idempotent, and include blocking-quality state in benchmark status.
- [ ] Run focused SWE tests and confirm they pass.

### Task 4: Official-evaluation provenance binding

**Files:**
- Modify: `tests/test_swebench_strict.py`
- Modify: `nz_coder/swebench/submission.py`
- Modify: `nz_coder/swebench/cli.py`

**Interfaces:**
- Consumes: inference manifest `run_id`, prediction rows, official harness directory layout.
- Produces: manifest `official_evaluation` provenance with evaluation run ID and prediction SHA-256; exact `<run>/<model>/<instance>` resolution and official `patch.diff` equality.

- [ ] Write tests with two runs/models for the same instance and prove the wrong run/model or mismatched patch is rejected.
- [ ] Run focused submission tests and confirm recursive matching incorrectly accepts the fixture.
- [ ] Persist evaluation provenance after a successful harness run, resolve only the declared run/model directory, validate the official patch, and copy it unchanged.
- [ ] Run focused submission tests and confirm they pass.

### Task 5: Regression verification

**Files:**
- Verify only; no new production surface.

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: fresh pytest evidence and a scoped diff review.

- [ ] Run all modified test modules together.
- [ ] Run the broader runtime and SWE-bench regression suites that fit available disk capacity.
- [ ] Inspect `git diff --check` and the scoped diff; report any unrelated pre-existing failures separately.
