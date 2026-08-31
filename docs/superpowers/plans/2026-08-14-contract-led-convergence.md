# Contract-Led Long-Task Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make long multi-file tasks contract-led, repository-aware, verification-efficient, closure-safe, context-fresh, and provider-schema-safe without adding model calls.

**Architecture:** Extend the existing planner response into a validated plan-plus-contract envelope; keep objective runtime facts separate from a requirement ledger; inject a bounded first-turn workset; schedule verification by budget zone; reserve two narrow closure calls; project stale evidence out of model context; adapt canonical schemas at the provider boundary.

**Tech Stack:** Python 3.9+, standard library, pytest, existing NZ-Coder Runtime/Provider abstractions.

## Global Constraints

- Do not add an Agent framework or external dependency.
- Do not add a planner model call.
- Preserve public tool names, handler signatures, and durable Session evidence.
- Keep the configured 20-call maximum as the emergency hard cap.
- Use TDD for every behavior change.
- Do not commit or create a worktree in the user's shared dirty workspace.

---

### Task 1: Task contract and requirement completion

**Files:**
- Create: `nz_coder/runtime/task_contract.py`
- Create: `nz_coder/runtime/completion_gate.py`
- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: `nz_coder/runtime/loop.py`
- Modify: `nz_coder/runtime/adapters/runner.py`
- Test: `tests/runtime/test_task_contract.py`
- Test: `tests/runtime/test_completion_gate.py`
- Test: `tests/test_loop_fake.py`

**Interfaces:**
- Produces: `TaskContract.from_planner_output(text, workspace)`, `RequirementLedger.observe_tool(...)`, and `CompletionGate.evaluate(runtime_state)`.
- Consumes: the existing planning call output, RuntimeState mutation generations, changed paths, and VerificationContract evidence.

- [x] Write tests proving malformed/unsafe/duplicate requirements are normalized without blocking planning.
- [x] Run the focused tests and confirm failure because the contract module does not exist.
- [x] Implement immutable contract/evidence records plus a mutable ledger and JSON-safe persistence.
- [x] Run focused tests and confirm the contract/ledger behavior passes.
- [x] Write and fail tests proving natural completion reopens when hard requirements lack current evidence.
- [x] Integrate the planner envelope and CompletionGate, then rerun focused tests.

### Task 2: Implementation bundle and execution facts

**Files:**
- Create: `nz_coder/intelligence/implementation_bundle.py`
- Modify: `nz_coder/intelligence/project_profile.py`
- Modify: `nz_coder/runtime/prompt_builder.py`
- Modify: `nz_coder/runtime/loop.py`
- Test: `tests/test_project_profile.py`
- Test: `tests/test_implementation_bundle.py`
- Test: `tests/test_runtime_prompt.py`

**Interfaces:**
- Produces: `build_project_execution_facts(workspace) -> dict` and `build_implementation_bundle(...) -> str`.
- Consumes: TaskContract expected artifacts, RepoRetrievalPolicy candidates, and the repository intelligence service.

- [x] Write failing tests for Python package/module cwd facts and bounded multi-artifact bundle output.
- [x] Implement execution facts with workspace-safe normalized paths.
- [x] Implement a bounded first-turn bundle using exact artifacts first and high-confidence candidates second.
- [x] Inject the bundle only on the first turn of moderate/complex multi-artifact coding tasks.
- [x] Run focused prompt and repository tests.

### Task 3: Layered verification scheduling

**Files:**
- Create: `nz_coder/runtime/verification_scheduler.py`
- Modify: `nz_coder/runtime/verification_contract.py`
- Modify: `nz_coder/runtime/runner.py`
- Modify: `nz_coder/intelligence/verification.py`
- Test: `tests/runtime/test_verification_scheduler.py`
- Test: `tests/runtime/test_verification_contract.py`
- Test: `tests/runtime/test_runner.py`

**Interfaces:**
- Produces: `VerificationScheduler.action(zone, ledger, manager) -> VerificationAction`.
- Consumes: VerificationManager's existing staged pipeline and the exact user VerificationContract.

- [x] Write failing tests proving yellow/orange do not execute exact acceptance.
- [x] Implement zone actions: yellow static, orange targeted, red affected/convergence, completion exact acceptance.
- [x] Allow red exact acceptance only when no hard requirement remains pending.
- [x] Integrate scheduler decisions in Runner and run focused tests.

### Task 4: Closure reserve

**Files:**
- Modify: `nz_coder/runtime/work_budget.py`
- Modify: `nz_coder/runtime/runner.py`
- Modify: `nz_coder/runtime/loop.py`
- Test: `tests/runtime/test_work_budget.py`
- Test: `tests/runtime/test_runner.py`
- Test: `tests/test_loop_fake.py`

**Interfaces:**
- Produces: `WorkBudgetController.phase(completed_turns)` and a closure-safe tool allowlist.
- Consumes: configured hard cap and current RuntimeState/RequirementLedger.

- [x] Write failing tests for 13 normal plus 2 reserved calls under a 20-call hard cap.
- [x] Implement nominal and closure budget calculations without changing the hard cap.
- [x] Restrict closure turns to known-path reads, writes, diff, and focused verification.
- [x] Replace the final hard-disable prompt with repair/finalize guidance and run focused tests.

### Task 5: Semantic supersession

**Files:**
- Modify: `nz_coder/runtime/tool_runtime/result_projection.py`
- Modify: `nz_coder/runtime/message_projection.py`
- Test: `tests/runtime/tool_runtime/test_focused_projection.py`
- Test: `tests/runtime/test_context_architecture.py`

**Interfaces:**
- Produces: durable `_nz_resource`, `_nz_evidence_kind`, `_nz_mutation_generation`, and `_nz_verification_passed` metadata plus stale model markers.
- Consumes: tool name/input/output and RuntimeState mutation generation.

- [x] Write failing tests proving an old file read is superseded after the same file changes.
- [x] Stamp tool messages with resource/generation evidence metadata.
- [x] Project stale reads and failed verification output to compact markers while preserving durable messages.
- [x] Run focused context/projection tests.

### Task 6: Provider schema adapter and linter

**Files:**
- Create: `nz_coder/providers/tool_schema.py`
- Modify: `nz_coder/runtime/loop.py`
- Test: `tests/test_tool_schema.py`
- Test: `tests/test_model_capabilities.py`

**Interfaces:**
- Produces: `adapt_tool_specs(specs, provider, model) -> list[dict]` and `lint_tool_specs(specs) -> list[SchemaIssue]`.
- Consumes: canonical tool definitions from `get_specs()`; handlers remain unchanged.

- [x] Write failing tests for recursive nested required fields, DeepSeek simplification, and canonical-schema immutability.
- [x] Implement the recursive schema adapter and linter.
- [x] Apply adaptation exactly once at the model-facing tool boundary.
- [x] Run provider/tool-schema tests.

### Task 7: Verification and documentation

**Files:**
- Modify: `docs/terminal-product-real-world-issues.md`
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: this plan's checkboxes.

- [x] Run all focused tests for Tasks 1–6.
- [x] Run the complete pytest suite.
- [x] Run Ruff on changed Python files.
- [x] Run `git diff --check`.
- [x] Run one fake-provider end-to-end terminal/runtime scenario.
- [x] Record implemented behavior, evidence, known limitations, and the deferred live-model A/B.
