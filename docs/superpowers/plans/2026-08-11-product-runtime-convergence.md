# Product Runtime Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Interactive, Headless, SDK and HTTP execute through one full Production Runtime without an AgentLoop product dependency.

**Architecture:** Promote the mature environment embedded in AgentLoop to ProductRunEnvironment, retain AgentLoop as compatibility only, and make NativeSDKRunner adapt RunRequest into that environment. HTTP and terminal remain product adapters over AgentClient.

**Tech Stack:** Python 3.9+, asyncio, standard library, existing prompt_toolkit/Rich/OpenAI dependencies.

## Global Constraints

- Do not introduce an Agent framework or another event/attachment/HTTP protocol.
- Do not rewrite the TUI or HTTP server protocol.
- Do not silently replace a real Production service with a no-op.
- Keep AgentLoop only as an explicit compatibility API.
- Use TDD for every production behavior change.

---

### Task 1: Canonical Product Environment

**Files:** Modify `nz_coder/runtime/loop.py`, `runtime/composition.py`, `runtime/host.py`; create `tests/runtime/test_product_environment.py`.

**Interfaces:** Produce `ProductRunEnvironment`, compatibility `AgentLoop`, and a resource scope callable by native requests.

- [x] Write architecture tests that require ProductRunEnvironment and reject AgentLoop construction in product composition.
- [x] Run them and observe the missing canonical type/resource scope failure.
- [x] Promote the mature environment and extract caller-owned resource binding.
- [x] Run the focused tests to green.

### Task 2: Full Native Production Runner

**Files:** Replace `nz_coder/runtime/native_sdk.py`; modify `runtime/core/request.py`, `sdk.py`; test `tests/runtime/test_native_product_runtime.py` and `tests/runtime/test_sdk.py`.

**Interfaces:** `NativeSDKRunner` creates/reuses ProductRunEnvironment and RunOptions carries interaction/event ports.

- [x] Write failing service-fingerprint, no-AgentLoop, MCP/skill/memory/verifier/media/snapshot and Model→Tool→Model tests.
- [x] Verify failures identify the reduced native graph.
- [x] Replace private native services with the full Product environment and delete native no-op classes.
- [x] Run native and SDK tests to green.

### Task 3: HTTP Native Migration

**Files:** Modify `nz_coder/http_service/manager.py`; test HTTP manager/server/client suites and create `tests/http_service/test_native_runtime.py` if needed.

**Interfaces:** ManagedSession owns `AgentClient`, `SessionEventBus`, and InteractionBroker; each run builds `RunRequest`.

- [x] Write a failing architecture test that makes AgentLoop/build_coding_agent construction fatal.
- [x] Verify current HTTP creation fails the test.
- [x] Migrate execution while preserving CRUD/SSE/replay/snapshot/abort/interactions.
- [x] Run the complete HTTP suite to green.

### Task 4: Interactive Native Migration

**Files:** Create `nz_coder/interface/session_controller.py`; modify `interface/cli.py`, `interface/commands/registry.py` and selected handlers; test terminal/controller suites.

**Interfaces:** Produce `TerminalSessionController.run`, `cancel`, `replace_environment`, Session/model/permission and Group 1-3 control operations.

- [x] Write a failing test proving interactive execution does not call build_coding_agent or AgentLoop.run.
- [x] Verify the legacy backend failure.
- [x] Route submissions through controller→RunRequest→AgentClient and incrementally expose explicit command capabilities.
- [x] Run terminal and command tests to green after each handler group.

### Task 5: Surface Parity and Documentation

**Files:** Create `tests/runtime/test_product_surface_parity.py`, `docs/product-runtime-convergence-phase7.md`; update README and learning log.

**Interfaces:** Four-surface matrix and differential observable fingerprint.

- [x] Add parity tests for MCP/tools/memory/skills/verification/media/events/Session.
- [x] Run and fix every silent surface downgrade.
- [x] Record final architecture, legacy consumers, three-way matrix, memory/extension/session/TUI gaps and reranked top ten.
- [x] Write the daemon/attach MVP plan without implementing PTY.

### Task 6: Final Verification

**Files:** No new production interface.

- [x] Run real CLI/headless/HTTP smoke and architecture import checks.
- [x] Run focused product suites and complete pytest.
- [x] Run Ruff, compileall and `git diff --check`.
- [x] Re-read every phase requirement and report only evidence-backed completion or explicit remaining blockers.
