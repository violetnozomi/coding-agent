# Terminal Product Parity Final Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing mature native Agent runtime into one coherent Embedded, Headless, SDK, HTTP, and Remote terminal product without introducing a second runtime truth.

**Architecture:** Product adapters build canonical user messages and call the existing `ProductRunEnvironment`/`AgentRunner`. Commands, extensions, memory, rendering, config, and doctor remain projections over their current owners. Remote adds authenticated HTTP projections only where a daemon must resolve workspace-owned state.

**Tech Stack:** Python 3.9+, standard library, existing Rich/prompt_toolkit/PyYAML dependencies, pytest, Ruff, setuptools.

## Global Constraints

- Freeze AgentRunner, ProductRunEnvironment, SessionRuntime, ToolRuntime, ContextRuntime, RepoIntelligenceService, Verification, Recovery, Memory Core, Skill Core, MCP Core, ProcessService, Web Search, SubAgent, Background Agent, Worktree, and Retrieval unless a product integration test exposes a real bug.
- Do not introduce an Agent framework or a second Session/Process/Memory/Extension truth.
- Do not trust client file paths; validate them against the authoritative workspace.
- Keep semantic embeddings optional and out of the default dependency set.
- Use test-first red/green cycles for every behavior change.

---

### Task 1: Phase A canonical remote submissions

**Files:** Modify `nz_coder/interface/submission.py`, `nz_coder/interface/backend.py`, `nz_coder/http_service/client.py`, `nz_coder/http_service/server.py`, `nz_coder/http_service/manager.py`, `nz_coder/interface/remote.py`; test `tests/test_user_attachments.py`, `tests/test_http_service.py`, `tests/test_daemon.py`.

**Interfaces:** Produce `serialize_submission(message: dict) -> dict`, HTTP `POST /session/:id/run` accepting one canonical user message, and server-side attachment revalidation.

- [x] Write tests proving file parts survive Remote submission and an outside/symlink path is rejected by the daemon.
- [x] Run them and observe failures because Remote accepts only `message: str`.
- [x] Implement canonical message transport and server-side reconstruction with existing `build_user_submission` contracts.
- [x] Run focused tests to green and smoke one authenticated daemon request.

### Task 2: Phase A custom commands

**Files:** Create `nz_coder/interface/custom_commands.py`; modify `nz_coder/interface/commands/registry.py`, `nz_coder/interface/commands/__init__.py`, `nz_coder/interface/terminal_input.py`, `nz_coder/interface/cli.py`, `nz_coder/interface/headless.py`, HTTP client/server/manager and Remote adapter; test `tests/test_custom_commands.py` and relevant terminal/HTTP suites.

**Interfaces:** Produce immutable `PromptCommand`, `CommandCatalog.discover(workspace)`, `expand(name, raw_args)`, and a command policy payload that only narrows `RunRequest.tool_names`.

- [x] Write discovery precedence, invalid frontmatter, `$ARGUMENTS`, positional argument, completion, allowed-tool narrowing, and Remote workspace-resolution tests.
- [x] Run them and observe missing-module/route failures.
- [x] Implement a bounded YAML frontmatter parser and inert prompt expansion; register discovered commands without executing code.
- [x] Run focused tests and a real `/command args` terminal smoke to green.

### Task 3: Phase A/B remote controls and extension lifecycle

**Files:** Modify `nz_coder/extensions/registry.py`, `nz_coder/extensions/cli.py`, owner adapters, HTTP service/client/backend, and Remote commands; test `tests/test_extensions.py`, `tests/test_daemon.py`, `tests/test_terminal_backend.py`.

**Interfaces:** Provide honest extension identity/status plus owner-backed `reload`, `enable`, and `disable`, returning `restart_required` where hot reload is unavailable; expose read-only model/mode/skills/MCP status remotely.

