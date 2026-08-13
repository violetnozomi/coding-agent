# Runtime Closure and Capability Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Freeze one native Agent execution core, retire avoidable legacy ownership, unify background/Workflow boundaries, add middleware/events/native SDK, and produce a benchmark-driven capability roadmap.

**Architecture:** Preserve AgentRunner/SessionRuntime as the single execution kernel. Add one host-neutral middleware pipeline and make RuntimeEvent project into the existing SessionEventBus. Keep task/workflow scheduling outside Session and provide a direct typed SDK path while retaining explicit legacy compatibility.

**Tech Stack:** Python 3.9+, asyncio, dataclasses, Protocols, ContextVar, pytest; no Agent framework or new runtime dependency.

## Global Constraints

- Do not introduce another model/tool/session loop.
- Do not add narrow `xxx_runtime.py` abstractions without real ownership or multiple consumers.
- Preserve public tool names, legacy AgentLoop APIs, Provider/MCP behavior, worktree isolation, verification, Memory, CLI/HTTP/evaluation, and Workflow contracts.
- Every behavior change follows red-green-refactor.
- Implement at most two benchmark-justified capability gaps.
- Stop after reports and verification; do not continue broad capability work.
- Do not commit, merge, push, reset, clean, or rewrite existing Git state.

---

### Task 1: Source Audit and Architecture Freeze Baseline

**Files:**
- Modify: `docs/infcode-alignment-learning-log.md`
- Test: `tests/runtime/test_context_architecture.py`

**Interfaces:**
- Produces: verified Main/child/background/Workflow call graph, adapter consumer table, AgentLoop ownership categories, dual-source table, and before metrics.

- [x] Re-read current native Runner, adapters, AgentLoop, SubAgent, background manager, Workflow, SDK, Session, events, and reference implementations.
- [x] Add architecture assertions proving native Runner has no legacy edge and Workflow contains no Provider/tool loop.
- [x] Record exact baseline metrics and unresolved dual sources.
- [x] Run existing native, child, manager, Workflow, SDK, Session, and architecture tests.

### Task 2: Ordered Runtime Middleware Pipeline

**Files:**
- Create: `nz_coder/runtime/core/middleware.py`
- Modify: `nz_coder/runtime/runner.py`
- Modify: `nz_coder/runtime/core/__init__.py`
- Test: `tests/runtime/test_middleware.py`

**Interfaces:**
- Produces: `RuntimeMiddleware`, `MiddlewarePipeline`; ordered run/model/tool-batch hooks injected into `AgentRunner`.

- [x] Write failing tests for forward before-order, reverse after-order, original-error observation, and middleware failure propagation.
- [x] Run tests and verify failures are caused by the missing pipeline.
- [x] Implement the minimal immutable pipeline without coding-specific imports.
- [x] Integrate one pipeline into native and legacy shared Runner paths around run/model/tool-batch execution.
- [x] Run middleware, Runner, cancellation, model, tool, and full focused runtime tests.

### Task 3: Unified Runtime Event Projection

**Files:**
- Modify: `nz_coder/runtime/core/events.py`
- Modify: `nz_coder/runtime/services.py`
- Modify: `nz_coder/runtime/core/middleware.py`
- Modify: `nz_coder/runtime/session/runtime.py`
- Test: `tests/runtime/test_runtime_events.py`

**Interfaces:**
- Produces: stable `RuntimeEventName`; host-free `RuntimeEventSink.publish(event)`; event middleware projecting to the existing SessionEventBus.

- [x] Write failing order tests for Session opened, run, model, tool, terminal, and error events.
- [x] Implement a host-neutral event envelope and fail-open production SessionEventBus projection.
- [x] Mark Session open as created/resumed in RunContext metadata.
- [x] Install event middleware in AgentRunner's default pipeline without duplicating UI event systems.
- [x] Run event bus, renderer, HTTP/SSE, Runner, child, and cancellation tests.

### Task 4: Native Public SDK Run, Child, and Resume

