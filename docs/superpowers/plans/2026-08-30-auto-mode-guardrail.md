# Auto Mode Guardrail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The repository owner requires the primary agent to make every code change; subagents may review but must not edit.

**Goal:** Add a transcript-aware, fail-interactive risk classifier to the local terminal's `auto` permission mode while leaving HTTP, SDK, headless, SWE-bench, and non-Auto modes behaviorally unchanged.

**Architecture:** A pure permission router performs hard-deny, explicit-rule, safe-path, and exact-session-approval decisions before any model call. A session-owned `AutoModeController` uses the existing Provider-neutral Model Gateway only for residual risk, then converts classifier block/failure into an async terminal `once / always / reject` choice. `ProductionGuardrailRuntime.before_tool` orchestrates the built-in policy independently of declared Agent guardrails and preserves `PermissionChecker` plus tool `_safe_path()` as final deterministic enforcement.

**Tech Stack:** Python 3.9+, standard library (`asyncio`, `dataclasses`, `enum`, `hashlib`, `json`, `pathlib`, `time`), existing NZ-Coder Model Gateway/tool registry/permission system, pytest.

**Spec:** `docs/superpowers/specs/2026-08-30-auto-mode-guardrail-design.md`

## Global Constraints

- Do not add an Agent framework or a new external dependency.
- Keep the existing `PermissionManager.check()` return dictionary and public tool schemas backward compatible.
- Enable classification only when all three conditions hold: the environment capability is true, an async Auto approval callback is installed, and permission mode is `auto`.
- Local terminal composition is the sole default-on surface; HTTP, SDK, headless, SWE-bench, background Agents, and compatibility callers default off.
- Classifier block, timeout, Provider failure, and parse failure go to human approval; only deterministic hard rules deny without override.
- Auto-generated `always` is session-scoped and exact-action-bound; it must not create a broad persistent Bash/process/MCP rule.
- Every file path is canonicalized against the workspace before classifier/fast allow and is validated again by the existing tool handler at dispatch.
- All new comments, identifiers, log names, and tool-facing strings are English.
- Catch and normalize expected classifier/projection errors; do not suppress `asyncio.CancelledError` from the owning run.
- Preserve the user's dirty worktree. Stage only the exact task hunks after inspecting the index; if a target file contains inseparable unrelated changes, do not commit that task.

## File Structure

- Create `nz_coder/tool_platform/permissioning/auto_router.py`: pure route selection, local path escape detection, canonical action hashing, and Auto safe-state policy.
- Create `nz_coder/runtime/auto_mode.py`: classifier projection/parsing, session state, circuit breaker, domain outcomes, and controller orchestration.
- Modify `nz_coder/tool_platform/permissioning/manager.py`: expose deterministic explicit-rule provenance without changing `check()`.
- Modify `nz_coder/tool_platform/permissioning/rules.py`: prevent an `argv-prefix` rule from matching shell-composed commands.
- Modify `nz_coder/runtime/model_gateway/models.py`: add the `AUTO_MODE` model-call purpose.
- Modify `nz_coder/config.py`: add bounded Auto classifier rollout, timeout, output, and degradation constants.
- Modify `nz_coder/interface/interactions.py`: expose event-loop-native Auto permission selection.
- Modify `nz_coder/runtime/loop.py`: own one session controller, bind the async approval callback, and build the observer-bound Auto context.
- Modify `nz_coder/interface/cli.py`: enable the capability only in local terminal composition.
- Modify `nz_coder/runtime/guardrail_runtime.py`: run current-Agent declared tool guardrails, then the built-in session Auto controller.
- Create `tests/test_auto_mode_router.py`: deterministic routing, path, digest, and exact approval tests.
- Create `tests/test_auto_mode.py`: prompt privacy, strict parsing, circuit breaker, and controller tests.
- Create `tests/test_auto_mode_guardrail.py`: runtime integration, declared rewrite, result projection, and surface gating tests.
- Modify `tests/test_permissions.py`: explicit-rule provenance and composed-shell prefix regression.
- Modify `tests/test_terminal_interactions.py`: async Auto selector and binding tests.
- Modify `tests/runtime/model_gateway/test_gateway_models.py`: new purpose contract.
- Modify `tests/test_headless_cli.py` and focused SWE tests only to assert zero Auto calls; do not change their production behavior.

---

### Task 1: Add bounded configuration and model-call purpose

**Files:**
- Modify: `nz_coder/config.py`
- Modify: `nz_coder/runtime/model_gateway/models.py`
- Modify: `tests/runtime/model_gateway/test_gateway_models.py`
- Test: `tests/test_auto_mode.py`

**Interfaces:**
- Consumes: existing `config.get()` and `ModelCallPurpose` enum.
- Produces: `ModelCallPurpose.AUTO_MODE`; `AUTO_MODE_CLASSIFIER_ENABLED: bool`; `AUTO_MODE_CLASSIFIER_TIMEOUT_SECONDS: float`; `AUTO_MODE_CLASSIFIER_MAX_OUTPUT_TOKENS: int`; `AUTO_MODE_CLASSIFIER_BLOCK_STREAK: int`; `AUTO_MODE_CLASSIFIER_INFRA_FAILURES: int`; `AUTO_MODE_CLASSIFIER_INFRA_WINDOW_SECONDS: float`.

- [ ] **Step 1: Write failing enum and configuration tests**

Add to `tests/runtime/model_gateway/test_gateway_models.py`:

```python
def test_auto_mode_has_distinct_model_call_purpose() -> None:
    assert ModelCallPurpose.AUTO_MODE.value == "auto_mode"
```

Create `tests/test_auto_mode.py` with the module docstring and this first test:

