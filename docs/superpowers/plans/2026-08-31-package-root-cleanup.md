# NZ-Coder Package Root Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce `nz_coder/` to formal public façades and move internal root modules into explicit domain packages without changing product behavior.

**Architecture:** Introduce dependency-light `foundation`, `protocol`, and `capabilities` packages, move other internal modules to their existing owning domains, and rewrite every repository-owned import to the canonical path. Keep only the approved public root façades and evaluation entrypoint wrappers.

**Tech Stack:** Python 3.9+, standard library, pytest, setuptools; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-31-package-structure-reorganization-design.md`

## Global Constraints

- Preserve all formal public imports listed in the spec.
- Preserve tool registration names, descriptions, schemas, side effects, handlers, and string return conventions.
- Do not change runtime behavior while moving modules.
- Do not reset, clean, or overwrite unrelated worktree changes.
- Do not create automated commits in the current dirty worktree.
- Every new package and moved module retains a module-level docstring.
- Python 3.9+ compatibility and existing dependency limits remain unchanged.

---

### Task 1: Lock the root-package architecture contract

**Files:**
- Create: `tests/test_package_structure.py`

**Interfaces:**
- Consumes: repository root and importable `nz_coder` package.
- Produces: a root-file allowlist, public-import smoke coverage, and dependency-boundary enforcement used by every later task.

- [ ] **Step 1: Write the failing root allowlist test**

```python
EXPECTED_ROOT_MODULES = {
    "__init__.py", "__main__.py", "aider_benchmark.py", "benchmark.py",
    "cli.py", "eval_runner.py", "loop.py", "permissions.py", "sdk.py",
    "swebench_lite.py",
}

def test_package_root_contains_only_public_facades():
    root = Path(nz_coder.__file__).parent
    actual = {path.name for path in root.glob("*.py")}
    assert actual == EXPECTED_ROOT_MODULES
```

- [ ] **Step 2: Write public import and AST boundary tests**

```python
@pytest.mark.parametrize("name", PUBLIC_MODULES)
def test_public_module_remains_importable(name):
    assert importlib.import_module(name)

def test_foundation_and_protocol_do_not_import_runtime():
    violations = forbidden_imports(("foundation", "protocol"), "nz_coder.runtime")
    assert violations == []
