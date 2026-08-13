# Native Runtime De-hosting and Child Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Main and child Agents through a native request-based runtime without constructing `AgentLoop`, while making child Session the only durable conversation source.

**Architecture:** Add a native `RunRequest + RunOptions -> SessionRuntime -> RunContext -> AgentRunner` path, then move coherent callback groups into owned services. Keep `AgentLoop` as a one-way compatibility facade and keep task/worktree orchestration outside Session.

**Tech Stack:** Python 3.9+, asyncio, dataclasses, Protocols, pytest; no new runtime dependency or Agent framework.

## Global Constraints

- Preserve existing public tool names, Provider behavior, CLI/HTTP/SDK result shapes, worktree isolation, verification, Memory, MCP, LSP, and Workflow behavior.
- Use strangler migration; no big-bang `AgentLoop` rewrite.
- Write and observe a failing behavioral test before every production behavior change.
- Native Runtime must not call legacy adapters or `AgentLoop._private` methods.
- Background Agent and Workflow receive compatibility-only changes in this phase.
- Do not commit, reset, clean, or otherwise modify Git state beyond working-tree files.

---

### Task 1: Native Runner Contract

**Files:**
- Modify: `nz_coder/runtime/core/request.py`
- Modify: `nz_coder/runtime/runner.py`
- Modify: `nz_coder/runtime/core/contracts.py`
- Test: `tests/runtime/test_native_runner.py`

**Interfaces:**
- Produces: `RunOptions`; `AgentRunner.run(request: RunRequest, *, options: RunOptions | None = None)`.
- Preserves: legacy execution through an explicit compatibility adapter.

- [x] Add a failing AgentLoop-free Model -> Tool -> Model -> Final integration test.
- [x] Run the test and confirm failure is caused by the host-shaped Runner API.
- [x] Implement immutable `RunOptions` and native Runner entry using constructor-injected `RuntimeServices`.
- [x] Move host-shaped entry conversion behind the legacy facade without changing public Main behavior.
- [x] Run native, Runner, model, tool, Session, cancellation, and compatibility tests.

### Task 2: Main Legacy Facade to Native Entry

**Files:**
- Modify: `nz_coder/runtime/loop.py`
- Modify: `nz_coder/runtime/composition.py`
- Modify: `nz_coder/runtime/adapters/runner.py`
- Test: `tests/runtime/test_native_main_facade.py`

**Interfaces:**
- Consumes: native `AgentRunner.run(request, options=...)`.
- Produces: one-way `Legacy AgentLoop -> RunRequest/RunOptions/native services` conversion.

- [x] Add a failing trace/identity test proving Main enters the native API.
- [x] Verify the test fails while Main still invokes the host-shaped Runner entry.
- [x] Convert `AgentLoop.run()`/composition to build native request and options once.
- [x] Restrict legacy adapter use to the entry boundary.
- [x] Run CLI, HTTP, SDK, evaluation, Main trace, and legacy compatibility tests.

### Task 3: Replace Runner Callback Bag with Owned Services

**Files:**
- Modify: `nz_coder/runtime/core/runner_context.py`
- Modify: `nz_coder/runtime/core/contracts.py`
- Modify: `nz_coder/runtime/services.py`
- Modify: `nz_coder/runtime/runner.py`
- Create: `nz_coder/runtime/message_runtime.py`
- Create: `nz_coder/runtime/planning_runtime.py`
- Create: `nz_coder/runtime/snapshot_runtime.py`
- Test: `tests/runtime/test_native_runtime_services.py`

**Interfaces:**
- Consumes: `RunContext`, `RuntimeServices`, `RunOptions`.
- Produces: focused stateful services; no general callback bag on the native path.

- [x] Characterize all 45 callbacks and add failing ownership tests for the first coherent groups.
- [x] Move message projection/mutation to SessionProcessor/message service and usage to RunContext.
- [x] Move planning/replan and snapshot/patch observation into cohesive service ports.
- [x] Move hooks/events into explicit sinks and keep lifecycle/guardrail/transition/verifier on existing ports.
- [x] Remove the flat callback bag from `RunnerExecutionContext`; native execution receives cohesive owners.
- [x] Run focused ownership, stream/message-part, planning, snapshot, handoff, and full Runner tests.

