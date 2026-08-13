# Session-first Runtime Final Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the approved Session-first Runtime migration so production Main, child, and background runs use one Session-owned Runner with host-free Model, Tool, Context, Runner-turn, and Lifecycle boundaries.

**Architecture:** Add focused model, runner, and lifecycle contexts with one AgentLoop adapter at composition. Keep scheduling artifacts separate from native parent-linked child Sessions, remove the obsolete SessionRepository from the production service graph, and retain only explicitly non-production compatibility facades.

**Tech Stack:** Python 3.9+, asyncio, dataclasses, Protocols/callables, pytest, Ruff; no new dependencies and no Agent framework.

## Global Constraints

- Preserve CLI, HTTP, SDK, evaluation, SWE-bench, Main/Sub/Background, and stable direct-call interfaces.
- SessionRuntime is the only production transcript/checkpoint/finalization owner.
- Production core services cannot receive or discover AgentLoop.
- Follow red-green-refactor for every behavior change.
- Do not call paid Providers, run SWE-bench, mutate Git state, or delete user data.

---

### Task 1: Focused ModelExecutionContext

**Files:**
- Create: `nz_coder/runtime/core/model_context.py`
- Create: `nz_coder/runtime/adapters/model.py`
- Modify: `nz_coder/runtime/services.py`
- Modify: `nz_coder/runtime/core/contracts.py`
- Modify: `nz_coder/runtime/runner.py`
- Test: `tests/runtime/core/test_model_execution_context.py`
- Test: `tests/runtime/model_gateway/test_focused_turn_runtime.py`

**Interfaces:**
- Produces `ModelExecutionContext` with explicit model identity, capabilities,
  request/projection, stream retirement, recovery, and trace capabilities.
- `ProductionTurnModelRuntime.complete_turn(context, messages, ...)` consumes it.

- [x] Write construction, buffered, streaming fallback, cancellation, and retirement tests without AgentLoop.
- [x] Run focused tests and confirm host-shaped ProductionTurnModelRuntime fails.
- [x] Implement ModelExecutionContext and the single legacy-host adapter.
- [x] Migrate all ProductionTurnModelRuntime methods and Runner call sites.
- [x] Gate ProductionTurnModelRuntime against direct host access and run Provider/stream/cancellation suites.

### Task 2: Focused RunnerExecutionContext

**Files:**
- Create: `nz_coder/runtime/core/runner_context.py`
- Create: `nz_coder/runtime/adapters/runner.py`
- Modify: `nz_coder/runtime/runner.py`
- Modify: `nz_coder/runtime/input_preflight.py`
- Modify: `nz_coder/runtime/agent_transition_runtime.py`
- Test: `tests/runtime/core/test_runner_execution_context.py`
- Modify: `tests/runtime/test_context_architecture.py`

**Interfaces:**
- Produces `RunnerExecutionContext` grouping turn control, message projection,
  snapshots, hooks/events, compaction, observation, and transition operations.
- `AgentRunner._run_turns(context, services, run_context, ...)` receives no host.

- [x] Write focused context construction and missing-capability failure tests.
- [x] Add a source gate requiring zero direct host access in `_run_turns`.
- [x] Run tests and confirm the current Runner fails the new contract.
- [x] Migrate turn preparation, message materialization, snapshots, usage, diagnostics, and hooks to explicit capabilities.
- [x] Migrate input/transition Runner call sites or add focused ports where the service is still host-shaped.
- [x] Run Runner/Session/Context/Input/Handoff/Tool/stop-hook/cancellation suites.

### Task 3: Focused LifecycleContext

**Files:**
- Create: `nz_coder/runtime/core/lifecycle_context.py`
- Extend: `nz_coder/runtime/adapters/runner.py`
- Modify: `nz_coder/runtime/run_lifecycle.py`
- Modify: `nz_coder/runtime/core/contracts.py`
- Modify: `nz_coder/runtime/runner.py`
- Test: `tests/runtime/core/test_lifecycle_context.py`
- Test: `tests/runtime/test_lifecycle_architecture.py`

**Interfaces:**
- Produces `LifecycleContext` with explicit run reset, role restore, publication,
  memory/final evidence, and public-result operations.
- Production lifecycle async path accepts the focused context only.

- [x] Write initialize/finalize/resume/exactly-once tests without AgentLoop.
- [x] Add AST gates and confirm current lifecycle implementation fails them.
- [x] Migrate ProductionRunLifecycle state mutation and callbacks to LifecycleContext.
- [x] Keep sync legacy facade through the adapter without duplicating lifecycle policy.
- [x] Run lifecycle, memory, admission, structured-output, trace, and terminal suites.

### Task 4: Native foreground/background child Sessions

**Files:**
- Modify: `nz_coder/runtime/subagent.py`
- Modify: `nz_coder/runtime/agent_manager.py`
- Modify: `nz_coder/runtime/session/runtime.py`
- Modify: `nz_coder/runtime/core/request.py`
- Test: `tests/runtime/test_child_session_runtime.py`
- Modify: `tests/test_subagent.py`
- Modify: `tests/test_agent_manager.py`

**Interfaces:**
- Every child request carries `session_id`, `parent_session_id`, agent/profile,
  workspace, and resume metadata into the same SessionRuntime.
- Task-control `state.json` contains scheduling/result facts, not transcript state.

- [x] Write foreground/background parent-link and native resume tests.
- [x] Write a regression test proving task-control state cannot replace Session transcript.
- [x] Run tests and identify the exact missing production propagation.
- [x] Add explicit child RunRequest/session metadata propagation and fail-closed validation.
- [x] Remove transcript ownership from task-control records while preserving backward-compatible reads.
- [x] Run SubAgent/Manager/Workflow/Session/cancellation/worktree suites.

### Task 5: Retire production SessionRepository compatibility

**Files:**
- Modify: `nz_coder/runtime/core/contracts.py`
- Modify: `nz_coder/runtime/services.py`
- Modify: `nz_coder/runtime/core/__init__.py`
- Modify: `nz_coder/runtime/session_repository.py`
- Modify: `docs/architecture.md`
- Test: `tests/runtime/core/test_contracts.py`
- Test: `tests/runtime/test_context_architecture.py`

**Interfaces:**
- `RuntimeServices` exposes only `session_runtime` for production persistence.
- FileSessionRepository/RunState, if retained, are marked legacy and absent from
  production imports and service construction.

- [x] Add a source/contract test requiring zero production `services.sessions` consumers.
- [x] Run it and confirm RuntimeServices/build graph still fail.
- [x] Remove SessionRepository from RuntimeServices and build_runtime_services.
- [x] Isolate legacy types from the production graph while preserving direct compatibility imports.
- [x] Run contract, SDK, HTTP, CLI, evaluation, and release-document suites.

### Task 6: Final architecture and product-path verification

**Files:**
- Modify: `tests/runtime/test_context_architecture.py`
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: `docs/unified-agent-runtime-migration.md`
- Modify: this plan

**Interfaces:**
- Produces A237 with exact source counts, execution chains, evidence, and debt.

- [x] Audit Main/Sub/Background/Workflow/SDK call graphs and focused-context boundaries.
- [x] Run Ruff, compile/import smoke, full pytest, architecture suites, and offline runtime/concurrency smoke.
- [x] Run `git diff --check` and inspect only current-scope diffs without modifying Git state.
- [x] Record exact results and remaining compatibility/global-registry/external-evidence debt in A237.
- [x] Mark every checkbox complete only after fresh final verification.