```python
"""Contracts for interactive Auto-mode classification."""
from __future__ import annotations

from nz_coder import config


def test_auto_mode_classifier_defaults_are_bounded() -> None:
    assert config.AUTO_MODE_CLASSIFIER_ENABLED is True
    assert config.AUTO_MODE_CLASSIFIER_TIMEOUT_SECONDS == 15.0
    assert config.AUTO_MODE_CLASSIFIER_MAX_OUTPUT_TOKENS == 256
    assert config.AUTO_MODE_CLASSIFIER_BLOCK_STREAK == 3
    assert config.AUTO_MODE_CLASSIFIER_INFRA_FAILURES == 5
    assert config.AUTO_MODE_CLASSIFIER_INFRA_WINDOW_SECONDS == 600.0
```

- [ ] **Step 2: Run the tests and verify the missing contracts fail**

Run:

```bash
pytest -q tests/runtime/model_gateway/test_gateway_models.py::test_auto_mode_has_distinct_model_call_purpose tests/test_auto_mode.py::test_auto_mode_classifier_defaults_are_bounded
```

Expected: FAIL because `AUTO_MODE` and the six configuration names do not exist.

- [ ] **Step 3: Add the minimal configuration and enum values**

Add `AUTO_MODE = "auto_mode"` to `ModelCallPurpose`. Add these values next to `PERMISSION_MODE` in `config.py`, using the existing boolean parsing convention:

```python
AUTO_MODE_CLASSIFIER_ENABLED = get(
    "NZ_AUTO_MODE_CLASSIFIER_ENABLED", "1"
).lower() in ("1", "true", "yes", "on")
AUTO_MODE_CLASSIFIER_TIMEOUT_SECONDS = max(
    1.0, float(get("NZ_AUTO_MODE_CLASSIFIER_TIMEOUT_SECONDS", "15"))
)
AUTO_MODE_CLASSIFIER_MAX_OUTPUT_TOKENS = max(
    64, int(get("NZ_AUTO_MODE_CLASSIFIER_MAX_OUTPUT_TOKENS", "256"))
)
AUTO_MODE_CLASSIFIER_BLOCK_STREAK = max(
    1, int(get("NZ_AUTO_MODE_CLASSIFIER_BLOCK_STREAK", "3"))
)
AUTO_MODE_CLASSIFIER_INFRA_FAILURES = max(
    1, int(get("NZ_AUTO_MODE_CLASSIFIER_INFRA_FAILURES", "5"))
)
AUTO_MODE_CLASSIFIER_INFRA_WINDOW_SECONDS = max(
    1.0, float(get("NZ_AUTO_MODE_CLASSIFIER_INFRA_WINDOW_SECONDS", "600"))
)
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
pytest -q tests/runtime/model_gateway/test_gateway_models.py tests/test_auto_mode.py::test_auto_mode_classifier_defaults_are_bounded
```

Expected: PASS.

- [ ] **Step 5: Commit the exact task hunks**

```bash
git add -p nz_coder/config.py nz_coder/runtime/model_gateway/models.py tests/runtime/model_gateway/test_gateway_models.py tests/test_auto_mode.py
git diff --cached --check
git commit -m "feat: define auto mode classifier bounds"
```

### Task 2: Implement deterministic Auto routing and narrow rule provenance

**Files:**
- Create: `nz_coder/tool_platform/permissioning/auto_router.py`
- Modify: `nz_coder/tool_platform/permissioning/manager.py`
- Modify: `nz_coder/tool_platform/permissioning/rules.py`
- Create: `tests/test_auto_mode_router.py`
- Modify: `tests/test_permissions.py`

**Interfaces:**
- Consumes: `PermissionManager.check()`, `first_matching_rule()`, `READ_TOOLS`, `get_tool_side_effect()`, `is_filesystem_mutation_tool()`, `is_transactional_dynamic_tool()`, and `collect_filesystem_mutation_paths()`.
- Produces: `AutoRouteKind`, immutable `AutoRoute`, `canonical_action_digest(tool_name: str, tool_input: dict, workspace: Path) -> str`, and `route_auto_action(tool_name: str, tool_input: dict, *, workspace: Path, permission_decision: dict, explicit_behavior: str | None, approved_digests: set[str] | frozenset[str]) -> AutoRoute`; `PermissionManager.explicit_rule_behavior(tool_name: str, tool_input: dict) -> str | None`.

- [ ] **Step 1: Write failing route matrix tests**

Create `tests/test_auto_mode_router.py` with table-driven cases:

