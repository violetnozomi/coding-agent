# MCP Agent Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make existing MCP prompts and resources autonomously discoverable and readable by the Agent without expanding Provider tool schemas.

**Architecture:** Bind the active MCPRuntime through a ContextVar and expose one operation-based registered tool. Reuse all existing transport and runtime methods.

**Tech Stack:** Python 3.9+, standard library, pytest; no new dependencies.

## Global Constraints

- Do not modify MCP wire transports or OAuth.
- Do not expose server commands, environment variables, credentials or tokens.
- Keep runtime binding per execution context.
- Return handler failures with the `Error: ` prefix.
- Do not import `AgentLoop` from the new capability.

---

### Task 1: Run-owned MCP binding

**Files:** `nz_coder/mcp/runtime.py`, `nz_coder/mcp/__init__.py`, `nz_coder/runtime/host.py`, `tests/test_mcp.py`

- [x] Write failing tests for scoped runtime lookup and cleanup.
- [x] Verify the tests fail because the binding API is absent.
- [x] Implement `scoped_mcp_runtime()` and `current_mcp_runtime()`.
- [x] Bind the active runtime in `ProductionRuntimeHost`.
- [x] Run MCP lifecycle regressions.

### Task 2: Agent-facing catalog tool

**Files:** `nz_coder/tools/mcp_catalog.py`, `nz_coder/tools/__init__.py`, `tests/test_mcp_catalog.py`

- [x] Write failing search/get/read/isolation/budget tests.
- [x] Verify failures are caused by the missing tool.
- [x] Implement bounded deterministic catalog search and exact retrieval.
- [x] Register/import the tool without changing dynamic MCP tools.
- [x] Run tool registry and MCP regressions.

### Task 3: Audit and verification

**Files:** `docs/capability-parity-phase5.md`, `docs/infcode-alignment-learning-log.md`, `tests/runtime/test_context_architecture.py`

- [x] Add the capability module to the AgentLoop import guard.
- [x] Update MCP discovery status and retain the Native SDK blocking chain.
- [x] Run full pytest, Ruff, compileall and diff checks.
- [x] Mark executable checkboxes complete only after evidence exists.
