# ToolExecutionContext Phase 2C Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the production asynchronous Tool Runtime consume a run-scoped focused context instead of AgentLoop.

**Architecture:** Create policy/lifecycle/projection context groups and a single ToolExecutionContext, with one legacy-host adapter at the Runner boundary. Migrate policy, result projection, dispatch, transactions, checkpoints, and post-processing while retaining the synchronous host compatibility path.

**Tech Stack:** Python 3.9+, asyncio, dataclasses, Protocol/callables, pytest, Ruff; no new dependency and no Agent framework.

## Global Constraints

- Preserve CLI, HTTP, SDK, evaluation, Main/Sub/Background, and direct sync compatibility.
- Production async Tool Runtime cannot receive or discover AgentLoop.
- SessionRuntime remains the only production async checkpoint owner.
- Follow red-green-refactor for every behavior change.
- Do not call paid Providers, run SWE-bench, or mutate Git state.

---

### Task 1: Define ToolExecutionContext and adapter

**Files:**
- Create: `nz_coder/runtime/core/tool_context.py`
- Create: `nz_coder/runtime/adapters/tool.py`
- Test: `tests/runtime/core/test_tool_execution_context.py`

**Interfaces:**
- Produces `ToolPolicyContext`, `ToolLifecycleContext`,
  `ToolProjectionContext`, `ToolExecutionContext`.
- Produces `tool_context_from_legacy_host(host, run_context, services)`.

- [x] Write construction and capability-isolation tests.
- [x] Run tests and observe missing modules/types.
- [x] Implement validated context groups and adapter.
- [x] Run focused tests, Ruff, compile, and diff check.

### Task 2: Migrate ToolPolicy and result projection

**Files:**
- Modify: `nz_coder/runtime/tool_runtime/policy.py`
- Modify: `nz_coder/runtime/tool_runtime/result_projection.py`
- Test: `tests/runtime/tool_runtime/test_focused_policy.py`
- Test: `tests/runtime/tool_runtime/test_focused_projection.py`

**Interfaces:**
- Every policy method consumes `ToolPolicyContext`.
- `ProductionToolResultProjector.consume()` consumes
  `ToolProjectionContext`.

- [x] Add rejection, scheduling-state, projection, attachment, and hook tests using focused contexts.
- [x] Run tests and observe the old host-shaped calls fail.
- [x] Replace direct host access with explicit context state/operations.
- [x] Run policy, projection, handoff, permission, and observability tests.

### Task 3: Migrate the production async pipeline

**Files:**
- Modify: `nz_coder/runtime/tool_runtime/pipeline.py`
- Modify: `nz_coder/runtime/core/contracts.py`
- Modify: `nz_coder/runtime/runner.py`
- Modify: `nz_coder/runtime/loop.py`
- Test: `tests/runtime/tool_runtime/test_focused_pipeline.py`
- Modify: `tests/runtime/tool_runtime/test_session_checkpoint.py`

**Interfaces:**
- `execute_batch_async(context, calls, messages, ...) -> str`.
- Sync `execute_batch_sync(host, calls, messages, ...)` remains compatible.

- [x] Add async read, write, cancellation, checkpoint, and transition tests that provide no AgentLoop object.
- [x] Run tests and observe the old signature/host dependency fail.
- [x] Migrate async dispatch and post-processing to ToolExecutionContext.
- [x] Construct one context in Runner and both legacy async facade paths.
- [x] Run Tool Runtime, cancellation, Runner, Session, handoff, and input tests.

### Task 4: Architecture gates and A236

**Files:**
- Modify: `tests/runtime/test_context_architecture.py`
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: this plan

- [x] Gate the async pipeline/policy/projection against direct host private access.
- [x] Audit remaining sync compatibility consumers and host references.
- [x] Run Ruff, compile/import, full pytest, architecture suite, parallel smoke, and diff check.
- [x] Record exact evidence and remaining debt in A236.
- [x] Mark the plan complete only after fresh final verification.