```

- [ ] **Step 3: Run the new test and verify RED**

Run: `pytest -q tests/test_package_structure.py`

Expected: the root allowlist fails and reports the current internal modules.

### Task 2: Move foundation and protocol modules

**Files:**
- Create: `nz_coder/foundation/__init__.py`
- Move: `nz_coder/config.py` -> `nz_coder/foundation/config.py`
- Move: `nz_coder/async_utils.py` -> `nz_coder/foundation/async_utils.py`
- Move: `nz_coder/json_safety.py` -> `nz_coder/foundation/json_safety.py`
- Move: `nz_coder/private_paths.py` -> `nz_coder/foundation/private_paths.py`
- Create: `nz_coder/protocol/__init__.py`
- Move: `nz_coder/message_schema.py` -> `nz_coder/protocol/message_schema.py`
- Move: `nz_coder/attachments.py` -> `nz_coder/protocol/attachments.py`
- Move: `nz_coder/session_events.py` -> `nz_coder/protocol/session_events.py`
- Modify: repository-owned Python imports under `nz_coder/` and `tests/`.

**Interfaces:**
- Consumes: existing module objects and call signatures unchanged.
- Produces: `nz_coder.foundation.*` and `nz_coder.protocol.*` canonical imports.

- [ ] **Step 1: Move the modules without editing their implementations**

Use exact source-to-destination mappings above and add docstring-only package initializers.

- [ ] **Step 2: Rewrite imports mechanically**

```text
nz_coder.config -> nz_coder.foundation.config
nz_coder.async_utils -> nz_coder.foundation.async_utils
nz_coder.json_safety -> nz_coder.foundation.json_safety
nz_coder.private_paths -> nz_coder.foundation.private_paths
nz_coder.message_schema -> nz_coder.protocol.message_schema
nz_coder.attachments -> nz_coder.protocol.attachments
nz_coder.session_events -> nz_coder.protocol.session_events
```

Also rewrite `from nz_coder import config` to
`from nz_coder.foundation import config`.

- [ ] **Step 3: Run focused verification**

Run: `pytest -q tests/test_package_structure.py tests/test_message_schema.py tests/test_http_service.py tests/test_runtime_context.py tests/test_tool_cancellation_context.py`

Expected: all tests pass except the root allowlist, which remains RED until all root tasks finish.

### Task 3: Move standalone capabilities and domain-owned modules

**Files:**
- Create: `nz_coder/capabilities/__init__.py`
- Move: `documents.py`, `vision.py`, `web_search.py`, `ripgrep.py` into `nz_coder/capabilities/`.
- Create: `nz_coder/interface/setup/__init__.py`
- Move: `doctor.py`, `initializer.py` into `nz_coder/interface/setup/`.
- Move: `reviewer.py` -> `nz_coder/intelligence/reviewer.py`.
- Move: `session_stats.py` -> `nz_coder/state/session_stats.py`.
- Create: `nz_coder/runtime/observability/__init__.py`
- Move: `run_evidence.py` -> `nz_coder/runtime/observability/run_evidence.py`.
- Create: `nz_coder/runtime/verification/__init__.py`
- Move: `verification_evidence.py` -> `nz_coder/runtime/verification/evidence.py`.
- Move: `recovery.py` -> `nz_coder/runtime/verification/recovery.py`.
- Modify: all repository-owned imports and monkeypatch targets for these modules.

**Interfaces:**
- Consumes: Task 2 canonical foundation/protocol paths.
- Produces: capability, setup, intelligence, state, observability, and verification canonical paths.

- [ ] **Step 1: Move each cohesive group and rewrite exact import prefixes**

No callable, class, constant, or tool registration changes are allowed.

- [ ] **Step 2: Update the two existing runtime compatibility imports**

Replace internal uses of `nz_coder.runtime.recovery` and
`nz_coder.runtime.ripgrep` with the new canonical paths, then remove those
obsolete runtime wrappers during Task 5.

- [ ] **Step 3: Run focused tests**

Run: `pytest -q tests/test_doctor.py tests/test_reviewer.py tests/test_run_evidence.py tests/test_recovery.py tests/test_web_search.py tests/test_webfetch.py tests/test_read_image_describe.py tests/test_tool_cancellation_context.py`

Expected: all selected tests pass.

### Task 4: Replace historical root imports with canonical package imports

**Files:**
- Modify: Python files under `nz_coder/` and `tests/` importing root wrappers.

**Interfaces:**
- Consumes: existing canonical modules in `state`, `runtime`, `intelligence`, and `tool_platform`.
- Produces: no repository-owned dependency on removable root wrappers.

- [ ] **Step 1: Apply the canonical mapping**

```text
nz_coder.changes -> nz_coder.state.changes
nz_coder.command_policy -> nz_coder.tool_platform.command_policy
nz_coder.context -> nz_coder.state.context
nz_coder.impact_analyzer -> nz_coder.intelligence.impact_analyzer
nz_coder.memory -> nz_coder.state.memory
nz_coder.project_profile -> nz_coder.intelligence.project_profile
nz_coder.prompt -> nz_coder.runtime.prompt
nz_coder.runtime_state -> nz_coder.runtime.runtime_state
nz_coder.sessions -> nz_coder.state.sessions
nz_coder.skills -> nz_coder.state.skills
nz_coder.subagent -> nz_coder.runtime.subagent
nz_coder.task_policy -> nz_coder.runtime.task_policy
nz_coder.tool_executor -> nz_coder.runtime.tool_executor
nz_coder.trace -> nz_coder.state.trace
nz_coder.transaction -> nz_coder.state.transaction
nz_coder.verification -> nz_coder.intelligence.verification
nz_coder.verification_planner -> nz_coder.intelligence.verification_planner
nz_coder.workspace -> nz_coder.state.workspace
```

- [ ] **Step 2: Verify no repository-owned Python import uses the old paths**

Run one AST-backed architecture test rather than a text-only assertion.

- [ ] **Step 3: Run state, intelligence, tool, and runtime regression tests**

Run: `pytest -q tests/test_changes_undo.py tests/test_context_budget.py tests/test_impact_analyzer.py tests/test_memory.py tests/test_permissions.py tests/test_project_profile.py tests/test_sessions.py tests/test_skills.py tests/test_task_policy.py tests/test_verification.py tests/test_verification_planner.py`

Expected: all selected tests pass.

### Task 5: Remove internal wrappers and scratch code

**Files:**
- Delete: `nz_coder/test.py`.
- Delete: the historical wrapper files covered by Task 4.
- Delete: obsolete `nz_coder/runtime/async_utils.py`, `nz_coder/runtime/recovery.py`, and `nz_coder/runtime/ripgrep.py` wrappers after canonical imports are updated.
- Preserve: all formal public façades in `EXPECTED_ROOT_MODULES`.

**Interfaces:**
- Consumes: Task 4's zero-reference guarantee.
- Produces: the final clean package root.

- [ ] **Step 1: Remove only files proven unreferenced by Task 4**

- [ ] **Step 2: Run the package structure test and verify GREEN**

Run: `pytest -q tests/test_package_structure.py`

Expected: all architecture and public-import tests pass.

- [ ] **Step 3: Run entrypoint and tool-registration regressions**

Run: `pytest -q tests/test_smoke.py tests/test_tool_side_effects.py tests/test_cli_commands.py tests/test_swebench_lite.py tests/test_swebench_strict.py`

Expected: all selected tests pass with no missing or duplicate tools.

### Task 6: Root-migration completion verification

**Files:**
- Modify only defects directly caused by Tasks 1–5.

**Interfaces:**
- Consumes: final root structure.
- Produces: a verified baseline for the runtime-decomposition plan.

- [ ] **Step 1: Compile the package**

Run: `python -m compileall -q nz_coder`

Expected: exit code 0.

- [ ] **Step 2: Run full tests**

Run: `pytest -q`

Expected: no failures and no new skips relative to the pre-migration baseline of `3053 passed, 21 skipped`.

- [ ] **Step 3: Inspect final root and diff**

Run: `find nz_coder -maxdepth 1 -type f -name '*.py' -printf '%f\n' | sort`

Expected: exactly the ten files in `EXPECTED_ROOT_MODULES`.
