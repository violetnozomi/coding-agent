# Tool Result Budget and Memory Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one model-visible tool result budget and route automatic memory learning through a durable proposal/review/apply ledger.

**Architecture:** Pure policy modules own projection and memory governance. Existing Tool Runtime and MemoryManager remain execution/storage adapters and `AgentLoop` receives no new capability.

**Tech Stack:** Python 3.9+, asyncio-compatible standard library, pytest; no new dependency.

## Global Constraints

- Preserve existing tool registration and handler return contracts.
- Keep full tool output durable when the model-visible result is truncated.
- Do not bypass the current MemoryManager backend or retrieval system.
- Do not import `runtime.loop.AgentLoop` from either capability.
- Use TDD for every behavior change.

---

### Task 1: Unified tool result policy

**Files:**
- Create: `nz_coder/tool_platform/results.py`
- Modify: `nz_coder/runtime/tool_runtime/result_projection.py`
- Test: `tests/tool_platform/test_result_projection.py`

**Interfaces:**
- Produces: `ToolResultBudget.for_context()`, `ToolResultProjector.project()` and `ProjectedToolResult`.
- Consumes: existing session artifact path and token estimator.

- [x] Write failing tests for identity, bounded head/tail projection, artifact reference, metadata and context-derived budgets.
- [x] Run the focused tests and confirm failures are caused by missing interfaces.
- [x] Implement immutable budget/result types and atomic full-output persistence.
- [x] Integrate the projector at the single production tool-result append boundary.
- [x] Run focused and Tool Runtime regression tests.

### Task 2: Memory proposal store and policy

**Files:**
- Create: `nz_coder/state/memory_control.py`
- Modify: `nz_coder/state/memory.py`
- Test: `tests/test_memory_control.py`

**Interfaces:**
- Produces: `MemoryProposal`, `MemoryControlPlane.submit()`, `approve()`, `reject()`, `pending()` and `ledger()`.
- Consumes: `MemoryManager.save()` only after an apply decision.

- [x] Write failing tests for proposal fields, safe auto-apply, review, dedupe, approval/rejection, provenance and concurrent submission.
- [x] Run focused tests and confirm the missing control plane is the failure.
- [x] Implement risk classification, atomic inbox documents and append-only ledger.
- [x] Route `run_auto_memory_pipeline()` candidates through the control plane.
- [x] Run focused and existing memory regressions.

### Task 3: Audit, benchmark and documentation

**Files:**
- Modify: `scripts/benchmark_capability_parity.py`
- Modify: `docs/capability-parity-phase5.md`
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: `tests/runtime/test_context_architecture.py`

- [x] Add before/after large-output and poisoned-memory behavioral measurements.
- [x] Add architecture import guards for both new capability modules.
- [x] Record the SDK blocking chain and update capability statuses honestly.
- [x] Run focused tests, full tests if disk permits, Ruff, compileall and diff checks.
- [x] Mark every executable plan checkbox complete only after evidence exists.