**Files:**
- Modify: `nz_coder/runtime/runner.py`
- Modify: `nz_coder/sdk.py`
- Modify: `nz_coder/runtime/core/__init__.py`
- Modify: `nz_coder/runtime/session/__init__.py`
- Test: `tests/runtime/test_sdk.py`

**Interfaces:**
- Produces: `AgentRunner.run_result(request, options) -> RunResult`; direct `AgentClient(runner=...)`; `AgentClient.run_child(...)` with parent-linked resume.

- [x] Write a failing SDK-only typed run test that rejects AgentLoop construction.
- [x] Write failing SDK child and process-restart resume tests.
- [x] Add typed native result projection owned by AgentRunner/RunContext.
- [x] Make direct Runner the primary SDK path; retain `agent_factory` as explicit compatibility.
- [x] Export stable Agent/Runner/Session/RunRequest/RunResult/Tool contracts.
- [x] Run SDK, handoff, Session, native Runner, and product compatibility tests.

### Task 5: Background and Workflow Boundary Closure

**Files:**
- Modify: `nz_coder/runtime/agent_manager.py`
- Modify: `tests/test_agent_manager.py`
- Modify: `tests/test_workflow_runtime.py`
- Modify: `tests/runtime/test_context_architecture.py`

**Interfaces:**
- Produces: standard child lifecycle events and static proof that foreground/background/process/Workflow Agent nodes end at the same run_subagent/AgentRunner chain.

- [x] Add failing event tests for background child started/finished ordering.
- [x] Add architecture tests forbidding Provider/model/tool loops in AgentManager and WorkflowRuntime.
- [x] Project child lifecycle through SessionEventBus while keeping scheduler fields in TaskRecord.
- [x] Verify thread/process cancellation, resume/result, Workflow DAG/cache/budget/artifact behavior.
- [x] Run manager, workflow, child, Session, HTTP event, and worktree tests.

### Task 6: Legacy Burn-down and Core Import Boundaries

**Files:**
- Modify: `tests/runtime/test_context_architecture.py`
- Modify: `docs/unified-agent-runtime-migration.md`

**Interfaces:**
- Produces: adapter consumer/delete-condition table, zero-consumer deletion decisions, AgentLoop attribute ownership table, core import guards, and maintenance-only freeze rules.

- [x] Add static guards forbidding runtime/core imports of LSP, repo map, verification planner, project creation, interface, and AgentLoop.
- [x] Recount every adapter consumer and direct host-private access.
- [x] Delete only wrappers with zero source consumers; document why retained adapters cannot yet be removed.
- [x] Classify all AgentLoop init attributes and migrate any remaining per-run duplicate whose owner already exists.
- [x] Run architecture, legacy facade, CLI/HTTP/SDK, and full focused product tests.

### Task 7: Benchmark-driven Capability Parity and Final Reports

**Files:**
- Create: `docs/runtime-architecture-closure.md`
- Create: `docs/three-way-capability-matrix.md`
- Create: `docs/next-capability-roadmap.md`
- Create: `docs/repo-intelligence-gap-report.md`
- Create: `scripts/benchmark_tool_exposure.py`
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: this plan

**Interfaces:**
- Produces: 20/60/120 tool exposure benchmark/design decision, fresh complete parity matrix, Reports A/B/C, architecture diagrams, next P0-P3 roadmap, and final quantitative/SCC evidence.

- [x] Build a deterministic provider-free 20/60/120 tool benchmark for schema tokens, retrieval recall, and latency.
- [x] Run it before deciding whether runtime exposure filtering is justified.
- [x] Re-scan InfCodeX, infcode-dev/OpenCode, and nzcoder capability implementations rather than filenames.
- [x] Write the full matrix, repo intelligence gap report, Architecture Closure report, and P0-P3 roadmap.
- [x] Measure after metrics, SCC count/largest/modules, middleware count, background/workflow paths, and duplicated Session state.
- [x] Run Ruff, compile/import smoke, benchmark, focused product/runtime suites, full pytest, and `git diff --check`.
- [x] Mark every checkbox complete only after fresh final verification.