```python
"""Deterministic admission routes used before the Auto classifier."""
from __future__ import annotations

import os

from nz_coder.tool_platform.permissioning.auto_router import (
    AutoRouteKind,
    canonical_action_digest,
    route_auto_action,
)


def _route(tmp_path, name: str, tool_input: dict, *, decision=None, explicit=None, approved=None):
    return route_auto_action(
        name,
        tool_input,
        workspace=tmp_path,
        permission_decision=decision or {"behavior": "allow", "reason": "Auto mode"},
        explicit_behavior=explicit,
        approved_digests=set(approved or ()),
    )


def test_router_hard_denies_before_explicit_or_session_allow(tmp_path) -> None:
    escaped = {"path": "../outside.py", "content": "x"}
    digest = canonical_action_digest("write_file", escaped, tmp_path)
    route = _route(
        tmp_path,
        "write_file",
        escaped,
        explicit="allow",
        approved={digest},
    )
    assert route.kind is AutoRouteKind.HARD_DENY
    assert route.reason_code == "workspace_escape"

    shell = _route(
        tmp_path,
        "bash",
        {"command": "rm -rf /"},
        decision={"behavior": "deny", "reason": "Blocked: recursive root delete"},
        explicit="allow",
    )
    assert shell.kind is AutoRouteKind.HARD_DENY


def test_router_fast_allows_local_read_and_transactional_edit(tmp_path) -> None:
    assert _route(tmp_path, "read_file", {"path": "app.py"}).kind is AutoRouteKind.FAST_ALLOW
    assert _route(tmp_path, "edit_file", {
        "path": "app.py", "old_text": "a", "new_text": "b",
    }).kind is AutoRouteKind.FAST_ALLOW


def test_router_classifies_shell_network_mcp_agent_and_unknown(tmp_path) -> None:
    cases = [
        ("bash", {"command": "git status"}),
        ("process", {"operation": "write", "process_id": "p1", "data": "x"}),
        ("mcp_demo_read", {"query": "status"}),
        ("webfetch", {"url": "https://example.test"}),
        ("task", {"prompt": "inspect"}),
        ("unregistered_tool", {}),
    ]
    assert all(
        _route(tmp_path, name, payload).kind is AutoRouteKind.CLASSIFY
        for name, payload in cases
    )


def test_router_honors_explicit_ask_before_fast_path(tmp_path) -> None:
    route = _route(tmp_path, "read_file", {"path": "app.py"}, explicit="ask")
    assert route.kind is AutoRouteKind.MANUAL


def test_session_approval_is_exact_to_action_and_workspace(tmp_path) -> None:
    action = {"command": "git status"}
    digest = canonical_action_digest("bash", action, tmp_path)
    assert _route(tmp_path, "bash", action, approved={digest}).kind is AutoRouteKind.FAST_ALLOW
    assert _route(tmp_path, "bash", {"command": "git status --short"}, approved={digest}).kind is AutoRouteKind.CLASSIFY


def test_router_rejects_nested_batch_and_patch_escape(tmp_path) -> None:
    batch = _route(tmp_path, "write_files_batch", {
        "files": [{"path": "ok.py", "content": "ok"}, {"path": "../bad.py", "content": "bad"}],
    })
    patch = _route(tmp_path, "apply_patch", {
        "changes": [{"op": "create", "path": "../bad.py", "content": "bad"}],
    })
    assert batch.kind is AutoRouteKind.HARD_DENY
    assert patch.kind is AutoRouteKind.HARD_DENY


def test_router_rejects_symlink_windows_drive_and_unc_escape(tmp_path) -> None:
    inputs = [
        {"path": "C:\\outside\\escaped.py", "content": "x"},
        {"path": "\\\\server\\share\\escaped.py", "content": "x"},
    ]
    if os.name != "nt":
        outside = tmp_path.parent / "outside"
        outside.mkdir(exist_ok=True)
        (tmp_path / "link").symlink_to(outside, target_is_directory=True)
        inputs.append({"path": "link/escaped.py", "content": "x"})
    assert all(
        _route(tmp_path, "write_file", tool_input).kind is AutoRouteKind.HARD_DENY
        for tool_input in inputs
    )
```

- [ ] **Step 2: Write failing explicit-rule and shell-composition tests**

Add to `tests/test_permissions.py`:

```python
def test_permission_manager_reports_matching_explicit_rule_source() -> None:
    pm = PermissionManager("auto")
    pm._allow_rules = [PermissionRule("bash", "allow", "prefix:git ")]
    pm._ask_rules = [PermissionRule("read_file", "ask")]

    assert pm.explicit_rule_behavior("bash", {"command": "git status"}) == "allow"
    assert pm.explicit_rule_behavior("read_file", {"path": "app.py"}) == "ask"
    assert pm.explicit_rule_behavior("todo", {}) is None


def test_argv_prefix_rule_rejects_shell_composition() -> None:
    rule = scoped_allow_rule("bash", {"command": "git status"})
    assert rule.matches("bash", {"command": "git status --short"}) is True
    for command in (
        "git status ; rm app.py",
        "git status && rm app.py",
        "git status | tee leaked.txt",
        "git status\nrm app.py",
        "git status $(touch changed.txt)",
        "git status `touch changed.txt`",
    ):
        assert rule.matches("bash", {"command": command}) is False
```

Import `scoped_allow_rule` from the implementation module in the test.

- [ ] **Step 3: Run the focused tests and verify failure**

Run:

```bash
pytest -q tests/test_auto_mode_router.py tests/test_permissions.py::test_permission_manager_reports_matching_explicit_rule_source tests/test_permissions.py::test_argv_prefix_rule_rejects_shell_composition
```

Expected: collection/import failure for the new router and method, plus the composed-command matcher test fails.

- [ ] **Step 4: Implement the pure router and explicit provenance**

Use these domain types in `auto_router.py`:

```python
class AutoRouteKind(str, Enum):
    HARD_DENY = "hard_deny"
    MANUAL = "manual"
    FAST_ALLOW = "fast_allow"
    CLASSIFY = "classify"


@dataclass(frozen=True)
class AutoRoute:
    kind: AutoRouteKind
    reason_code: str
    reason: str
    action_digest: str
```

Canonicalize the digest with:

```python
payload = json.dumps(
    {"tool": normalized_name, "input": tool_input, "workspace": str(workspace.resolve())},
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
)
return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

Define an explicit safe-state set containing only:

```python
AUTO_SAFE_STATE_TOOLS = frozenset({
    "compact", "load_optional_tools", "load_skill", "plan_enter", "plan_exit",
    "plan_verification", "question", "read_scratchpad", "recall_memory",
    "todo", "verify_changed_files", "verify_project_build", "write_plan",
})
```

Route in this exact precedence: local file path escape; `permission_decision == deny`; explicit ask; explicit allow; exact approved digest; Bash/process/MCP/network/Agent-event classification; local `readonly`; explicit safe state; transactional local filesystem mutation; residual classification.

Implement `PermissionManager.explicit_rule_behavior()` with `deny -> allow -> ask` matching and no mode fallback. The router still consumes `PermissionManager.check()` first, so shell/path hard denial remains authoritative over an allow rule.

- [ ] **Step 5: Harden `argv-prefix` matching**

Before the existing `shlex.split()` prefix comparison, reject any candidate command containing a composition construct outside the safe reusable contract:

```python
def _has_shell_composition(command: str) -> bool:
    return bool(re.search(r"(?:&&|\|\||[;&|\n`<>]|\$\(|\)\s*(?:$|[;&|]))", command))
