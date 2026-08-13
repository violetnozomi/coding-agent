# Focused Context Runtime Phase 2B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove AgentLoop from the production Context Runtime contract and move production Tool Runtime checkpoints to SessionRuntime.

**Architecture:** Add an immutable focused context plus a narrow legacy adapter, then change ContextManager and Runner to consume it. Add an explicit async checkpoint callback to ToolRuntime so production tool state is persisted by the active RunContext while direct legacy callers retain a bounded fallback.

**Tech Stack:** Python 3.9+, asyncio, dataclasses, pathlib, typing Protocol/callables, pytest, Ruff; no new dependencies and no Agent framework.

**Execution status (2026-08-10):** Complete. The final tree passed 1582 full
tests and 63 focused architecture/runtime tests. Context Runtime is host-free;
only asynchronous production Tool checkpoints moved in this phase, while the
synchronous compatibility path and other Tool host capabilities remain.

## Global Constraints

- Preserve CLI, HTTP, SDK, evaluation, `AgentLoop.run()`, and direct tool compatibility.
- Session owns complete history; Context Runtime only derives the current model input.
- New behavior follows red-green-refactor and does not hide failures.
- Do not call paid Providers or run SWE-bench.
- Do not commit, branch, merge, or otherwise mutate Git state.

---

### Task 1: Add the focused ContextExecutionContext

**Files:**
- Create: `nz_coder/runtime/core/context.py`
- Create: `nz_coder/runtime/adapters/context.py`
- Test: `tests/runtime/core/test_context_execution_context.py`

**Interfaces:**
- Produces `ContextExecutionContext` and `context_from_legacy_host(host)`.
- Context exposes `workspace`, `budget`, `projected_tokens(messages)`,
  `compact(messages)`, `stamp_auto_compaction(messages)`, and `trace(...)`.

- [x] Write tests proving construction snapshots workspace/budget and delegates each operation.
- [x] Run the tests and observe failure because the modules do not exist.
- [x] Implement the immutable focused context and narrow adapter.
- [x] Run focused tests, Ruff, compile, and diff check.

### Task 2: Make ProductionContextManager host-free

**Files:**
- Modify: `nz_coder/runtime/context_manager.py`
- Modify: `nz_coder/runtime/core/contracts.py`
- Modify: `nz_coder/runtime/runner.py`
- Modify: `nz_coder/runtime/loop.py`
- Test: `tests/runtime/test_context_runtime.py`
- Test: `tests/runtime/test_runner.py`

**Interfaces:**
- `prepare_sync(context, messages, ...) -> bool`
- `prepare_async(context, messages, ...) -> bool`

- [x] Add behavior tests for no-compaction and hard-limit compaction using a real focused context.
- [x] Run tests and observe the old host-shaped contract fail.
- [x] Replace all ContextManager host access with focused-context operations.
- [x] Construct the focused context at Runner and legacy sync/async adapter boundaries.
- [x] Run context, Runner, compaction, and architecture tests.

### Task 3: Move async Tool checkpoints to SessionRuntime

**Files:**
- Modify: `nz_coder/runtime/core/contracts.py`
- Modify: `nz_coder/runtime/tool_runtime/pipeline.py`
- Modify: `nz_coder/runtime/runner.py`
- Test: `tests/runtime/tool_runtime/test_session_checkpoint.py`
- Modify: `tests/runtime/test_runner.py`

**Interfaces:**
- `execute_batch_async(..., checkpoint: Callable[[str], Awaitable[None]] | None)`.
- Runner binds the callback to `SessionRuntime.checkpoint(run_context, status)`.

- [x] Add tests proving start/finish/interruption checkpoints call the injected async boundary and never call legacy host persistence.
- [x] Run tests and observe failure because the callback is unsupported.
- [x] Implement callback precedence and reject legacy fallback when an active RunContext exists.
- [x] Bind both normal and stream-executed tool paths in Runner.
- [x] Run Tool Runtime, cancellation, Runner, and Session tests.

### Task 4: Architecture gates and documentation

**Files:**
- Modify: `tests/runtime/test_context_architecture.py`
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: this plan

**Interfaces:**
- Architecture tests reject `host.` access in `ProductionContextManager`.
- Architecture tests require Runner ToolRuntime calls to provide the checkpoint boundary.

- [x] Add and run architecture gates.
- [x] Run Ruff, compile/import smoke, full pytest, architecture suite, offline parallel smoke, and `git diff --check`.
- [x] Record exact evidence and remaining host-private debt in A235.
- [x] Mark all plan steps complete only after fresh verification.
