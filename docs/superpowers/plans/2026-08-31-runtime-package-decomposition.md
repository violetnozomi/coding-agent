# NZ-Coder Runtime Package Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the crowded flat `nz_coder/runtime/` module layout with explicit agent, conversation, execution, verification, workflow, process, and session packages without changing product behavior.

**Architecture:** Move complete modules, not callable fragments, into cohesive runtime subpackages and mechanically rewrite every repository-owned import to the canonical location. Keep existing `adapters`, `core`, `model_gateway`, `tool_runtime`, `worktree`, `observability`, and `session` boundaries; package initializers remain dependency-light and perform no eager registration.

**Tech Stack:** Python 3.9+, standard library, pytest, setuptools; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-31-package-structure-reorganization-design.md`

## Global Constraints

- Preserve `nz_coder.sdk`, `nz_coder.cli`, `nz_coder.loop`, `nz_coder.permissions`, benchmark/evaluation façades, `python -m nz_coder`, and every configured console script.
- Preserve every callable, class, constant, tool registration name, schema, handler, side effect, and string-result convention; this is a path-only migration.
- Do not add compatibility wrappers for internal `nz_coder.runtime.<flat_module>` paths.
- Do not eagerly re-export runtime internals from package `__init__.py` files.
- Do not reset, clean, or overwrite unrelated worktree changes, and do not create automated commits in the current dirty worktree.
- Every new package has a module-level docstring; moved modules retain their existing module-level docstrings.
- Python 3.9+ compatibility and the existing dependency limits remain unchanged.

## Locked File Mapping

```text
runtime/agent/
  admission.py agent_manager.py agent_resilience.py agent_role_runtime.py
  agent_transition_runtime.py auto_mode.py child_contracts.py child_result.py
  guardrail_runtime.py guardrails.py handoffs.py lineage.py planning_runtime.py
  subagent.py task_contract.py task_policy.py

runtime/conversation/
  context_manager.py continuation_context.py input_preflight.py
  message_projection.py message_runtime.py model_result.py prompt.py
  prompt_builder.py structured_output.py think_tags.py usage_history.py

runtime/core/
  execution_context.py

runtime/execution/
  composition.py host.py loop.py native_sdk.py
  product_surfaces.py provider_stream.py run_lifecycle.py runner.py
  runtime_state.py services.py tool_executor.py turn_economy.py work_budget.py

runtime/tool_runtime/
  observers.py

runtime/verification/
  completion_gate.py evidence.py hooks.py llm_judge.py recovery.py
  sidecar_verifier.py stall_detector.py stall_sidecar.py
  verification_contract.py verification_scheduler.py

runtime/workflows/
  workflow_builtins.py workflow_capsule.py workflow_contracts.py
  workflow_features.py workflow_generation.py workflow_host.py
  workflow_library.py workflow_lifecycle.py workflow_manifest.py
  workflow_process.py workflow_resolver.py workflow_review.py
  workflow_run_store.py workflow_runtime.py workflow_sdk.py workflow_sweep.py

runtime/process/
  platform_runtime.py process_backends.py process_service.py snapshot_runtime.py
  workspace_snapshot.py workdir.py

runtime/session/
  lifecycle.py model.py runtime.py session_processor.py session_repository.py
  session_revert.py store.py
```

Existing `runtime/adapters/`, `runtime/core/`, `runtime/model_gateway/`,
`runtime/observability/`, `runtime/tool_runtime/`, and `runtime/worktree/` retain
their current names. Shared tool-execution result and mutation-classification
contracts live in `tool_platform/execution.py`, below runtime orchestration.

The leaf-package dependency guard has two exact, lazy composition seams:

```text
runtime/adapters/runner.py -> runtime/execution/run_lifecycle.py
runtime/agent/subagent.py -> runtime/execution/composition.py
```

No other runtime leaf module may import `runtime.execution`.

---

### Task 1: Lock the runtime-root architecture contract

**Files:**
- Modify: `tests/test_package_structure.py`

**Interfaces:**
- Consumes: the mapping above and the existing AST import helper.
- Produces: a runtime-root allowlist and required-subpackage contract that stays RED until all flat modules move.

- [ ] **Step 1: Add the failing runtime-root structure test**

```python
EXPECTED_RUNTIME_ROOT_MODULES = {"__init__.py"}
EXPECTED_RUNTIME_SUBPACKAGES = {
    "adapters", "agent", "conversation", "core", "execution",
    "model_gateway", "observability", "process", "session", "tool_runtime",
    "verification", "workflows", "worktree",
}