```

Apply this guard only to `argv-prefix` rules. Existing `family:pytest` remains governed by `_bash_command_family()` and its narrow command tokens.

- [ ] **Step 6: Run router and permission tests**

Run:

```bash
pytest -q tests/test_auto_mode_router.py tests/test_permissions.py tests/test_terminal_interactions.py
```

Expected: PASS.

- [ ] **Step 7: Commit the exact task hunks**

```bash
git add -p nz_coder/tool_platform/permissioning/auto_router.py nz_coder/tool_platform/permissioning/manager.py nz_coder/tool_platform/permissioning/rules.py tests/test_auto_mode_router.py tests/test_permissions.py
git diff --cached --check
git commit -m "feat: route auto mode actions deterministically"
```

### Task 3: Implement strict classifier projection and degradation state

**Files:**
- Create: `nz_coder/runtime/auto_mode.py`
- Modify: `tests/test_auto_mode.py`

**Interfaces:**
- Consumes: Task 1 configuration and `ModelCallPurpose.AUTO_MODE`; Task 2 `AutoRoute`, `AutoRouteKind`, and `route_auto_action()`.
- Produces: immutable `AutoClassifierDecision`, mutable `AutoModeState`, immutable `AutoModeContext`, immutable `AutoAdmission`, `parse_tool_arguments(raw: object) -> dict | None`, `AutoModeController(enabled: bool, state: AutoModeState | None = None)`, and `AutoModeController.admit(context: AutoModeContext, tool_name: str, tool_input: dict, messages: list[dict]) -> AutoAdmission`.

- [ ] **Step 1: Add failing parser and privacy tests**

Extend `tests/test_auto_mode.py`:

```python
import asyncio
import json

from nz_coder.runtime.auto_mode import (
    AutoModeController,
    AutoModeContext,
    AutoModeState,
    parse_tool_arguments,
)
from nz_coder.runtime.model_gateway import ModelCallOutcome
from nz_coder.runtime.model_gateway import ModelCallPurpose
from nz_coder.permissions import PermissionManager


async def _never_approve(_name, _tool_input, _details):
    raise AssertionError("approval must not be requested")


def _context(tmp_path, *, complete, approve, clock=lambda: 0.0, traces=None):
    events = [] if traces is None else traces
    return AutoModeContext(
        permissions=PermissionManager("auto"),
        workspace=tmp_path,
        complete=complete,
        approve=approve,
        trace=lambda event, **payload: events.append((event, payload)),
        clock=clock,
    )


def test_parse_tool_arguments_accepts_only_json_objects() -> None:
    assert parse_tool_arguments({"path": "app.py"}) == {"path": "app.py"}
    assert parse_tool_arguments('{"path":"app.py"}') == {"path": "app.py"}
    assert parse_tool_arguments("[") is None
    assert parse_tool_arguments("[]") is None
    assert parse_tool_arguments(None) is None


def test_classifier_prompt_excludes_reasoning_tool_output_secrets_and_write_content(tmp_path) -> None:
    seen = []

    async def complete(call):
        seen.append(call)
        return ModelCallOutcome.completed(
            content='{"decision":"allow","reason_code":"bounded","reason":"ok"}'
        )

    context = _context(tmp_path, complete=complete, approve=_never_approve)
    messages = [
        {"role": "user", "content": "Fix the parser"},
        {"role": "assistant", "content": "SECRET_REASONING"},
        {"role": "tool", "content": "Authorization: Bearer secret-token"},
    ]
    admission = asyncio.run(AutoModeController(True).admit(
        context,
        "bash",
        {"command": "python build.py", "content": "PRIVATE_FILE_BODY"},
        messages,
    ))

    wire = json.dumps(seen[0].messages, ensure_ascii=False)
    assert admission.allowed is True
    assert "Fix the parser" in wire
    assert "SECRET_REASONING" not in wire
    assert "secret-token" not in wire
    assert "PRIVATE_FILE_BODY" not in wire
    assert seen[0].purpose is ModelCallPurpose.AUTO_MODE
    assert seen[0].tools == ()
    assert seen[0].streaming is False
```

Define `_context()` and `_never_approve()` as real test helpers in the same file. `_context()` constructs `PermissionManager("auto")`, an empty trace collector, workspace, completion callback, approval callback, and a deterministic clock.

- [ ] **Step 2: Add failing decision, fallback, and circuit-breaker tests**

Cover these exact cases with async fakes:

```python
def test_classifier_block_uses_human_once_instead_of_hard_deny(tmp_path) -> None:
    async def complete(_call):
        return ModelCallOutcome.completed(
            content='{"decision":"block","reason_code":"shell","reason":"may mutate"}'
        )

    async def approve(_name, _tool_input, details):
        assert details["reason"] == "may mutate"
        return "once"

    admission = asyncio.run(AutoModeController(True).admit(
        _context(tmp_path, complete=complete, approve=approve),
        "bash", {"command": "python build.py"}, [{"role": "user", "content": "build"}],
    ))
    assert admission.allowed is True
    assert admission.source == "human"


def test_classifier_parse_failure_uses_human_reject(tmp_path) -> None:
    async def complete(_call):
        return ModelCallOutcome.completed(content="not-json")

    async def reject(_name, _tool_input, details):
        assert details["classifier_status"] == "parse_error"
        return "reject"

    admission = asyncio.run(AutoModeController(True).admit(
        _context(tmp_path, complete=complete, approve=reject),
        "bash", {"command": "python build.py"}, [],
    ))
    assert admission.allowed is False
    assert admission.permission_denied is True
