# Long-Task Convergence Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Determine whether recent Runtime fixes close TP-025, and implement one trace-proven convergence optimization only if the fresh baseline still exceeds 15 model calls or 25 tool calls.

**Architecture:** A fresh isolated copy of the original `cron_engine` fixture receives the exact prior task and explicit acceptance command. Trace facts choose at most one bounded optimization; existing RuntimeState, context projection, Repo Intelligence, and tool pipeline remain the integration boundaries.

**Tech Stack:** Python 3.9+, standard library, existing NZ-Coder Runtime, pytest, JSONL trace.

## Global Constraints

- No new dependency or Agent framework.
- Preserve the durable transcript and existing public tool interfaces.
- Never prune failures, tracebacks, current-generation diffs, or acceptance results.
- Do not lower the hard turn cap to manufacture a passing metric.
- Do not require Git for product behavior or create a commit for this work.

---

### Task 1: Fresh comparable real-product baseline

**Files:**
- Source fixture: `/home/pyh/test_nzcoder/cron_engine`
- Create at runtime: `/home/pyh/test_nzcoder/.product-convergence-*`
- Update: `docs/terminal-product-real-world-issues.md`

**Interfaces:**
- Produces: one isolated Session trace with exact model/tool/token/time and acceptance facts.

- [x] Copy the unmodified `cron_engine` fixture into a fresh isolated product workspace.
- [x] Confirm `python -m pytest -q cron_engine/tests` passes before the task.
- [x] Run `nz-coder run` with the exact previous month/weekday-name task, default 20-turn budget, auto permissions, and DeepSeek V4 Flash.
- [x] Independently execute the complete acceptance command.
- [x] Extract model calls, tool calls, failed calls, cumulative tokens, elapsed time, automatic contract event, and terminal state from JSONL.
- [x] Stop without code changes if calls are at most 15, tools are at most 25, and acceptance passes. (Condition was false; continued to Task 2.)

### Task 2: Trace-driven failing contract, only when baseline misses

**Files (choose only the branch supported by trace):**
- Context branch: `nz_coder/state/context.py`, `nz_coder/runtime/context_manager.py`
- Retrieval branch: existing Repo Intelligence and prompt-builder modules
- Convergence branch: `nz_coder/runtime/work_budget.py`, `nz_coder/runtime/runner.py`
- Test: corresponding focused test module under `tests/`

**Interfaces:**
- Produces: one failing regression that reproduces the dominant observed waste without a live Provider.

- [x] Classify repeated reads, initial discovery, or serial recovery from ordered trace evidence.
- [x] Write failing tests for the selected pre-edit shell drift and later trace-proven recovery boundaries.
- [x] Run each exact test and confirm the expected failure.
- [x] Do not implement unrelated context-pruning or Repo Intelligence branches.

### Task 3: Minimal convergence implementation, only when required

**Files:**
- Modify only the selected Task 2 branch and its tests.

**Interfaces:**
- Consumes: the failing contract from Task 2.
- Produces: bounded, provider-neutral convergence behavior.

- [x] Implement the smallest changes that satisfy the failing contracts.
- [x] Run the exact tests to green.
- [x] Run focused RuntimeState/Tool Runtime/Runner/recovery/work-budget tests.
- [x] Run Ruff on every changed Python file.

### Task 4: Product re-test and honest closure

**Files:**
- Update: `docs/terminal-product-real-world-issues.md`
- Update: `docs/infcode-alignment-learning-log.md`

**Interfaces:**
- Produces: TP-025 closed evidence or a narrower remaining diagnosis.

- [x] Recreate fresh isolated fixtures rather than resuming the baseline Session.
- [x] Re-run the exact task and independent acceptance suite.
- [x] Compare baseline and final call/tool/token/time counts.
- [x] Run the complete repository pytest suite and `git diff --check` (2151 passed, 21 skipped).
- [x] Keep TP-025 at `verify`: correctness passed, but the best complete trace (18/28) missed 15/25.
