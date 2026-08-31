# Terminal Efficiency Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make NZ-Coder return durable terminal text and converge on targeted verification within the 15-call nominal product SLA using deterministic runtime evidence.

**Architecture:** Preserve the existing native runner and add behavior at its current ownership boundaries. `ProductionRunLifecycle` owns terminal content, `VerificationScheduler` owns pure pressure/mutation selection, recovery composes independent deterministic signals, and `RuntimeState` owns convergence-time project facts and policy decisions.

**Tech Stack:** Python 3.9+, standard library, pytest, Ruff; no new runtime dependency or Agent framework.

## Global Constraints

- Keep the 15-call nominal SLA and 20-call absolute hard cap.
- Exact acceptance and unresolved hard requirements remain authoritative; call reduction cannot manufacture success.
- Do not add a planner, sidecar model, provider call, or cron-task-specific rule.
- Preserve existing tool names, tool input schemas, and legacy adapter behavior.
- All new behavior must be covered by a test that is observed failing before production code changes.
- Use only task-owned files in local commits; do not push and do not include unrelated dirty-worktree changes.

---

## File Structure

- Modify `nz_coder/runtime/run_lifecycle.py`: resolve and store authoritative terminal content before completion publication.
- Modify `nz_coder/runtime/core/lifecycle_context.py`: document the optional terminal-content lifecycle callback argument.
- Modify `nz_coder/runtime/loop.py`: project resolved terminal content into the current assistant message without overwriting provider text.
- Modify `nz_coder/runtime/work_budget.py`: expose the current pressure zone independently of one-shot notices.
- Modify `nz_coder/runtime/verification_scheduler.py`: make stage decisions generation-aware.
- Modify `nz_coder/runtime/runtime_state.py`: persist scheduled stage generations and bounded workspace facts; enforce non-Git closure rules.
- Modify `nz_coder/runtime/runner.py`: reconsider staged verification at settled tool boundaries and trace decisions.
- Create `nz_coder/intelligence/failure_diagnostics.py`: define and render deterministic composite diagnostic signals.
- Modify `nz_coder/recovery.py`: collect existing failure facts into signals and select the most specific action.
- Modify `nz_coder/runtime/tool_runtime/policy.py`: return actionable denial text and trace reason for Git-only closure commands.
- Modify `nz_coder/runtime/hooks.py`: persist and trace structured recovery classifications when a diagnostic is injected.
- Create `tests/runtime/test_run_lifecycle.py`: direct production-lifecycle content and event-order contracts.
- Modify `tests/runtime/test_native_runner.py`: native terminal and mutation-aware scheduling contracts.
- Modify `tests/runtime/test_verification_scheduler.py`: pure zone/generation scheduler contracts.
- Modify `tests/runtime/test_work_budget.py`: stable pressure-zone contracts.
- Modify `tests/test_recovery.py`: composite recovery precedence contracts.
- Modify `tests/test_hooks.py`: recovery diagnostic state/trace projection contract.
- Modify `tests/test_runtime_state.py`: non-Git closure policy and serialization contracts.
- Modify `tests/runtime/test_tool_runtime.py`: product-facing closure denial metadata and guidance.
- Modify `docs/infcode-alignment-learning-log.md`: record reference mapping, implementation, and measured result.
- Modify `docs/terminal-product-real-world-issues.md`: close or update the fifth-run issues with verification evidence.

---

### Task 1: Durable terminal content ownership

**Files:**
- Create: `tests/runtime/test_run_lifecycle.py`
- Modify: `tests/runtime/test_native_runner.py`
- Modify: `nz_coder/runtime/run_lifecycle.py:103-173`
- Modify: `nz_coder/runtime/core/lifecycle_context.py:61-105`
- Modify: `nz_coder/runtime/loop.py:803-845`

**Interfaces:**
- Consumes: the `content_text: str | None` parameter of `ProductionRunLifecycle.finalize()` and the current lifecycle callback `persist_assistant_end`.
- Produces: `persist_assistant_end(messages: list[dict], status: str, content_text: str = "") -> bool`; every lifecycle result dict includes `content: str`.

- [ ] **Step 1: Write a failing native terminal persistence test**

Create `tests/runtime/test_run_lifecycle.py` with a `LifecycleExecutionContext`
factory whose callbacks append `persist`, `commit`, and `publish` markers. Make
`persist_assistant_end` fill empty assistant content and return `True`. Assert:

```python
result = ProductionRunLifecycle().finalize_sync(
    context,
    messages,
    "completed",
    stream=False,
    content_text="Completed the requested changes in app.py.",
)
assert result["content"] == "Completed the requested changes in app.py."
assert messages[-1]["content"] == result["content"]
assert events.index("persist") < events.index("commit") < events.index("publish")
```

Add a second case in which the provider returned `"provider summary"`; the lifecycle must preserve that assistant content instead of replacing it.
In the first case, pass the returned dict to `runner._typed_result()` with a
minimal `RunContext` and assert `typed.final_text == result["content"]`. This
pins the lifecycle-to-SDK/headless boundary rather than only the lifecycle
helper.

- [ ] **Step 2: Run the terminal tests and verify RED**

Run:

```bash
pytest -q tests/runtime/test_run_lifecycle.py
```

Expected: FAIL because `ProductionRunLifecycle.last_status` has no `content` field and `_persist_assistant_end_state` cannot receive terminal content.

- [ ] **Step 3: Implement terminal-content resolution in the lifecycle**

In `nz_coder/runtime/run_lifecycle.py`, add a current-turn assistant lookup and resolve text before end-state persistence:

```python
def _terminal_content(messages: list, supplied: str | None) -> tuple[str, str]:
    if isinstance(supplied, str) and supplied.strip():
        return supplied, "boundary"
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "assistant":
            value = message.get("content")
            if isinstance(value, str) and value.strip():
                return value, "assistant"
    return "", "empty"
```

Call it before `persist_assistant_end`, pass the text to the callback, and include it in both normal and aborted `last_status` payloads:

```python
resolved_content, content_source = _terminal_content(messages, content_text)
persisted_into_assistant = bool(
    context.persist_assistant_end(messages, status, resolved_content)
)
state.last_status = {
    "status": status,
    "content": resolved_content,
    "errors": (
        context.recovery.consecutive_errors if status == "max_turns" else 0
    ),
    **context.vm.status(),
    "runtime": runtime,
}
context.trace(
    "terminal_content_persisted",
    source=content_source,
    nonempty=bool(resolved_content),
    persisted_into_assistant=persisted_into_assistant,
)
```

Keep cancellation/interruption content empty when neither source exists.

- [ ] **Step 4: Project content into the existing assistant message**

Change `_persist_assistant_end_state` to accept `content_text: str = ""` and
return whether content was projected. Before `set_assistant_end_state`, fill
only empty textual content:

```python
persisted_content = False
if content_text.strip() and not str(target.get("content") or "").strip():
    target["content"] = content_text
    persisted_content = True
# Keep end-state and checkpoint calls unchanged.
return persisted_content
```

Do not create an assistant message and do not overwrite non-empty provider text. Existing checkpointing remains after end-state mutation.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
pytest -q tests/runtime/test_run_lifecycle.py tests/runtime/test_native_runner.py tests/runtime/core/test_lifecycle_context.py tests/test_session_lifecycle.py
```

Expected: all selected tests pass; native `RunResult.final_text` and transcript text agree.

- [ ] **Step 6: Commit the independently working terminal fix**

```bash
git add tests/runtime/test_run_lifecycle.py tests/runtime/test_native_runner.py nz_coder/runtime/run_lifecycle.py nz_coder/runtime/core/lifecycle_context.py nz_coder/runtime/loop.py
git commit -m "fix: persist authoritative terminal content"
```

---

### Task 2: Mutation-aware pressure verification

**Files:**
- Modify: `tests/runtime/test_work_budget.py`
- Modify: `tests/runtime/test_verification_scheduler.py`
- Modify: `tests/runtime/test_native_runner.py`
- Modify: `nz_coder/runtime/work_budget.py:74-129`
- Modify: `nz_coder/runtime/verification_scheduler.py:7-108`
- Modify: `nz_coder/runtime/runtime_state.py:140-164,218-249`
- Modify: `nz_coder/runtime/runner.py:447-485,1251-1468`

**Interfaces:**
- Consumes: `RuntimeState.mutation_generation`, verification-pipeline status, unresolved requirement IDs, and `WorkBudgetController` thresholds.
- Produces: `WorkBudgetController.zone(completed_turns: int) -> str`; `RuntimeState.scheduled_verification_generations: dict[str, int]`; generation-aware `VerificationScheduler.action(zone: str, *, verification_status: dict, unresolved_requirements: tuple[str, ...] | list[str], has_exact_contract: bool, mutation_generation: int = 0, scheduled_generations: dict[str, int] | None = None) -> VerificationAction`.

- [ ] **Step 1: Write failing pure zone and generation tests**

Add to `tests/runtime/test_work_budget.py`:

```python
def test_work_budget_reports_current_zone_after_notice_was_consumed():
    budget = WorkBudgetController(max_turns=20)
    assert budget.next_notice(12).zone == "orange"
    assert budget.next_notice(12) is None
    assert budget.zone(12) == "orange"
    assert budget.zone(13) == "red"