def test_runtime_root_contains_only_domain_packages():
    runtime_root = PACKAGE_ROOT / "runtime"
    actual_modules = {path.name for path in runtime_root.glob("*.py")}
    actual_packages = {
        path.name for path in runtime_root.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    assert actual_modules == EXPECTED_RUNTIME_ROOT_MODULES
    assert actual_packages == EXPECTED_RUNTIME_SUBPACKAGES
```

- [ ] **Step 2: Run the test and verify RED**

Run: `pytest -q tests/test_package_structure.py::test_runtime_root_contains_only_domain_packages`

Expected: FAIL listing the current flat runtime modules and the six missing packages.

### Task 2: Move process and Session lifecycle modules

**Files:**
- Create: `nz_coder/runtime/process/__init__.py`
- Move: the six `runtime/process/` modules and three flat `session_*` modules from the locked mapping.
- Modify: repository-owned Python imports under `nz_coder/` and `tests/`.

**Interfaces:**
- Consumes: existing workdir `ContextVar`, process service registry, workspace snapshot, and Session types unchanged.
- Produces: `nz_coder.runtime.process.*` and consolidated `nz_coder.runtime.session.*` canonical imports.

- [ ] **Step 1: Move the exact files and add a docstring-only process initializer**

```python
"""Workspace, platform process, and snapshot runtime services."""
```

- [ ] **Step 2: Rewrite exact import prefixes across `nz_coder/` and `tests/`**

```text
nz_coder.runtime.platform_runtime -> nz_coder.runtime.process.platform_runtime
nz_coder.runtime.process_backends -> nz_coder.runtime.process.process_backends
nz_coder.runtime.process_service -> nz_coder.runtime.process.process_service
nz_coder.runtime.snapshot_runtime -> nz_coder.runtime.process.snapshot_runtime
nz_coder.runtime.workspace_snapshot -> nz_coder.runtime.process.workspace_snapshot
nz_coder.runtime.workdir -> nz_coder.runtime.process.workdir
nz_coder.runtime.session_processor -> nz_coder.runtime.session.session_processor
nz_coder.runtime.session_repository -> nz_coder.runtime.session.session_repository
nz_coder.runtime.session_revert -> nz_coder.runtime.session.session_revert
```

- [ ] **Step 3: Run focused process and Session tests**

Run: `pytest -q tests/test_process_backends.py tests/test_process_service.py tests/test_windows_platform_runtime.py tests/test_workspace_snapshot.py tests/test_session_processor.py tests/runtime/session tests/runtime/test_session_repository.py tests/test_session_revert.py tests/test_session_lifecycle.py`

Expected: all selected tests pass.

### Task 3: Move conversation modules and usage history

**Files:**
- Create: `nz_coder/runtime/conversation/__init__.py`
- Move: the eleven `runtime/conversation/` modules from the locked mapping.
- Modify: repository-owned Python imports under `nz_coder/` and `tests/`.

**Interfaces:**
- Consumes: protocol messages, state compaction, model gateway envelopes, and process workdir.
- Produces: canonical prompt, context, projection, structured-output, and think-tag modules.

- [ ] **Step 1: Move the exact files and add the package initializer**

```python
"""Prompt, context, message, and structured-output runtime services."""
```

- [ ] **Step 2: Rewrite each old flat prefix to `nz_coder.runtime.conversation.<module>`**

The exact module names are `context_manager`, `continuation_context`,
`input_preflight`, `message_projection`, `message_runtime`, `model_result`,
`prompt`, `prompt_builder`, `structured_output`, `think_tags`, and
`usage_history`.

- [ ] **Step 3: Run focused conversation tests**

Run: `pytest -q tests/test_context_budget.py tests/runtime/test_context_runtime.py tests/runtime/test_continuation_context.py tests/runtime/test_message_projection.py tests/runtime/test_prompt_builder_runtime.py tests/test_runtime_prompt.py tests/test_structured_output.py tests/test_think_tag_demux.py tests/test_document_preflight.py`

Expected: all selected tests pass.

### Task 4: Move Agent policy and child-execution modules

**Files:**
- Create: `nz_coder/runtime/agent/__init__.py`
- Move: the sixteen `runtime/agent/` modules from the locked mapping.
- Modify: repository-owned Python imports under `nz_coder/` and `tests/`.

**Interfaces:**
- Consumes: conversation structured output, model gateway, Session store, tool runtime, process workdir, and worktree manager.
- Produces: canonical admission, Agent graph, child result, Auto mode, guardrail, policy, and subagent modules.

- [ ] **Step 1: Move the exact files and add the package initializer**

```python
"""Agent admission, policy, handoff, resilience, and child execution."""
```

- [ ] **Step 2: Rewrite each old flat prefix to `nz_coder.runtime.agent.<module>`**

Apply the sixteen exact basenames in the locked mapping. Rewrite both
`from ... import ...` and side-effect `import nz_coder.runtime.<module>` forms.

- [ ] **Step 3: Run focused Agent tests**

Run: `pytest -q tests/test_agent_admission.py tests/test_agent_manager.py tests/test_agent_resilience.py tests/test_auto_mode.py tests/test_auto_mode_guardrail.py tests/test_auto_mode_router.py tests/test_child_result.py tests/test_handoffs.py tests/test_subagent.py tests/runtime/test_task_contract.py tests/test_task_policy.py`

Expected: all selected tests pass with child worktree and tool-registration behavior unchanged.

### Task 5: Consolidate verification modules

**Files:**
- Move: the eight remaining flat verification modules into the existing `nz_coder/runtime/verification/` package.
- Modify: repository-owned Python imports under `nz_coder/` and `tests/`.

**Interfaces:**
- Consumes: foundation error classification, Agent task contracts/policy, execution context, and intelligence verification planning.
- Produces: one canonical package for recovery, evidence, completion gates, hooks, judges, stalls, contracts, and scheduling.

- [ ] **Step 1: Move the exact files**

Move `completion_gate.py`, `hooks.py`, `llm_judge.py`, `sidecar_verifier.py`,
`stall_detector.py`, `stall_sidecar.py`, `verification_contract.py`, and
`verification_scheduler.py`; keep existing `evidence.py` and `recovery.py`.

- [ ] **Step 2: Rewrite each old flat prefix to `nz_coder.runtime.verification.<module>`**

Do not change `nz_coder.runtime.verification.evidence` or
`nz_coder.runtime.verification.recovery`, which are already canonical.

- [ ] **Step 3: Run focused verification tests**

Run: `pytest -q tests/runtime/test_completion_gate.py tests/test_hooks.py tests/test_llm_judge.py tests/test_sidecar_verifier.py tests/test_stall_detector.py tests/test_stall_sidecar.py tests/runtime/test_verification_contract.py tests/runtime/test_verification_scheduler.py tests/test_recovery.py`

Expected: all selected tests pass.

### Task 6: Move declarative workflow modules

**Files:**
- Create: `nz_coder/runtime/workflows/__init__.py`
- Move: every sixteen `workflow_*` module from the locked mapping.
- Modify: repository-owned Python imports under `nz_coder/` and `tests/`.

**Interfaces:**
- Consumes: Agent child results/manager, process workdir, worktree manager, state Session identity, and tool registration.
- Produces: `nz_coder.runtime.workflows.*` as the single workflow namespace.

- [ ] **Step 1: Move all sixteen modules and add the package initializer**

```python
"""Declarative workflow definitions, persistence, execution, and tools."""
```

- [ ] **Step 2: Rewrite every `nz_coder.runtime.workflow_<suffix>` prefix**

The target preserves the basename beneath the package, for example
`nz_coder.runtime.workflow_runtime` becomes
`nz_coder.runtime.workflows.workflow_runtime`.

- [ ] **Step 3: Run focused workflow tests**

Run: `pytest -q tests/test_workflow_advanced.py tests/test_workflow_capsules.py tests/test_workflow_generation.py tests/test_workflow_host.py tests/test_workflow_lifecycle_advanced.py tests/test_workflow_parity_contracts.py tests/test_workflow_process.py tests/test_workflow_runtime.py`

Expected: all selected tests pass with the same registered workflow tools.

### Task 7: Move execution composition and state-machine modules

**Files:**
- Create: `nz_coder/runtime/execution/__init__.py`
- Move: the thirteen `runtime/execution/` modules from the locked mapping.
- Move: `execution_context.py` to `runtime/core/`, `usage_history.py` to
  `runtime/conversation/`, and `tool_observers.py` to
  `runtime/tool_runtime/observers.py`.
- Create: `tool_platform/execution.py` for contracts shared by execution and
  runtime leaf packages.
- Modify: repository-owned Python imports under `nz_coder/` and `tests/`.

**Interfaces:**
- Consumes: Agent, conversation, process, Session, verification, model gateway,
  core, adapters, tool runtime, and tool-platform contracts.
- Produces: the canonical production loop, runner, host, services, lifecycle, SDK adapter, runtime state, budgets, and tool execution namespace.

- [ ] **Step 1: Move the exact files and add the package initializer**

```python
"""Production Agent composition, execution state machine, and lifecycle."""
```

- [ ] **Step 2: Rewrite each old flat prefix to its canonical package**

Apply all exact basenames in the locked mapping. Preserve the public root
façades `nz_coder.loop` and `nz_coder.sdk`; only their internal imports change
to the new canonical paths. Keep `ToolExecutor` in runtime execution while
leaf packages import shared result and mutation contracts from
`nz_coder.tool_platform.execution`.

- [ ] **Step 3: Run execution and entrypoint tests**

Run: `pytest -q tests/test_runtime_composition.py tests/runtime/test_native_runner.py tests/runtime/test_native_service_contexts.py tests/runtime/test_run_lifecycle.py tests/runtime/test_turn_economy.py tests/runtime/test_work_budget.py tests/test_execution_context.py tests/test_runtime_state.py tests/test_tool_cancellation_context.py tests/test_smoke.py tests/test_cli_commands.py tests/test_tool_side_effects.py`

Expected: all selected tests pass and tool registration remains complete and duplicate-free.

### Task 8: Final canonical-path and documentation verification

**Files:**
- Modify: `nz_coder/runtime/__init__.py`
- Modify: current architecture documents that link to old flat runtime paths.
- Modify only migration-caused defects found by final verification.

**Interfaces:**
- Consumes: Tasks 1–7 final package tree.
- Produces: a dependency-light runtime namespace, no flat runtime Python modules, and a fully verified product baseline.

- [ ] **Step 1: Keep `runtime/__init__.py` dependency-light**

```python
"""Agent runtime domain packages and production execution services."""
```

- [ ] **Step 2: Update current architecture-document links**

Rewrite active architecture documentation paths in `docs/architecture.md`,
`docs/nzcoder_core_architecture.md`, `docs/unified-agent-runtime-migration.md`,
and `docs/product-runtime-convergence-phase7.md`. Do not rewrite historical
plans/specs whose old paths describe the state at their publication time.

- [ ] **Step 3: Verify the runtime-root architecture test is GREEN**

Run: `pytest -q tests/test_package_structure.py`

Expected: all package-root and runtime-root architecture tests pass.

The dependency-direction test permits only the two exact lazy composition
seams documented above; all shared primitives must remain outside
`runtime.execution`.

- [ ] **Step 4: Verify no repository-owned Python import names a removed flat runtime module**

Run: `rg -n 'nz_coder\.runtime\.(admission|agent_manager|agent_resilience|agent_role_runtime|agent_transition_runtime|auto_mode|child_contracts|child_result|completion_gate|composition|context_manager|continuation_context|execution_context|guardrail_runtime|guardrails|handoffs|hooks|host|input_preflight|lineage|llm_judge|loop|message_projection|message_runtime|model_result|native_sdk|planning_runtime|platform_runtime|process_backends|process_service|product_surfaces|prompt|prompt_builder|provider_stream|run_lifecycle|runner|runtime_state|services|session_processor|session_repository|session_revert|sidecar_verifier|snapshot_runtime|stall_detector|stall_sidecar|structured_output|subagent|task_contract|task_policy|think_tags|tool_executor|tool_observers|turn_economy|usage_history|verification_contract|verification_scheduler|work_budget|workdir|workflow_)' nz_coder tests -g '*.py'`

Expected: no output.

- [ ] **Step 5: Compile and run the full suite**

Run: `python -m compileall -q nz_coder`

Run: `pytest -q`

Expected: exit code 0 and no failures, with the root-migration baseline of
`3064 passed, 21 skipped` plus only the new runtime architecture test.