### Task 4: Tool, Context, and Model De-hosting

**Files:**
- Modify: `nz_coder/runtime/services.py`
- Modify: `nz_coder/runtime/tool_runtime/pipeline.py`
- Modify: `nz_coder/runtime/tool_runtime/policy.py`
- Modify: `nz_coder/runtime/tool_runtime/result_projection.py`
- Create: `nz_coder/runtime/tool_observers.py`
- Modify: `nz_coder/runtime/context_manager.py`
- Modify: `nz_coder/runtime/model_gateway/gateway.py`
- Modify: `nz_coder/runtime/model_gateway/runtime.py`
- Modify: `nz_coder/runtime/adapters/{tool,context,model}.py`
- Test: `tests/runtime/test_native_service_boundaries.py`

**Interfaces:**
- Produces: generic Tool/Context/Model services that consume `RunContext` and focused contexts, plus coding observers for index/LSP/patch effects.

- [x] Add failing architecture/behavior tests for native Tool/Context/Model execution without host-private callbacks.
- [x] Move checkpointing to SessionRuntime and transaction settlement to the Tool execution owner.
- [x] Inject code-index, LSP, patch-risk, snapshot, and verification effects through coding observers.
- [x] Make Context budget/compaction consume RunContext/request/model capability state.
- [x] Make Model selection/call/usage/retry consume request and focused Provider runtime state.
- [x] Run tool policy, transactions, context/compaction, Provider, stream, retry, and cancellation suites.

### Task 5: Native Child Session

**Files:**
- Modify: `nz_coder/runtime/subagent.py`
- Modify: `nz_coder/runtime/child_contracts.py`
- Modify: `nz_coder/runtime/child_result.py`
- Modify: `nz_coder/runtime/session/{model,runtime,store}.py`
- Test: `tests/runtime/test_native_child_session.py`

**Interfaces:**
- Consumes: native Runner API and parent `SessionIdentity`.
- Produces: child `RunRequest` with `parent_session_id`; typed orchestration-only task state.

- [x] Add failing parent -> child -> same Runner integration test.
- [x] Add failing invariant test forbidding native task-state transcript persistence.
- [x] Resolve child AgentDefinition/Profile/permissions/model/workspace into a native RunRequest.
- [x] Create/load child Session through SessionRuntime and call the same native Runner.
- [x] Separate TaskStatus from SessionStatus and remove overlapping native fields.
- [x] Run child result, worktree, conflict, verification, manager, and Session tests.

### Task 6: Session-only Child Resume and Legacy Migration

**Files:**
- Modify: `nz_coder/runtime/subagent.py`
- Modify: `nz_coder/runtime/session/runtime.py`
- Modify: `nz_coder/runtime/session/store.py`
- Test: `tests/runtime/test_native_child_resume.py`

**Interfaces:**
- Produces: `task(prompt A)` followed by `task(session_id, prompt B)` on one durable child transcript.

- [x] Add a failing process-restart child-resume test with a new SessionRuntime/Store instance.
- [x] Load the child Session by identity and append only the new user activation.
- [x] Keep old `state.messages` as a one-time bootstrap only when no native Session exists.
- [x] Remove task transcript/session-status/usage synchronization after native activation; retain distinct TaskStatus and result projection.
- [x] Run resume, persistence, Session ownership, child, and cancellation tests.

### Task 7: Architecture Closure and Three-way Report

**Files:**
- Modify: `tests/runtime/test_context_architecture.py`
- Modify: `docs/unified-agent-runtime-migration.md`
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: this plan

**Interfaces:**
- Produces: native-path architecture guards, actual before/after metrics, SCC report, state ownership table, three-way capability matrix, Q1-Q6 answers, and top-ten debt list.

- [x] Add architecture guards preventing native Runtime -> legacy adapter/AgentLoop private dependencies.
- [x] Measure Loop LOC/attrs/methods, callback counts, host-private references, child fields, and import SCCs.
- [x] Run Ruff, compile/import smoke, focused runtime/product suites, and full pytest.
- [x] Run AgentLoop-free native smoke, Main/child parity, persistence restart, CLI/SDK smoke, and `git diff --check`.
- [x] Update migration and learning documents with exact evidence and remaining debt.
- [x] Mark all checkboxes complete only after a fresh final verification.
