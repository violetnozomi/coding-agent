# Tool Policy Continuous Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Continue the InfCodeX/infcode-dev source-level audit by removing the remaining places where NZ-Coder confuses scheduler execution mode, tool capability, side effects, and product observability.

**Architecture:** Keep `execution` responsible only for scheduling and use the existing run-local tool policy metadata for authority, capability, and semantic event classification. Preserve narrow explicit exceptions where a stateful tool is intentionally admitted as interaction/read capability, and fail closed for unknown tools.

**Tech Stack:** Python 3.9+, pytest, standard library, existing NZ-Coder tool registry.

**Spec:** `docs/infcode-alignment-learning-log.md` A293-A294 and InfCodeX `packages/coding/src/tools/{side-effect,registry}.ts`.

## Global Constraints

- Do not add an Agent framework or external dependency.
- Write and observe a failing behavioral test before each production fix.
- Preserve public tool names, schemas, and string handler results.
- Do not run paid Providers, SWE instances, or Docker harnesses.
- Do not create commits; the workspace contains user-owned changes.

---

### Task 1: A295 AgentGraph admission capability projection

**Files:**
- Modify: `nz_coder/runtime/admission.py`
- Test: `tests/test_agent_admission.py`

**Interfaces:**
- Consumes: `get_tool_side_effect(name: str) -> str` and `is_filesystem_mutation_tool(name: str) -> bool`.
- Produces: `resolve_tool_capability(name, tool_input=None) -> str` derived from registered metadata with explicit interaction/subagent exceptions.

- [x] **Step 1: Write the failing registered-tool capability test**

```python
def test_admission_derives_extension_capability_from_side_effect_metadata():
    assert resolve_tool_capability(serial_readonly) == "read"
    assert resolve_tool_capability(serial_network_read) == "bash:network"
    assert resolve_tool_capability(serial_shell) == "bash:mutating"
    assert resolve_tool_capability(serial_filesystem_write) == "edit"
    assert resolve_tool_capability(serial_state_write) == "subagent"
```

- [x] **Step 2: Run the focused test and confirm current static lists misclassify the first three tools as `subagent`**

Run: `pytest -q tests/test_agent_admission.py::test_admission_derives_extension_capability_from_side_effect_metadata`

- [x] **Step 3: Replace generic static read/network decisions with the side-effect projection**

Keep Bash/process command-aware handling first, local filesystem writes as `edit`, known interaction-only state tools as `read`, orchestration tools as `subagent`, network effects as `bash:network`, shell effects as `bash:mutating`, and unknown/state effects fail-closed as `subagent`.

- [x] **Step 4: Run admission and runtime-policy regression tests**

Run: `pytest -q tests/test_agent_admission.py tests/runtime/tool_runtime/test_focused_policy.py tests/test_handoffs.py`

- [x] **Step 5: Inspect the diff without committing**

Run: `git diff --check && git diff -- nz_coder/runtime/admission.py tests/test_agent_admission.py`

### Task 2: A296 semantic tool event categories

**Files:**
- Modify: `nz_coder/runtime/tool_executor.py`
- Modify: `nz_coder/runtime/loop.py`
- Test: `tests/test_session_events.py`

**Interfaces:**
- Consumes: `get_tool_side_effect(name: str) -> str`.
- Produces: one shared category projection used by executor metadata and `session.tool.completed` events.

- [x] **Step 1: Add failing tests for serial readonly tools**

```python
assert category(serial_readonly_tool) == "read"
assert category(reads_network_tool) == "read"
assert category(mutates_shell_tool) == "command"
assert category(mutates_fs_tool) == "edit"
assert category(mutates_state_tool) == "state"
```

- [x] **Step 2: Confirm the current execution-mode implementation reports serial readonly tools as `state`**

Run the exact new tests with `pytest -q` and retain the expected assertion output in the turn evidence.

- [x] **Step 3: Centralize semantic category projection**

Keep Agent orchestration names as `agent`; derive all other categories from side-effect metadata. Remove the duplicate ternary in `loop.py`.

- [x] **Step 4: Run event, executor, and terminal formatting regressions**

Run: `pytest -q tests/test_session_events.py tests/test_observability.py tests/test_run_renderer.py tests/test_terminal_infcode_commands.py tests/test_agent_resilience.py tests/test_permissions.py`

- [x] **Step 5: Inspect the diff without committing**

Run: `git diff --check && git diff -- nz_coder/runtime/tool_executor.py nz_coder/runtime/loop.py`

### Task 3: A297 active/optional policy metadata parity

**Files:**
- Modify: `nz_coder/tools/__init__.py`
- Test: `tests/test_tool_side_effects.py`
- Test: `tests/test_lsp.py`

**Interfaces:**
- Consumes: built-in registry, dynamic tool context, and optional-pack declarations.
- Produces: deterministic metadata queries that distinguish active tools from declared optional tools without treating either as unknown mutation accidentally.

- [x] **Step 1: Add failing parity tests covering unloaded optional reads, loaded overrides, and reserved-name collisions**

Assert that optional declarations retain read/write effects before import, active registrations override declarations after import, and a single policy snapshot contains each active dynamic tool once.

- [x] **Step 2: Run the focused tests and record precedence and ownership failures**

Run: `pytest -q tests/test_tool_side_effects.py tests/test_lsp.py`

- [x] **Step 3: Implement only the evidenced parity/precedence and ownership fixes**

Do not expose unloaded optional schemas to the model; only preserve their declared policy for direct fail-safe queries.

- [x] **Step 4: Run registry, MCP, optional-tool, and Plan visibility regressions**

Run: `pytest -q tests/test_tool_side_effects.py tests/test_mcp.py tests/test_lsp.py tests/test_plan_mode.py tests/tool_platform/test_exposure.py`

- [x] **Step 5: Inspect the diff without committing**

Run: `git diff --check && git diff -- nz_coder/tools/__init__.py tests/test_tool_side_effects.py tests/test_lsp.py`

### Task 4: Documentation and repository-wide verification

**Files:**
- Modify: `docs/swebench-progress.md`
- Modify: `docs/infcode-alignment-learning-log.md`

**Interfaces:**
- Consumes: fresh test and static-check output from Tasks 1-3.
- Produces: A295+ evidence entries that distinguish runtime correctness from unmeasured SWE/provider performance.

- [x] **Step 1: Run focused policy regressions**

Run: `pytest -q tests/test_agent_admission.py tests/test_tool_side_effects.py tests/test_permissions.py tests/test_plan_mode.py tests/test_subagent.py tests/test_mcp.py tests/test_session_events.py`

- [x] **Step 2: Run full repository verification**

Run: `pytest -q`

- [x] **Step 3: Run static gates**

Run: `python -m ruff check nz_coder tests && python -m compileall -q nz_coder && git diff --check`

- [x] **Step 4: Update both development documents with exact evidence**

Record the root cause, reference-source boundary, RED behavior, final test counts, and the fact that no paid Provider/SWE/Docker evaluation was run.

- [x] **Step 5: Re-read this plan and mark only evidence-backed checkboxes complete**

Run: `rg -n "^- \[ \]" docs/superpowers/plans/2026-08-26-tool-policy-continuous-repair.md`