```

Add boundary tests where three completed blocks set `state.degraded`, five logical errors at monotonic times `0, 100, 200, 300, 599` set degraded, an error at `601` evicts the timestamp at `0`, completed allow resets `consecutive_blocks`, and degraded residual calls invoke approval without calling `complete`.

Add one `always` test that approves `bash {"command": "python build.py"}`, then proves the identical action skips both Provider and UI while `python build.py --clean` is classified again. Add one trace test that searches serialized trace payloads and proves they contain only the digest prefix/reason code, never the raw command, user text, classifier prose, or credential-shaped fields.

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
pytest -q tests/test_auto_mode.py
```

Expected: FAIL because `nz_coder.runtime.auto_mode` does not exist.

- [ ] **Step 4: Implement domain models and strict projection**

Use these stable signatures:

```python
@dataclass(frozen=True)
class AutoModeContext:
    permissions: PermissionManager
    workspace: Path
    complete: Callable[[ModelCall], Awaitable[ModelCallOutcome]]
    approve: Callable[[str, dict, dict], Awaitable[str]]
    trace: Callable[..., None]
    clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class AutoAdmission:
    allowed: bool
    permission_denied: bool
    source: str
    reason: str
    reason_code: str
    action_digest: str
    classifier_status: str = "skipped"
```

Bound intent to the first and latest real user messages with a combined maximum of 12,000 characters. Exclude assistant/tool roles entirely. Recursively remove or summarize `content`, `old_text`, `new_text`, `data`, authorization/cookie/password/secret/token/API-key fields; cap strings at 2,000 characters, collections at 50 items, nesting at six levels, and the final JSON payload at 20,000 characters with an explicit `truncated` boolean.

Build the call exactly as:

```python
ModelCall(
    purpose=ModelCallPurpose.AUTO_MODE,
    messages=(
        {"role": "system", "content": AUTO_MODE_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, allow_nan=False)},
    ),
    tools=(),
    max_output_tokens=config.AUTO_MODE_CLASSIFIER_MAX_OUTPUT_TOKENS,
    streaming=False,
    timeout_seconds=config.AUTO_MODE_CLASSIFIER_TIMEOUT_SECONDS,
    response_format={"type": "json_object"},
    metadata={"allow_response_format_fallback": True},
)
```

Parse with one `json.loads()` call. Require `decision in {"allow", "block"}`, a regex-bounded `reason_code` of at most 80 characters, `reason` of at most 500 characters, no tool calls, and `ModelCallStatus.COMPLETED`. All other completed outcomes are `parse_error`; non-completed outcomes are `unavailable`. Let owning-task `asyncio.CancelledError` propagate.

- [ ] **Step 5: Implement state and controller orchestration**

`AutoModeState` owns `approved_actions: set[str]`, `consecutive_blocks`, `infrastructure_failures: deque[float]`, `degraded`, and `degraded_reason`. It exposes `approve_action()`, `observe_allow()`, `observe_block(limit)`, and `observe_failure(now, limit, window_seconds)`.

`AutoModeController.admit()` obtains `permission_decision` plus `explicit_rule_behavior()`, routes through Task 2, skips Provider in hard/manual/fast/degraded cases, records exact approval only for Auto `always`, and emits one bounded `auto_mode_decision` trace. Never trace raw tool input, prompt, command, write content, or classifier prose; trace only tool name, decision/source/status, reason code, counters, and digest prefix.

- [ ] **Step 6: Run classifier/state tests**

Run:

```bash
pytest -q tests/test_auto_mode.py tests/test_auto_mode_router.py tests/runtime/model_gateway/test_gateway_models.py
```

Expected: PASS.

- [ ] **Step 7: Commit the exact task hunks**

```bash
git add -p nz_coder/runtime/auto_mode.py tests/test_auto_mode.py
git diff --cached --check
git commit -m "feat: classify residual auto mode risk"
```

### Task 4: Add event-loop-native terminal approval and session composition

**Files:**
- Modify: `nz_coder/interface/interactions.py`
- Modify: `nz_coder/runtime/loop.py`
- Modify: `nz_coder/interface/cli.py`
- Modify: `tests/test_terminal_interactions.py`
- Create: `tests/test_auto_mode_guardrail.py`

**Interfaces:**
- Consumes: Task 3 `AutoModeController` and `AutoModeContext`.
- Produces: `TerminalInteractionBridge.ask_auto_permission(tool_name: str, tool_input: dict, details: dict) -> str` async method; `ProductRunEnvironment(auto_mode_classifier_enabled: bool = False)`; `ProductRunEnvironment._auto_mode_context() -> AutoModeContext | None`; `set_interaction_askers(..., auto_permission_asker=None)`.

- [ ] **Step 1: Write failing async terminal selector tests**

Add to `tests/test_terminal_interactions.py`:

```python
def test_auto_permission_bridge_runs_on_event_loop_and_shows_exact_scope() -> None:
    async def scenario():
        terminal = FakeTerminalInput(["always"])
        renderer = FakeRenderer()
        bridge = TerminalInteractionBridge(terminal, renderer, asyncio.get_running_loop())
        result = await bridge.ask_auto_permission(
            "bash",
            {"command": "python build.py"},
            {"reason": "May change dependencies", "degraded": False},
        )
        return result, terminal, renderer

    result, terminal, renderer = asyncio.run(scenario())
    assert result == "always"
    assert terminal.requests[0]["values"][1] == (
        "always", "Always allow this exact action in this session",
    )
    assert "May change dependencies" in terminal.requests[0]["text"]
    assert renderer.paused == renderer.resumed == 1
```

Extend `test_bind_terminal_interactions_updates_both_agent_askers` to assert `auto_permission_asker.__self__ is bridge`. Add Escape/`None` returning `reject` and degraded copy displayed in the selector.

- [ ] **Step 2: Write failing product-environment composition tests**

Start `tests/test_auto_mode_guardrail.py` with:

```python
"""Production integration for the interactive Auto-mode guardrail."""
from __future__ import annotations

import inspect

from nz_coder.runtime.loop import ProductRunEnvironment


def test_product_environment_auto_classifier_defaults_off() -> None:
    parameter = inspect.signature(ProductRunEnvironment.__init__).parameters[
        "auto_mode_classifier_enabled"
    ]
    assert parameter.default is False
```

This signature check avoids constructing a live Provider. Add a CLI unit test by monkeypatching `build_product_environment` and asserting `_build_agent()` passes `auto_mode_classifier_enabled=config.AUTO_MODE_CLASSIFIER_ENABLED`.

```python
def test_cli_build_agent_enables_auto_classifier_only_at_terminal_composition(monkeypatch):
    from types import SimpleNamespace

    from nz_coder import config
    from nz_coder.interface import cli

    captured = {}
    sentinel = object()
    monkeypatch.setattr(
        "nz_coder.providers.models.active_model_selection",
        lambda: SimpleNamespace(provider="fake"),
    )
    monkeypatch.setattr(
        "nz_coder.providers.configuration.provider_connection",
        lambda _provider: SimpleNamespace(provider="fake", configured=True),
    )

    def build(system_prompt, **kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("nz_coder.runtime.composition.build_product_environment", build)
    assert cli._build_agent("system", object(), "session-a") is sentinel
    assert captured["auto_mode_classifier_enabled"] is config.AUTO_MODE_CLASSIFIER_ENABLED
```

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
pytest -q tests/test_terminal_interactions.py tests/test_auto_mode_guardrail.py
```

Expected: FAIL because the async callback, environment capability, and CLI keyword are absent.

- [ ] **Step 4: Implement terminal selector and callback binding**

Expose a public coroutine instead of calling the existing synchronous bridge from `before_tool`:

```python
async def ask_auto_permission(
    self,
    tool_name: str,
    tool_input: dict,
    details: dict,
) -> str:
    summary = format_tool_summary(tool_name, tool_input)
    reason = str(details.get("reason") or "Classifier unavailable")[:500]
    degraded = bool(details.get("degraded"))
    result = await self._select(
        title="Auto mode permission required",
        text=(
            f"{summary}\nReason: {reason}\n"
            + ("Auto classifier is degraded; rules and human approval are active.\n" if degraded else "")
            + "Choose a decision for this exact action. Esc rejects."
        ),
        values=[
            ("once", "Allow once"),
            ("always", "Always allow this exact action in this session"),
            ("reject", "Reject"),
        ],
    )
    return str(result) if result in {"once", "always", "reject"} else "reject"
```

Pass this method through `bind_terminal_interactions()` and the optional `auto_permission_asker` parameter on `set_interaction_askers()`.

- [ ] **Step 5: Implement session ownership and observer-bound context**

Add the optional boolean constructor parameter, validate it is a real bool, create `self.auto_mode_controller = AutoModeController(enabled=value)`, initialize `self.auto_permission_asker = None`, and implement:

```python
def _auto_mode_context(self) -> AutoModeContext | None:
    if (
        not self.auto_mode_controller.enabled
        or self.permissions.mode != "auto"
        or not callable(self.auto_permission_asker)
    ):
        return None
    gateway = self._gateway(max_retries=0)
    return AutoModeContext(
        permissions=self.permissions,
        workspace=Path(self.workdir),
        complete=gateway.complete,
        approve=self.auto_permission_asker,
        trace=self.tracer.log,
    )
```

The callback uses the observer-bound gateway, so `_model_gateway_observer` performs exact-once purpose, usage, cost, and active-run ledger accounting. Do not manually add usage in `auto_mode.py`.

In `interface/cli.py::_build_agent`, pass `auto_mode_classifier_enabled=config.AUTO_MODE_CLASSIFIER_ENABLED`. Do not pass it from `native_sdk.py`, HTTP, headless, SWE, or background Agent composition.

- [ ] **Step 6: Run terminal/composition tests**

Run:

```bash
pytest -q tests/test_terminal_interactions.py tests/test_auto_mode_guardrail.py tests/test_cli_commands.py
```

Expected: PASS.

- [ ] **Step 7: Commit the exact task hunks**

```bash
git add -p nz_coder/interface/interactions.py nz_coder/runtime/loop.py nz_coder/interface/cli.py tests/test_terminal_interactions.py tests/test_auto_mode_guardrail.py
git diff --cached --check
git commit -m "feat: bind terminal auto mode approval"
```

### Task 5: Integrate Auto admission into the production tool guardrail boundary

**Files:**
- Modify: `nz_coder/runtime/guardrail_runtime.py`
- Modify: `tests/test_auto_mode_guardrail.py`
- Modify: `tests/test_handoffs.py`

**Interfaces:**
- Consumes: `ProductRunEnvironment._auto_mode_context()`, `AutoModeController.admit()`, and `parse_tool_arguments()`.
- Produces: Auto admission for coding runtime with no `agent_graph`; current-Agent selection for declared tool guardrails; recoverable permission-denied `ToolExecutionResult` projection.

- [ ] **Step 1: Write failing no-graph and recovery tests**

Use a small fake host/controller in `tests/test_auto_mode_guardrail.py`:

```python
import asyncio
from types import SimpleNamespace

from nz_coder.runtime.auto_mode import AutoAdmission
from nz_coder.runtime.guardrail_runtime import ProductionGuardrailRuntime


class _GuardrailHost:
    def __init__(self, *, agent_graph, controller):
        self.agent_graph = agent_graph
        self.auto_mode_controller = controller
        self.current_agent_name = "worker"
        self.tracer = SimpleNamespace(log=lambda *_args, **_kwargs: None)

    def _auto_mode_context(self):
        return object()