- [x] Write tests for owner delegation, persistence, unsupported lifecycle truth, and Remote inspection.
- [x] Run them and confirm current metadata-only reload and missing Remote controls fail.
- [x] Implement the smallest owner adapters and authenticated HTTP projections.
- [x] Run focused lifecycle and Remote tests to green.

### Task 4: Phase B memory product safety

**Files:** Modify `nz_coder/state/memory_control.py`, memory CLI/commands only if tests reveal gaps; test `tests/test_memory_control.py`, `tests/test_memory_commands.py`.

**Interfaces:** Preserve fingerprint/version compare-and-apply for inspect/approve/reject and expose ledger provenance consistently.

- [x] Add a stale-proposal mutation test and terminal/CLI parity test.
- [x] Run them to determine whether Core already satisfies the contract.
- [x] If the stale test fails, add compare-and-apply validation through `MemoryControlPlane`; otherwise freeze Memory Core.
- [x] Run memory suites to green.

### Task 5: Phase C shared tool presentation

**Files:** Modify `nz_coder/interface/run_renderer.py`; test `tests/test_run_renderer.py`, `tests/test_terminal_backend.py`.

**Interfaces:** Produce immutable presentation data for read, search, edit/apply_patch, bash, process, web, repo lookup, child, verification, and MCP events; deduplicate replay by event identity.

- [x] Write literal-output behavior tests for each high-frequency category and duplicated replay events.
- [x] Run them and identify unsupported categories.
- [x] Implement presentation adapters only; do not modify tool handlers.
- [x] Run renderer and Remote replay tests to green.

### Task 6: Phase C config and doctor UX

**Files:** Create `nz_coder/interface/config_cli.py`; modify `nz_coder/interface/cli.py`, `nz_coder/doctor.py`; test `tests/test_config_cli.py`, `tests/test_doctor.py`.

**Interfaces:** Provide `nz-coder config show [--sources] [--json]` with redaction and doctor classifications `REQUIRED`, `OPTIONAL`, `EXPERIMENTAL` plus actionable hints.

- [x] Write precedence, provenance, redaction, JSON cleanliness, classification, and fix-hint tests.
- [x] Run them and observe the missing command/fields.
- [x] Implement read-only effective-config projection and classified doctor output.
- [x] Run focused CLI/doctor tests and real commands to green.

### Task 7: Phase D packaging, platforms, and scenario benchmark

**Files:** Modify `pyproject.toml`, `scripts/release_smoke.py`; create `nz_coder/interface/platform_capabilities.py`, `scripts/benchmark_terminal_product_final.py`; test `tests/test_platform_capabilities.py`, `tests/test_terminal_product_final.py`, release docs tests.

**Interfaces:** Produce honest Linux/macOS/Windows/WSL capability probes and a `ProductScenarioSuite` covering T1-T20 without network credentials.

- [x] Write platform truth and scenario-result tests, including Windows pipe-only PTY and optional semantic dependencies.
- [x] Run them and observe missing probes/scenarios.
- [x] Implement probes and the bounded offline suite; keep package data restricted to runtime assets.
- [x] Build wheel/sdist, install the wheel in a fresh virtualenv, and run help/doctor/headless/daemon/attach smokes.

### Task 8: Final audit, stress, regression, and documentation

**Files:** Create `docs/terminal-product-parity-final-report-2026-08-13.md`; modify `README.md`, `docs/infcode-alignment-learning-log.md`, and this plan.

**Interfaces:** Produce an 80+ item three-way matrix, Embedded/Headless/SDK/HTTP/Remote surface matrix, product metrics, explicit release verdicts, and evidence links.

- [x] Run width/CJK/ANSI/large-output/input/Remote reconnect stress scenarios with bounded fixtures.
- [x] Run Core Verification, Repo Retrieval, Process, Web Search, Multi-Agent, and Session sanity suites.
- [x] Run the full pytest suite, Ruff, compileall, build/install smoke, and real terminal/daemon smokes.
- [x] Re-read every attachment acceptance item, remove dead product adapters with no consumers, and record exact evidence and remaining honest gaps.