```

Add to `tests/runtime/test_verification_scheduler.py`:

```python
def test_targeted_stage_is_eligible_once_per_mutation_generation():
    scheduler = VerificationScheduler()
    first = scheduler.action(
        "orange",
        verification_status=_status(static="passed"),
        unresolved_requirements=("R1",),
        has_exact_contract=True,
        mutation_generation=2,
        scheduled_generations={"targeted": 1},
    )
    repeated = scheduler.action(
        "orange",
        verification_status=_status(static="passed"),
        unresolved_requirements=("R1",),
        has_exact_contract=True,
        mutation_generation=2,
        scheduled_generations={"targeted": 2},
    )
    assert first.stage == "targeted"
    assert first.mutation_generation == 2
    assert repeated.kind == "none"
```

- [ ] **Step 2: Run pure tests and verify RED**

```bash
pytest -q tests/runtime/test_work_budget.py tests/runtime/test_verification_scheduler.py
```

Expected: FAIL because `zone`, `VerificationAction.mutation_generation`, and generation arguments do not exist.

- [ ] **Step 3: Extract a stable pressure-zone calculation**

Implement one calculation used by both `zone()` and `next_notice()`:

```python
def zone(self, completed_turns: int) -> str:
    completed = max(0, int(completed_turns))
    yellow_at, orange_at, red_at = self._thresholds()
    if completed >= red_at:
        return "red"
    if completed >= orange_at:
        return "orange"
    if completed >= yellow_at:
        return "yellow"
    return "green"
```

`next_notice()` calls `zone()` and retains its emit-once semantics.

- [ ] **Step 4: Make scheduler selection generation-aware**

Add `mutation_generation: int = 0` to `VerificationAction`. Add optional scheduler inputs with compatibility defaults. When a stage command is selected, suppress it if the recorded generation is greater than or equal to the current positive generation:

```python
generation = max(0, int(mutation_generation))
attempted = int((scheduled_generations or {}).get(stage, -1))
if generation > 0 and attempted >= generation:
    return VerificationAction()
