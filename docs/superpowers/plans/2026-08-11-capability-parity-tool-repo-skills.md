# Coding Agent Capability Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a fresh three-way capability verdict and implement benchmark-justified Tool Intelligence, Repo Intelligence V2, and Governed Skills without reopening the Agent Runtime.

**Architecture:** Adapt existing registries and indexes through focused services. Run-scoped ContextVar bindings connect Session-owned state to tool exposure and skill policy, while the existing AgentRunner and ToolRuntime remain the only execution kernel.

**Tech Stack:** Python 3.9+, asyncio, dataclasses, ContextVar, sqlite/json caches, pytest; standard library only.

## Global Constraints

- No new Agent loop, framework, provider dependency, or Runtime-wide refactor.
- Preserve tool names/handlers, MCP/LSP/Workflow behavior, Session truth, and legacy public contracts.
- Implement at most three capability clusters and distinguish lexical/structural/symbol/semantic search honestly.
- Every behavior change follows red-green-refactor.
- Do not commit, merge, push, reset, clean, or rewrite existing Git state.

### Task 1: Fresh source audit and benchmark baseline

**Files:** Create `docs/capability-parity-phase5.md`; modify benchmark scripts and architecture tests.

- [x] Re-read real call chains in all three repositories and record evidence for 50+ capabilities.
- [x] Benchmark 20/60/120/200 tool catalogs and current repo/skill behavior before implementation.
- [x] Add static guards preventing new capability modules from importing AgentLoop or concrete UI/runtime core violations.

### Task 2: Tool catalog and search index

**Files:** Create `nz_coder/tool_platform/catalog.py`, `nz_coder/tool_platform/search.py`; modify `nz_coder/tools/__init__.py`; test `tests/tool_platform/test_catalog_search.py`.

- [x] Write failing immutable catalog, dynamic MCP inclusion, exact/keyword ranking, and collision tests.
- [x] Implement registry adapters and deterministic weighted search with bounded schema collection.
- [x] Verify legacy registry behavior remains unchanged outside an exposure scope.

### Task 3: Progressive exposure and per-run unlock

**Files:** Create `nz_coder/tool_platform/exposure.py`, `nz_coder/tools/tool_search.py`; modify RuntimeServices/AgentRunner composition; test `tests/tool_platform/test_exposure.py`.

- [x] Write failing resident/deferred/search-next-turn/session/child/MCP-scale contract tests.
- [x] Implement budget-aware planner, run-owned unlock state, middleware binding, and `tool_search`.
- [x] Integrate through Runtime middleware/service composition rather than AgentLoop capability methods.

### Task 4: Repository graph and query tool

**Files:** Create `nz_coder/intelligence/repository_graph.py`, `nz_coder/tools/repo_context.py`; test `tests/test_repository_graph.py`.

- [x] Write failing cold/warm/update/delete/rename/import/cycle/multi-language tests.
- [x] Implement bounded persistent graph materialization and explicit language parsers.
- [x] Register operation-based overview/changed/module/relationship/cycles queries with safe paths and bounded output.

### Task 5: Governed skill execution

**Files:** Modify `nz_coder/state/skills.py`, `nz_coder/runtime/tool_runtime/policy.py`; test `tests/test_skill_governance.py`.

- [x] Write failing precedence/model/provenance/resource/invalid/reload/isolation/enforcement tests.
- [x] Parse and validate model metadata; bind a per-run SkillExecutionContext.
- [x] Enforce allowed-tools as an intersection in the Tool policy with auditable rejection metadata.

### Task 6: Capability benchmarks and reports

**Files:** Create `scripts/benchmark_capability_parity.py`; create/update phase reports and learning log.

- [x] Run after benchmarks and compare schema tokens, selection, index/cache/update/query, and governance metrics.
- [x] Write Current Verdict, 50+ matrix, Top 10 Real Gaps, False Gaps, and capability roadmap.
- [x] Record Native SDK default legacy path and other unimplemented gaps without overclaiming.

### Task 7: Final verification and closure

**Files:** Modify this plan and `docs/infcode-alignment-learning-log.md`.

- [x] Run focused capability and architecture suites.
- [x] Run full pytest, Ruff, compile/import smoke, benchmark, SCC scan, and `git diff --check`.
- [x] Mark every checkbox complete only from fresh evidence and stop capability expansion.