class _FixedController:
    def __init__(self, admission):
        self.admission = admission

    async def admit(self, _context, _tool_name, _tool_input, _messages):
        return self.admission


def test_guardrail_runs_session_auto_policy_without_agent_graph() -> None:
    calls = []

    class Controller:
        async def admit(self, context, tool_name, tool_input, messages):
            calls.append((context, tool_name, tool_input, messages))
            return AutoAdmission(
                allowed=True,
                permission_denied=False,
                source="classifier",
                reason="safe",
                reason_code="safe",
                action_digest="a" * 64,
                classifier_status="completed",
            )

    host = _GuardrailHost(agent_graph=None, controller=Controller())
    guarded, rejected = asyncio.run(ProductionGuardrailRuntime().before_tool(
        host,
        {"id": "call-1", "function": {"name": "bash", "arguments": '{"command":"git status"}'}},
        [{"role": "user", "content": "inspect"}],
    ))
    assert rejected is None
    assert guarded["id"] == "call-1"
    assert calls[0][1:3] == ("bash", {"command": "git status"})


def test_guardrail_projects_user_reject_as_permission_denied_result() -> None:
    admission = AutoAdmission(
        allowed=False,
        permission_denied=True,
        source="human",
        reason="Rejected by user",
        reason_code="user_reject",
        action_digest="b" * 64,
        classifier_status="completed",
    )
    host = _GuardrailHost(agent_graph=None, controller=_FixedController(admission))
    _, rejected = asyncio.run(ProductionGuardrailRuntime().before_tool(
        host,
        {"id": "call-2", "function": {"name": "bash", "arguments": {"command": "python build.py"}}},
        [],
    ))
    assert rejected.executed is False
    assert rejected.dispatch_failed is True
    assert rejected.permission_denied is True
    assert rejected.output.startswith("Denied:")
```

Add a test proving malformed JSON skips the classifier and reaches the existing ToolExecutor invalid-argument error path.

Add a batch-order test with two residual calls whose classifier fake tracks active invocations. Assert `max_active == 1`, both calls settle once, and the existing scheduler still receives both admitted calls. Add a synchronous compatibility test with the capability disabled and assert `dispatch_sync()` makes zero classifier calls.

- [ ] **Step 2: Write failing current-Agent declared guardrail test**

Add a focused test to `tests/test_handoffs.py` with a graph whose start Agent and current Agent have distinct tool guardrails. Set `host.current_agent_name` to the second Agent and assert only the second guardrail observes the call. Keep existing input/output start-Agent behavior assertions unchanged.

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
pytest -q tests/test_auto_mode_guardrail.py tests/test_handoffs.py -k 'guardrail or auto_mode'
```

Expected: FAIL because `before_tool()` returns early when no declared guardrails and `_selected()` uses `graph.start`.

- [ ] **Step 4: Restructure `before_tool()` without changing declared verdict semantics**

Remove the early return when `_selected(host, "tool")` is empty. Always copy the call, run declared current-Agent guardrails first, and then look up:

```python
controller = getattr(host, "auto_mode_controller", None)
context_factory = getattr(host, "_auto_mode_context", None)
context = context_factory() if callable(context_factory) else None
if controller is None or context is None:
    return current, None
```

Parse the rewritten arguments with `parse_tool_arguments()`. If parsing fails, return `current, None` so the stable executor emits its existing JSON diagnostic. Otherwise await `controller.admit(...)`.

For a denied admission, construct:

```python
ToolExecutionResult(
    name=name,
    tool_input=tool_input,
    output=f"Denied: {admission.reason}",
    executed=False,
    dispatch_failed=True,
    command_failed=False,
    is_write=is_transactional_write_tool(name),
    permission_denied=True,
    metadata={
        "guardrail": "auto_mode",
        "agent": host.current_agent_name,
        "source": admission.source,
        "reason_code": admission.reason_code,
        "action_digest": admission.action_digest,
    },
)
```

Do not use generic `escalate` for Auto.

- [ ] **Step 5: Select declared tool guardrails from the current Agent**

Change `_selected()` so `kind == "tool"` resolves `host.current_agent_name` when it names a graph Agent; otherwise use `graph.start`. Input/output continue to use `graph.start`. Add no second policy registry: the session Auto controller is invoked independently after the declared loop.

- [ ] **Step 6: Run integration tests**

Run:

```bash
pytest -q tests/test_auto_mode_guardrail.py tests/test_handoffs.py tests/runtime/tool_runtime/test_focused_policy.py
```

Expected: PASS.

- [ ] **Step 7: Commit the exact task hunks**

```bash
git add -p nz_coder/runtime/guardrail_runtime.py tests/test_auto_mode_guardrail.py tests/test_handoffs.py
git diff --cached --check
git commit -m "feat: enforce interactive auto admission"
```

### Task 6: Lock down surface compatibility, accounting, and cancellation

**Files:**
- Modify: `tests/test_auto_mode_guardrail.py`
- Modify: `tests/test_headless_cli.py`
- Modify: `tests/test_swebench_strict.py`
- Modify: `tests/test_loop_fake.py`

**Interfaces:**
- Consumes: completed Tasks 1–5.
- Produces: regression evidence that only local interactive Auto calls the classifier and that `AUTO_MODE` accounting appears once.

- [ ] **Step 1: Add failing surface-gating assertions**

Add tests that construct or intercept each surface composition:

```python
def test_auto_mode_context_requires_capability_mode_and_async_approval(tmp_path) -> None:
    environment = ProductRunEnvironment.__new__(ProductRunEnvironment)
    environment.auto_mode_controller = SimpleNamespace(enabled=False)
    environment.permissions = SimpleNamespace(mode="auto")
    environment.auto_permission_asker = _approve_once
    environment.workdir = tmp_path
    assert ProductRunEnvironment._auto_mode_context(environment) is None

    environment.auto_mode_controller.enabled = True
    environment.permissions.mode = "default"
    assert ProductRunEnvironment._auto_mode_context(environment) is None

    environment.permissions.mode = "auto"
    environment.auto_permission_asker = None
    assert ProductRunEnvironment._auto_mode_context(environment) is None
```