return VerificationAction(
    kind="stage",
    stage=stage,
    command=command,
    reason=reason,
    mutation_generation=generation,
)
```

Completion acceptance remains independent of stage-generation suppression.

- [ ] **Step 5: Persist scheduled generations in runtime state**

Add and reset these fields:

```python
scheduled_verification_generations: dict[str, int] = field(default_factory=dict)
budget_pressure_zone: str = "green"
```

Existing `asdict()` serialization and guarded `restore()` persist it without a custom migration.
At each turn start assign `context.runtime_state.budget_pressure_zone =
work_budget.zone(turn_index)` alongside `work_phase`.

- [ ] **Step 6: Write a failing native safe-boundary integration test**

Create a native model/tools pair that mutates after the orange notice has been consumed. Assert one targeted runtime check and no repeat without another mutation:

```python
assert tools.names.count("bash") == 1
assert state.scheduled_verification_generations == {
    "targeted": state.mutation_generation,
}
assert any(
    event == "verification_scheduler_decision"
    and payload["stage"] == "targeted"
    for event, payload in trace_events
)
```

- [ ] **Step 7: Run integration test and verify RED**

```bash
pytest -q tests/runtime/test_native_runner.py -k 'pressure_verification or mutation_generation'
```

Expected: FAIL because a consumed notice is never reconsidered after mutation.

- [ ] **Step 8: Schedule at settled tool boundaries**

In `_settle_terminal_boundary`, before its pre-nominal early return, compute the current zone, trace the decision, and dispatch a selected stage. Pass state generations into `_verification_action`. In `_execute_scheduled_verification`, record the stage generation before dispatch and persist state immediately so a failed check does not repeat until a new mutation.

```python
zone = work_budget.zone(completed_turns)
action = self._verification_action(context, zone)
context.hooks.trace(
    "verification_scheduler_decision",
    zone=zone,
    kind=action.kind,
    stage=action.stage,
    command_fingerprint=(
        hashlib.sha256(action.command.encode("utf-8")).hexdigest()[:12]
        if action.command else ""
    ),
    mutation_generation=action.mutation_generation,
    reason=action.reason,
)
```

Add the standard-library `hashlib` import to `runner.py`; do not place the raw
command in this new trace event.

- [ ] **Step 9: Run scheduler and native tests and verify GREEN**

```bash
pytest -q tests/runtime/test_work_budget.py tests/runtime/test_verification_scheduler.py tests/runtime/test_native_runner.py
```

Expected: all selected tests pass; one targeted verification occurs for each new mutation generation under pressure.

- [ ] **Step 10: Commit the independently working scheduler fix**

```bash
git add tests/runtime/test_work_budget.py tests/runtime/test_verification_scheduler.py tests/runtime/test_native_runner.py nz_coder/runtime/work_budget.py nz_coder/runtime/verification_scheduler.py nz_coder/runtime/runtime_state.py nz_coder/runtime/runner.py
git commit -m "feat: verify mutations at convergence boundaries"
```

---

### Task 3: Composite deterministic failure diagnosis

**Files:**
- Create: `nz_coder/intelligence/failure_diagnostics.py`
- Modify: `tests/test_recovery.py`
- Modify: `tests/test_runtime_state.py`
- Modify: `tests/test_hooks.py`
- Modify: `nz_coder/recovery.py:395-570`
- Modify: `nz_coder/runtime/runtime_state.py:146-164,218-249`
- Modify: `nz_coder/runtime/hooks.py:1073-1090`

**Interfaces:**
- Consumes: failed-test, traceback, regression, and subprocess-workspace detectors already present in `recovery.py`.
- Produces: `DiagnosticSignal`; `render_failure_diagnostic(signals: list[DiagnosticSignal], *, failed_tests: list[str], traceback_text: str) -> str`; persisted primary/supporting recovery classifications.

- [ ] **Step 1: Replace the old precedence assertion with failing composite assertions**

Change the stale-helper plus multi-file test in `tests/test_recovery.py` to assert:

```python
assert "primary_classification: subprocess_workspace_drift" in diagnostic
assert "supporting_classification: widespread_test_regression" in diagnostic
assert "cron_engine/tests/test_cli.py" in diagnostic
assert "Update that helper's `cwd`" in diagnostic
assert "Do not patch individual test helpers" not in diagnostic
```

Add a no-drift multi-file test asserting
`primary_classification: widespread_test_regression`.

Add to `tests/test_hooks.py`:

```python
def test_tool_failure_diagnostic_hook_records_structured_recovery_facts():
    recorded = []
    traces = []
    diagnostic = (
        "<test-failure-diagnostic>\n"
        "primary_classification: subprocess_workspace_drift\n"
        "supporting_classification: widespread_test_regression\n"
        "repair_target: tests/test_cli.py\n"
        "</test-failure-diagnostic>"
    )
    loop = SimpleNamespace(
        recovery=SimpleNamespace(
            tool_failure_diagnostic=lambda _name, _output: diagnostic,
        ),
        runtime_state=SimpleNamespace(
            record_recovery_diagnostic=recorded.append,
            primary_recovery_classification="subprocess_workspace_drift",
            supporting_recovery_classifications=["widespread_test_regression"],
            recovery_repair_targets=["tests/test_cli.py"],
        ),
        tracer=SimpleNamespace(log=lambda event, **data: traces.append((event, data))),
    )
    messages = []
    tool_failure_diagnostic_hook(ToolResultContext(
        loop=loop,
        messages=messages,
        result=SimpleNamespace(name="bash"),
        output="failed",
    ))
    assert recorded == [diagnostic]
    assert traces[-1][1]["primary"] == "subprocess_workspace_drift"
```

- [ ] **Step 2: Run recovery tests and verify RED**

```bash
pytest -q tests/test_recovery.py tests/test_runtime_state.py tests/test_hooks.py -k 'workspace_drift or widespread or structured_recovery'
```

Expected: FAIL because the widespread branch returns before workspace-drift
detection and runtime state has no structured recovery recorder.

- [ ] **Step 3: Create the diagnostic signal model and renderer**

Create `nz_coder/intelligence/failure_diagnostics.py` with:

```python
"""Structured deterministic failure signals and diagnostic rendering."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiagnosticSignal:
    classification: str
    specificity: int
    evidence: str
    action: str
    repair_target: str = ""


def render_failure_diagnostic(
    signals: list[DiagnosticSignal],
    *,
    failed_tests: list[str],
    traceback_text: str,
) -> str:
    ordered = sorted(signals, key=lambda item: item.specificity, reverse=True)
    primary = ordered[0]
    parts = [
        "<test-failure-diagnostic>",
        f"primary_classification: {primary.classification}",
    ]
    parts.extend(
        f"supporting_classification: {item.classification}"
        for item in ordered[1:]
    )
    if primary.repair_target:
        parts.append(f"repair_target: {primary.repair_target}")
    parts.extend([primary.evidence, f"Next action:\n1. {primary.action}"])
    if failed_tests:
        parts.append(
            "Failing tests:\n"
            + "\n".join(f"  - {item}" for item in failed_tests[:6])
        )
    if traceback_text:
        parts.append(f"Root cause:\n{traceback_text}")
    parts.append("</test-failure-diagnostic>")
    return "\n\n".join(part for part in parts if part)
```

Use fixed policy specificity: workspace drift `100`, subprocess package root
`90`, import/package layout `80`, widespread regression `50`, generic failure
`10`.

- [ ] **Step 4: Refactor recovery into collection then rendering**

Add a collector that computes the same concrete predicates currently embedded
in `_build_test_failure_diagnostic()`:

```python
def _collect_test_failure_signals(output: str) -> list[DiagnosticSignal]:
    signals: list[DiagnosticSignal] = []
    failed_tests = _extract_failed_tests(output)
    failed_paths = list(dict.fromkeys(
        item.split("::", 1)[0].replace("\\", "/")
        for item in failed_tests
        if "::" in item
    ))
    workspace_drift = _workspace_drift_signal(output)
    if workspace_drift is not None:
        signals.append(workspace_drift)
    if len(failed_paths) >= 2:
        signals.append(_widespread_regression_signal(failed_paths))
    if _is_subprocess_package_root_failure(output):
        signals.append(_subprocess_package_root_signal(output, failed_tests))
    if _is_import_collection_failure(output):
        signals.append(_import_layout_signal(output))
    if not signals:
        signals.append(_generic_test_failure_signal(output))
    return signals
```

Implement each named private helper by moving its existing condition and wording
out of the current early-return branch. `_workspace_drift_signal()` sets
`repair_target=workspace_drift.helper`. Catch errors around workspace-drift
detection only. Call `render_failure_diagnostic()` once after collection.

- [ ] **Step 5: Persist classification facts after verification failure**

Add and reset:

```python
primary_recovery_classification: str = ""
supporting_recovery_classifications: list[str] = field(default_factory=list)
recovery_repair_targets: list[str] = field(default_factory=list)
```

Add `RuntimeState.record_recovery_diagnostic(diagnostic: str) -> None`, which
extracts `primary_classification:`, every `supporting_classification:`, and
`repair_target:` line. In `tool_failure_diagnostic_hook()`, call this method
before appending the synthetic user message, then trace `primary`, `supporting`,
and `repair_targets`. `asdict()` and `restore()` own serialization.

Extend `_known_closure_paths()` with normalized `recovery_repair_targets`, so a
resolved helper becomes immediately readable/editable during closure.

- [ ] **Step 6: Run recovery and state tests and verify GREEN**

```bash
pytest -q tests/test_recovery.py tests/test_runtime_state.py tests/test_hooks.py
```

Expected: all tests pass; drift is primary and widespread regression remains a
supporting observation.

- [ ] **Step 7: Commit the independently working diagnostic fix**

```bash
git add nz_coder/intelligence/failure_diagnostics.py nz_coder/recovery.py nz_coder/runtime/runtime_state.py nz_coder/runtime/hooks.py tests/test_recovery.py tests/test_runtime_state.py tests/test_hooks.py
git commit -m "fix: compose actionable recovery diagnostics"
```

---

### Task 4: Non-Git convergence facts and actionable policy denial

**Files:**
- Modify: `tests/test_runtime_state.py`
- Modify: `tests/runtime/test_tool_runtime.py`
- Modify: `nz_coder/runtime/runtime_state.py:151-164,184-249,697-790`
- Modify: `nz_coder/runtime/run_lifecycle.py:15-63`
- Modify: `nz_coder/runtime/tool_runtime/policy.py:285-334`

**Interfaces:**
- Consumes: current workspace, `RuntimeState.work_phase`, Bash command input, and current closure paths.
- Produces: `RuntimeState.workspace_git_available: bool | None`; `RuntimeState.closure_phase_decision(tool_name: str, tool_input: dict | None = None) -> tuple[str, str]`; denial reason `git_required_but_unavailable`.

- [ ] **Step 1: Write failing runtime-state policy tests**

Add to `tests/test_runtime_state.py`:

```python
def test_non_git_closure_blocks_raw_git_inspection_but_allows_runtime_tools():
    state = RuntimeState(
        work_phase="closure_repair",
        workspace_git_available=False,
    )
    assert state.closure_phase_decision(
        "bash", {"command": "git diff -- app.py"}
    ) == ("block", "git_required_but_unavailable")
    assert state.closure_phase_decision("diff_status", {}) == ("allow", "")

    orange = RuntimeState(
        work_phase="normal",
        budget_pressure_zone="orange",
        workspace_git_available=False,
    )
    assert orange.closure_phase_decision(
        "bash", {"command": "git status --short"}
    ) == ("block", "git_required_but_unavailable")


def test_git_inspection_remains_available_before_convergence():
    state = RuntimeState(work_phase="normal", workspace_git_available=False)
    assert state.closure_phase_decision(
        "bash", {"command": "git status --short"}
    ) == ("allow", "")
```

Add round-trip assertions for `workspace_git_available` values `True`, `False`,
and `None`.

- [ ] **Step 2: Run runtime-state tests and verify RED**

```bash
pytest -q tests/test_runtime_state.py -k 'git and closure'
```

Expected: FAIL because neither the field nor structured decision exists.

- [ ] **Step 3: Capture bounded Git capability once per run**

Add `workspace_git_available: bool | None = None`. During lifecycle
initialization assign:

```python
try:
    context.runtime_state.workspace_git_available = (
        current_workdir() / ".git"
    ).exists()
except OSError:
    context.runtime_state.workspace_git_available = None
```

This treats both Git directories and worktree `.git` files as available.

- [ ] **Step 4: Add a structured closure decision**

Move the current closure implementation to `_closure_phase_action()`. Keep the
old string surface and add the reasoned surface:

```python
def closure_phase_action(self, tool_name: str, tool_input: dict | None = None) -> str:
    return self.closure_phase_decision(tool_name, tool_input)[0]


def closure_phase_decision(
    self,
    tool_name: str,
    tool_input: dict | None = None,
) -> tuple[str, str]:
    name = str(tool_name or "")
    payload = tool_input or {}
    command = " ".join(str(payload.get("command") or "").split())
    git_only = bool(re.match(r"^git\s+(?:diff|status)(?:\s|$)", command))
    pressured = (
        self.work_phase in {
            "closure_repair", "closure_finalize", "bounded_emergency",
        }
        or self.budget_pressure_zone in {"orange", "red"}
    )
    if pressured and name == "bash" and self.workspace_git_available is False and git_only:
        return "block", "git_required_but_unavailable"
    return self._closure_phase_action(name, payload), ""
```

- [ ] **Step 5: Write a failing product-facing denial test**

In `tests/runtime/test_tool_runtime.py`, route a Bash `git diff` call through
`closure_phase_rejections()` and assert:

```python
result = rejected[0]
assert result.metadata["reason"] == "git_required_but_unavailable"
assert "diff_status" in result.output
assert trace_events[-1][1]["reason"] == "git_required_but_unavailable"
```

- [ ] **Step 6: Run the product-facing test and verify RED**

```bash
pytest -q tests/runtime/test_tool_runtime.py -k 'non_git or closure'
```

Expected: FAIL because policy emits only a generic closure denial.

- [ ] **Step 7: Use the structured decision in tool policy**

Call `closure_phase_decision` when present and fall back to the old action
method for compatibility. For this reason render:

```text
Denied: this workspace is not a Git repository, so raw git inspection cannot
provide evidence. Use diff_status or the recorded changed-file evidence.
```

Include `reason` in result metadata and the blocked trace. Leave other closure
messages unchanged.

- [ ] **Step 8: Run policy tests and verify GREEN**

```bash
pytest -q tests/test_runtime_state.py tests/runtime/test_tool_runtime.py tests/runtime/test_work_budget.py
```

Expected: all tests pass; Git commands are restricted only during convergence
in a confirmed non-Git workspace.

- [ ] **Step 9: Commit the independently working policy fix**

```bash
git add tests/test_runtime_state.py tests/runtime/test_tool_runtime.py nz_coder/runtime/runtime_state.py nz_coder/runtime/run_lifecycle.py nz_coder/runtime/tool_runtime/policy.py
git commit -m "fix: reject git-only probes in non-git closure"
```

---

### Task 5: Cross-component regression and real product validation

**Files:**
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: `docs/terminal-product-real-world-issues.md`

**Interfaces:**
- Consumes: the four completed runtime changes and `/home/pyh/test_nzcoder` product fixture.
- Produces: reproducible local verification evidence and one documented real-run result.

- [ ] **Step 1: Run the focused cross-component suite**

```bash
pytest -q \
  tests/runtime/test_native_runner.py \
  tests/runtime/test_verification_scheduler.py \
  tests/runtime/test_work_budget.py \
  tests/test_recovery.py \
  tests/test_runtime_state.py \
  tests/runtime/test_tool_runtime.py \
  tests/test_session_lifecycle.py
```

Expected: all selected tests pass with no warning introduced by changed code.

- [ ] **Step 2: Run the complete local suite**

```bash
pytest -q
```

Expected: the complete repository suite passes; existing environment-dependent
skips remain skips.

- [ ] **Step 3: Run static quality checks**

```bash
ruff check \
  nz_coder/runtime/run_lifecycle.py \
  nz_coder/runtime/core/lifecycle_context.py \
  nz_coder/runtime/loop.py \
  nz_coder/runtime/work_budget.py \
  nz_coder/runtime/verification_scheduler.py \
  nz_coder/runtime/runtime_state.py \
  nz_coder/runtime/runner.py \
  nz_coder/intelligence/failure_diagnostics.py \
  nz_coder/recovery.py \
  nz_coder/runtime/tool_runtime/policy.py
```

Expected: exit code 0.

- [ ] **Step 4: Inspect the implementation diff**

```bash
git diff --check
git diff --stat
git diff -- nz_coder tests docs/superpowers
```

Expected: no whitespace errors, secrets, cron-specific runtime conditions, or
unrelated user files.

- [ ] **Step 5: Run one fresh real long task**

Launch NZ-Coder against `/home/pyh/test_nzcoder` with the same deterministic
cron task contract used in the fifth run. Use a fresh session but keep exact
acceptance and provider configuration unchanged for a valid before/after
comparison.

Expected evidence:

```text
status: completed
coding model calls: <= 15
typed/headless final_text: non-empty
exact acceptance: passed for current mutation generation
hard requirements unresolved: 0
package-install attempts: 0
emergency broad exploration: 0
```

If a target is missed, preserve the trace and record the real value. Do not
rewrite status or remove evidence to meet the target.

- [ ] **Step 6: Update the two project learning documents**

Append a dated entry to `docs/infcode-alignment-learning-log.md` with:

- InfCodeX `RunResult.output`, stop-hook/invariant, and deterministic-evaluator mappings;
- OpenCode persisted assistant/completed/idle mapping;
- exact NZ-Coder files changed;
- focused/full test counts;
- real-run calls, tools, tokens, duration, final-text status, acceptance, and ledger outcome.

Update fifth-run issues in `docs/terminal-product-real-world-issues.md` to
`fixed`, `improved`, or `still open`, citing the new trace/session path.

- [ ] **Step 7: Re-run documentation and worktree checks**

```bash
git diff --check
git status --short
```

Expected: only intended implementation, tests, design, plan, and learning
document changes are task-owned.

- [ ] **Step 8: Commit the verified evidence**

```bash
git add docs/infcode-alignment-learning-log.md docs/terminal-product-real-world-issues.md
git commit -m "docs: record terminal efficiency closure evidence"
```

Do not push; publishing remains a separate user-authorized action.