Import `SimpleNamespace` from `types` and define `_approve_once` as an async test helper returning `"once"`. Add parameterized assertions for terminal permission modes `default`, `plan`, and `acceptEdits`; all must return no Auto context and issue zero classifier calls even when the environment capability and callback exist.

In `tests/test_headless_cli.py`, capture the `RunRequest` and assert its metadata has no truthy `auto_mode_classifier_enabled` even for `--permission-mode auto`. In `tests/test_swebench_strict.py`, intercept normal and retry Agent construction and assert the new capability is absent/false. No test should mutate the production headless or SWE request schema merely to carry a false value.

- [ ] **Step 2: Add exact-once purpose accounting test**

Extend an existing `tests/test_loop_fake.py` Gateway observer test with one synthetic `model_call_finish` event:

```python
agent._model_gateway_observer("model_call_finish", {
    "purpose": "auto_mode",
    "usage": {
        "input": 12, "output": 3, "reasoning": 0,
        "cache_read": 0, "cache_write": 0,
    },
    "attempts": 1,
    "duration_ms": 7.5,
    "provider_id": "fake",
    "model_id": "judge",
    "cost": 0.001,
    "cost_source": "estimated",
})
assert agent.runtime_state.provider_calls_by_purpose["auto_mode"] == 1
assert agent.runtime_state.provider_usage_by_purpose["auto_mode"]["input"] == 12
```

Use an active fake `RunContext` from the existing fixture and assert its total usage grows once, not twice.

- [ ] **Step 3: Add cancellation tests**

Create an async completion fake that waits on an event, start `controller.admit()`, cancel the owning task, release the fake in its cancellation cleanup, and assert `asyncio.CancelledError` propagates, the approval callback is not invoked, and no session digest is stored. Add a terminal approval cancellation test asserting renderer resume executes in `finally`.

- [ ] **Step 4: Run compatibility and accounting tests**

Run:

```bash
pytest -q tests/test_auto_mode.py tests/test_auto_mode_guardrail.py tests/test_headless_cli.py tests/test_swebench_strict.py tests/test_loop_fake.py -k 'auto_mode or provider_usage_by_purpose or permission_mode'
```

Expected: PASS with zero Auto Provider calls on disabled surfaces and one purpose-accounted call on the enabled fake terminal path.

- [ ] **Step 5: Run the complete focused product slice**

Run:

```bash
pytest -q tests/test_permissions.py tests/test_terminal_interactions.py tests/test_auto_mode_router.py tests/test_auto_mode.py tests/test_auto_mode_guardrail.py tests/test_handoffs.py tests/test_headless_cli.py tests/test_swebench_strict.py tests/test_mcp.py tests/test_loop_fake.py
```

Expected: PASS.

- [ ] **Step 6: Commit the exact test hunks**

```bash
git add -p tests/test_auto_mode.py tests/test_auto_mode_guardrail.py tests/test_headless_cli.py tests/test_swebench_strict.py tests/test_loop_fake.py
git diff --cached --check
git commit -m "test: prove auto mode surface isolation"
```

### Task 7: Verify the complete product change

**Files:**
- Verify: all files listed in this plan

**Interfaces:**
- Consumes: completed implementation.
- Produces: current-tree test, lint, import, and diff evidence; no new production API.

- [ ] **Step 1: Run the new tests alone**

```bash
pytest -q tests/test_auto_mode_router.py tests/test_auto_mode.py tests/test_auto_mode_guardrail.py
```

Expected: PASS.

- [ ] **Step 2: Run permission, terminal, runtime, HTTP, headless, MCP, and SWE regression tests**

```bash
pytest -q tests/test_permissions.py tests/test_terminal_interactions.py tests/test_handoffs.py tests/test_headless_cli.py tests/test_http_service.py tests/test_mcp.py tests/test_loop_fake.py tests/test_swebench_lite.py tests/test_swebench_strict.py tests/runtime/model_gateway tests/runtime/tool_runtime
```

Expected: PASS. Existing platform-dependent skips remain skips.

- [ ] **Step 3: Run the full suite**

```bash
pytest -q
```

Expected: all tests pass; compare the count with the pre-change baseline `2893 passed, 21 skipped` and explain only intentional new-test growth.

- [ ] **Step 4: Run static verification**

```bash
ruff check nz_coder tests
python -m compileall -q nz_coder tests
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 5: Inspect final scope and protected surface behavior**

```bash
git status --short
git diff --stat
git diff -- nz_coder/config.py nz_coder/runtime/model_gateway/models.py nz_coder/tool_platform/permissioning nz_coder/runtime/auto_mode.py nz_coder/runtime/guardrail_runtime.py nz_coder/runtime/loop.py nz_coder/interface/interactions.py nz_coder/interface/cli.py tests/test_auto_mode_router.py tests/test_auto_mode.py tests/test_auto_mode_guardrail.py
```

Expected: only intended Auto-mode hunks are attributed to this implementation; unrelated user changes remain untouched.

- [ ] **Step 6: Request read-only code review**

Dispatch cold-context reviewers for runtime correctness, adversarial permission bypasses, and surface compatibility. They must not edit files. Resolve every P0/P1 finding in the primary agent, rerun the smallest failing test first, then repeat Steps 1–5.

- [ ] **Step 7: Record the verified result**

If implementation commits were safe in the dirty worktree, ensure the final task commit contains only intended hunks. Otherwise leave the verified changes uncommitted and report why no mixed commit was created. Report exact test/lint counts and the remaining known limitations: remote Auto opt-in, separate classifier model selection, cross-session degradation persistence, and persisted exact Auto approval are outside this release.
