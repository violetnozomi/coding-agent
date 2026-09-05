"""Tests for configurable runtime hooks."""
from __future__ import annotations

import json
from types import SimpleNamespace


def test_tool_failure_diagnostic_hook_records_structured_recovery_facts():
    from nz_coder.runtime.verification.hooks import ToolResultContext, tool_failure_diagnostic_hook

    recorded = []
    traces = []
    diagnostic = (
        "<test-failure-diagnostic>\n"
        "primary_classification: subprocess_workspace_drift\n"
        "supporting_classification: widespread_test_regression\n"
        "repair_target: tests/test_cli.py\n"
        "</test-failure-diagnostic>"
    )
    runtime_state = SimpleNamespace(
        record_recovery_diagnostic=recorded.append,
        primary_recovery_classification="subprocess_workspace_drift",
        supporting_recovery_classifications=["widespread_test_regression"],
        recovery_repair_targets=["tests/test_cli.py"],
    )
    loop = SimpleNamespace(
        recovery=SimpleNamespace(
            tool_failure_diagnostic=lambda _name, _output: diagnostic,
        ),
        runtime_state=runtime_state,
        tracer=SimpleNamespace(
            log=lambda event, **data: traces.append((event, data)),
        ),
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
    assert traces[-1][1]["repair_targets"] == ["tests/test_cli.py"]


def test_tool_failure_hook_passes_contract_artifacts_to_real_recovery():
    """The hook must connect RuntimeState artifacts to read failure recovery."""
    from nz_coder.runtime.verification.recovery import RecoveryState
    from nz_coder.runtime.verification.hooks import ToolResultContext, tool_failure_diagnostic_hook

    recorded = []
    runtime_state = SimpleNamespace(
        task_contract={
            "requirements": [{
                "expected_artifacts": [
                    "cron_engine/parser.py",
                    "cron_engine/README.md",
                ],
            }],
        },
        record_recovery_diagnostic=recorded.append,
        primary_recovery_classification="",
        supporting_recovery_classifications=[],
        recovery_repair_targets=[],
    )
    loop = SimpleNamespace(
        recovery=RecoveryState(),
        runtime_state=runtime_state,
        tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
    )
    messages = []

    tool_failure_diagnostic_hook(ToolResultContext(
        loop=loop,
        messages=messages,
        result=SimpleNamespace(
            name="read_file",
            tool_input={"path": "README.md"},
        ),
        output="Error: File not found: README.md",
    ))

    assert "cron_engine/README.md" in messages[-1]["content"]
    assert recorded and "declared_artifact_path" in recorded[-1]


def test_tool_failure_hook_skips_current_environment_blocker_diagnostic():
    """An infrastructure failure must not inject source-repair instructions."""
    from nz_coder.runtime.verification.hooks import ToolResultContext, tool_failure_diagnostic_hook

    command = "python3 -m pytest tests/lint/unittest_lint.py -q"
    traces = []

    def unexpected_diagnostic(*_args, **_kwargs):
        raise AssertionError("environment blockers do not need code-repair guidance")

    loop = SimpleNamespace(
        recovery=SimpleNamespace(tool_failure_diagnostic=unexpected_diagnostic),
        runtime_state=SimpleNamespace(task_contract={}),
        vm=SimpleNamespace(status=lambda: {
            "verification_state": "blocked_environment",
            "environment_blocker": {
                "command": command,
                "output": "ModuleNotFoundError: No module named 'astroid'",
            },
        }),
        tracer=SimpleNamespace(
            log=lambda event, **data: traces.append((event, data)),
        ),
    )
    messages = []

    tool_failure_diagnostic_hook(ToolResultContext(
        loop=loop,
        messages=messages,
        result=SimpleNamespace(
            name="bash",
            tool_input={"command": command},
        ),
        output="Command exited with code 4\nModuleNotFoundError: No module named 'astroid'",
    ))

    assert messages == []
    assert traces == [(
        "tool_failure_diagnostic_skipped",
        {"name": "bash", "reason": "verification_environment_blocker"},
    )]


def test_tool_failure_hook_recovers_from_detailed_blocker_output(tmp_path):
    """The VM blocker excerpt must recover a locally available Django runner."""
    from nz_coder.runtime.verification.recovery import RecoveryState
    from nz_coder.runtime.verification.hooks import ToolResultContext, tool_failure_diagnostic_hook
    from nz_coder.runtime.process.workdir import scoped_workdir

    command = "python3 tests/runtests.py auth_tests.test_migrations -v1"
    blocker_output = "ModuleNotFoundError: No module named 'django'"
    (tmp_path / "django").mkdir()
    (tmp_path / "django" / "__init__.py").write_text("", encoding="utf-8")
    runner = tmp_path / "tests" / "runtests.py"
    runner.parent.mkdir()
    runner.write_text("# Django native test runner\n", encoding="utf-8")
    runtime_state = SimpleNamespace(
        task_contract={},
        primary_recovery_classification="",
        supporting_recovery_classifications=[],
        recovery_repair_targets=[],
    )
    loop = SimpleNamespace(
        recovery=RecoveryState(),
        runtime_state=runtime_state,
        vm=SimpleNamespace(status=lambda: {
            "verification_state": "blocked_environment",
            "environment_blocker": {
                "command": command,
                "output": blocker_output,
            },
        }),
        tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
    )
    messages = []

    with scoped_workdir(tmp_path):
        tool_failure_diagnostic_hook(ToolResultContext(
            loop=loop,
            messages=messages,
            result=SimpleNamespace(
                name="bash",
                tool_input={"command": command},
            ),
            output="Command exited with code 1",
        ))

    assert len(messages) == 1
    assert (
        "PYTHONPATH=. python3 tests/runtests.py "
        "auth_tests.test_migrations -v1"
    ) in messages[0]["content"]
    assert "Inspect the implicated source file" not in messages[0]["content"]


def test_tool_failure_hook_skips_policy_owned_rejection_diagnostic():
    """A precise policy rejection must not be wrapped in generic recovery text."""
    from nz_coder.runtime.verification.hooks import ToolResultContext, tool_failure_diagnostic_hook

    traces = []

    def unexpected_diagnostic(*_args, **_kwargs):
        raise AssertionError("policy output already contains the required next action")

    loop = SimpleNamespace(
        recovery=SimpleNamespace(tool_failure_diagnostic=unexpected_diagnostic),
        runtime_state=SimpleNamespace(task_contract={}),
        tracer=SimpleNamespace(
            log=lambda event, **data: traces.append((event, data)),
        ),
    )
    messages = []

    tool_failure_diagnostic_hook(ToolResultContext(
        loop=loop,
        messages=messages,
        result=SimpleNamespace(
            name="grep_search",
            tool_input={"query": "needle"},
            executed=False,
            dispatch_failed=True,
            permission_denied=False,
            metadata={"guardrail": "implementation_phase"},
        ),
        output=(
            "Denied: enough repository evidence has already been gathered; "
            "make the first source edit now."
        ),
    ))

    assert messages == []
    assert traces == [(
        "tool_failure_diagnostic_skipped",
        {"name": "grep_search", "reason": "policy_owned_rejection"},
    )]


def test_retired_strict_terminal_marker_does_not_suppress_recovery_diagnostic():
    """A retired investigation marker cannot act as a hidden policy rejection."""
    from nz_coder.runtime.verification.hooks import ToolResultContext, tool_failure_diagnostic_hook

    diagnostic = "Inspect the failed read and choose a corrected path."
    calls = []
    loop = SimpleNamespace(
        recovery=SimpleNamespace(
            tool_failure_diagnostic=lambda name, output, **kwargs: (
                calls.append((name, output, kwargs)) or diagnostic
            ),
        ),
        runtime_state=SimpleNamespace(
            task_contract={},
            primary_recovery_classification="",
            supporting_recovery_classifications=[],
            recovery_repair_targets=[],
        ),
        tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
    )
    messages = []

    tool_failure_diagnostic_hook(ToolResultContext(
        loop=loop,
        messages=messages,
        result=SimpleNamespace(
            name="grep_search",
            tool_input={"query": "needle"},
            permission_denied=False,
            metadata={"strict_terminal_blocker": True},
        ),
        output="Error: search backend returned an incomplete result",
    ))

    assert calls == [(
        "grep_search",
        "Error: search backend returned an incomplete result",
        {"tool_input": {"query": "needle"}},
    )]
    assert messages[-1]["content"] == diagnostic


def test_tool_failure_hook_skips_actionable_strict_shell_rejection():
    """Strict Bash guidance is already model-visible and must remain authoritative."""
    from nz_coder.runtime.verification.hooks import ToolResultContext, tool_failure_diagnostic_hook

    traces = []

    def unexpected_diagnostic(*_args, **_kwargs):
        raise AssertionError("strict Bash rejection does not need generic recovery")

    loop = SimpleNamespace(
        recovery=SimpleNamespace(tool_failure_diagnostic=unexpected_diagnostic),
        runtime_state=SimpleNamespace(task_contract={}),
        tracer=SimpleNamespace(
            log=lambda event, **data: traces.append((event, data)),
        ),
    )
    messages = []

    tool_failure_diagnostic_hook(ToolResultContext(
        loop=loop,
        messages=messages,
        result=SimpleNamespace(
            name="bash",
            tool_input={"command": "python -c 'print(1)'"},
            executed=True,
            dispatch_failed=True,
            permission_denied=False,
            metadata={},
        ),
        output=(
            "Error: arbitrary Python execution is forbidden in SWE-bench strict "
            "mode. Use repository tools or a direct narrow pytest command."
        ),
    ))

    assert messages == []
    assert traces == [(
        "tool_failure_diagnostic_skipped",
        {"name": "bash", "reason": "actionable_policy_output"},
    )]


def test_manual_compact_hook_uses_agent_bound_compaction():
    from nz_coder.runtime.verification.hooks import ToolBatchContext, manual_compact_hook

    calls = []

    class Loop:
        tracer = SimpleNamespace(log=lambda *_args, **_kwargs: None)

        def _compact_messages(self, messages, focus=None):
            calls.append((list(messages), focus))
            return [{"role": "user", "content": "compacted"}]

        def _on_context_compacted(self):
            calls.append("reset")

    messages = [{"role": "user", "content": "long"}]
    notices = []
    manual_compact_hook(
        ToolBatchContext(
            loop=Loop(),
            messages=messages,
            manual_compact=True,
            used_todo=False,
            on_text=notices.append,
            write_total=0,
            write_denied=0,
        )
    )

    assert calls == [([{"role": "user", "content": "long"}], None), "reset"]
    assert messages == [{"role": "user", "content": "compacted"}]
    assert notices == ["[manual compact]"]


def test_todo_reminder_is_silent_when_runtime_contract_owns_progress(monkeypatch):
    """The task contract is the sole progress ledger after admission."""
    from nz_coder.runtime.verification import hooks

    monkeypatch.setattr(
        hooks,
        "get_reminder",
        lambda _rounds: "<reminder>duplicate plan update</reminder>",
    )
    loop = SimpleNamespace(
        runtime_state=SimpleNamespace(contract_owns_progress=lambda: True),
        rounds_without_todo=4,
    )
    messages = []

    hooks.todo_reminder_hook(hooks.ToolBatchContext(
        loop=loop,
        messages=messages,
        manual_compact=False,
        used_todo=False,
        on_text=None,
        write_total=0,
        write_denied=0,
    ))

    assert loop.rounds_without_todo == 0
    assert messages == []


def test_strict_generation_hook_accepts_requested_test_changes():
    from nz_coder.runtime.verification.hooks import StopHookContext, strict_generation_stop_hook
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    context = StopHookContext(
        transcript=(),
        last_assistant_text="done",
        runtime_state={
            "mutation_generation": 1,
            "diff_generation": 1,
            "verification_generation": 1,
            "has_diff": True,
            "source_only": False,
            "tests_modified": True,
            "wants_tests": True,
            "strict_generation_ready": True,
            "verification": {"verification_needed": False},
        },
    )

    with scoped_runtime_overrides(strict_local_tools=True):
        decision = strict_generation_stop_hook(context)

    assert decision.action == "complete"


def test_strict_generation_hook_rejects_unrequested_test_changes():
    from nz_coder.runtime.verification.hooks import StopHookContext, strict_generation_stop_hook
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    context = StopHookContext(
        transcript=(),
        last_assistant_text="done",
        runtime_state={
            "mutation_generation": 1,
            "diff_generation": -1,
            "verification_generation": 1,
            "has_diff": True,
            "source_only": False,
            "tests_modified": True,
            "wants_tests": False,
            "strict_generation_ready": False,
            "verification": {"verification_needed": True},
        },
    )

    with scoped_runtime_overrides(strict_local_tools=True):
        decision = strict_generation_stop_hook(context)

    assert decision.action == "reanimate"


def test_strict_generation_hook_surfaces_pending_targeted_evidence():
    from nz_coder.runtime.verification.hooks import StopHookContext, strict_generation_stop_hook
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    context = StopHookContext(
        transcript=(),
        last_assistant_text="done",
        runtime_state={
            "mutation_generation": 2,
            "strict_generation_ready": True,
            "verification": {
                "verification_needed": True,
                "verification_pipeline": {
                    "stages": [{
                        "name": "targeted",
                        "commands": [{
                            "command": "pytest tests/test_api.py::test_retry",
                            "required": True,
                            "status": "pending",
                        }],
                    }],
                },
            },
        },
    )

    with scoped_runtime_overrides(strict_local_tools=True):
        decision = strict_generation_stop_hook(context)

    assert decision.action == "reanimate"
    assert "pytest tests/test_api.py::test_retry" in decision.message


def test_strict_generation_hook_requests_non_empty_targeted_evidence():
    from nz_coder.runtime.verification.hooks import StopHookContext, strict_generation_stop_hook
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    context = StopHookContext(
        transcript=(),
        last_assistant_text="done",
        runtime_state={
            "mutation_generation": 2,
            "strict_generation_ready": False,
            "verification": {
                "verification_needed": True,
                "verification_pipeline": {
                    "stages": [{
                        "name": "targeted",
                        "required": True,
                        "evidence_required": True,
                        "status": "pending",
                        "commands": [],
                    }],
                },
            },
        },
    )

    with scoped_runtime_overrides(strict_local_tools=True):
        decision = strict_generation_stop_hook(context)

    assert decision.action == "reanimate"
    assert "direct narrow behavioral test" in decision.message
    assert "at least one test" in decision.message


def test_strict_generation_hook_allows_targeted_environment_blocker():
    from nz_coder.runtime.verification.hooks import StopHookContext, strict_generation_stop_hook
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    context = StopHookContext(
        transcript=(),
        last_assistant_text="done",
        runtime_state={
            "mutation_generation": 2,
            "diff_generation": 2,
            "has_diff": True,
            "verification": {
                "verification_needed": True,
                "verification_state": "blocked_environment",
                "environment_blocker": {
                    "stage": "targeted",
                    "command": "pytest tests/test_api.py::test_retry",
                },
            },
        },
    )

    with scoped_runtime_overrides(strict_local_tools=True):
        decision = strict_generation_stop_hook(context)

    assert decision.action == "complete_unverified"


def test_strict_generation_hook_recovers_local_django_import_blocker(tmp_path):
    """A local package-path retry must run before terminal environment fallback."""
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.verification.hooks import StopHookContext, strict_generation_stop_hook
    from nz_coder.runtime.process.workdir import scoped_workdir

    (tmp_path / "django").mkdir()
    (tmp_path / "django" / "__init__.py").write_text("", encoding="utf-8")
    runner = tmp_path / "tests" / "runtests.py"
    runner.parent.mkdir()
    runner.write_text("# Django native test runner\n", encoding="utf-8")
    context = StopHookContext(
        transcript=(),
        last_assistant_text="done",
        runtime_state={
            "mutation_generation": 2,
            "diff_generation": 2,
            "has_diff": True,
            "verification": {
                "verification_needed": True,
                "verification_state": "blocked_environment",
                "environment_blocker": {
                    "stage": "targeted",
                    "command": (
                        "python3 tests/runtests.py "
                        "auth_tests.test_migrations -v1"
                    ),
                    "output": "ModuleNotFoundError: No module named 'django'",
                },
            },
        },
    )

    with (
        scoped_workdir(tmp_path),
        scoped_runtime_overrides(strict_local_tools=True),
    ):
        decision = strict_generation_stop_hook(context)

    assert decision.action == "reanimate"
    assert (
        "PYTHONPATH=. python3 tests/runtests.py "
        "auth_tests.test_migrations -v1"
    ) in decision.message


def test_strict_generation_hook_recovers_django_parallel_runtime(tmp_path):
    """A host unittest mismatch must retry the same Django scope serially."""
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.verification.hooks import StopHookContext, strict_generation_stop_hook
    from nz_coder.runtime.process.workdir import scoped_workdir

    (tmp_path / "django").mkdir()
    (tmp_path / "django" / "__init__.py").write_text("", encoding="utf-8")
    runner = tmp_path / "tests" / "runtests.py"
    runner.parent.mkdir()
    runner.write_text("# Django native test runner\n", encoding="utf-8")
    context = StopHookContext(
        transcript=(),
        last_assistant_text="done",
        runtime_state={
            "mutation_generation": 2,
            "diff_generation": 2,
            "has_diff": True,
            "verification": {
                "verification_needed": True,
                "verification_state": "blocked_environment",
                "environment_blocker": {
                    "stage": "targeted",
                    "command": (
                        "PYTHONPATH=. python3 tests/runtests.py "
                        "auth_tests.test_migrations -v1"
                    ),
                    "output": "RuntimeWarning: TestResult has no addDuration method",
                },
            },
        },
    )

    with (
        scoped_workdir(tmp_path),
        scoped_runtime_overrides(strict_local_tools=True),
    ):
        decision = strict_generation_stop_hook(context)

    assert decision.action == "reanimate"
    assert (
        "PYTHONPATH=. python3 tests/runtests.py "
        "auth_tests.test_migrations -v1 --parallel 1"
    ) in decision.message


def test_strict_generation_hook_does_not_repeat_serial_django_retry(tmp_path):
    """An already-serial recovery command cannot create a reanimation loop."""
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.verification.hooks import StopHookContext, strict_generation_stop_hook
    from nz_coder.runtime.process.workdir import scoped_workdir

    (tmp_path / "django").mkdir()
    (tmp_path / "django" / "__init__.py").write_text("", encoding="utf-8")
    runner = tmp_path / "tests" / "runtests.py"
    runner.parent.mkdir()
    runner.write_text("# Django native test runner\n", encoding="utf-8")
    context = StopHookContext(
        transcript=(),
        last_assistant_text="done",
        runtime_state={
            "mutation_generation": 2,
            "diff_generation": 2,
            "has_diff": True,
            "verification": {
                "verification_needed": True,
                "verification_state": "blocked_environment",
                "environment_blocker": {
                    "stage": "targeted",
                    "command": (
                        "PYTHONPATH=. python3 tests/runtests.py "
                        "auth_tests.test_migrations -v1 --parallel 1"
                    ),
                    "output": "RuntimeWarning: TestResult has no addDuration method",
                },
            },
        },
    )

    with (
        scoped_workdir(tmp_path),
        scoped_runtime_overrides(strict_local_tools=True),
    ):
        decision = strict_generation_stop_hook(context)

    assert decision.action == "complete_unverified"


def test_strict_generation_hook_rejects_static_only_environment_blocker():
    from nz_coder.runtime.verification.hooks import StopHookContext, strict_generation_stop_hook
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    context = StopHookContext(
        transcript=(),
        last_assistant_text="done",
        runtime_state={
            "mutation_generation": 2,
            "diff_generation": 2,
            "has_diff": True,
            "verification": {
                "verification_needed": True,
                "verification_state": "blocked_environment",
                "environment_blocker": {
                    "stage": "static",
                    "command": "ruff check pkg/module.py",
                },
            },
        },
    )

    with scoped_runtime_overrides(strict_local_tools=True):
        decision = strict_generation_stop_hook(context)

    assert decision.action == "reanimate"


def _stub_loop(
    tracer=None,
    *,
    requested_paths=None,
    actual_output_paths=None,
    created_files=None,
    modified_files=None,
    wants_tests=False,
    tests_modified=False,
):
    runtime_state = SimpleNamespace(
        turn_count=3,
        task_mode="feature",
        initial_task_text="Implement the feature.",
        acceptance_criteria=["Add feature"],
        requested_paths=list(requested_paths or ["src/app.py"]),
        changed_files=list(modified_files or []),
        edits_this_run=1,
        has_diff=bool(actual_output_paths or modified_files),
        wants_tests=wants_tests,
        tests_modified=tests_modified,
    )
    run_evidence = SimpleNamespace(
        created_files=list(created_files or []),
        modified_files=list(modified_files or []),
        actual_output_paths=list(actual_output_paths or []),
        expected_files=[],
    )
    return SimpleNamespace(
        session_id="session-1",
        agent_id="agent-1",
        trace_id="trace-1",
        runtime_state=runtime_state,
        run_evidence=run_evidence,
        tracer=tracer,
    )


def test_parse_hook_condition_matches_tool_and_runtime_context():
    from nz_coder.runtime.verification.hooks import HookContext, parse_hook_condition

    ctx = HookContext(
        loop=None,
        messages=[{"role": "user", "content": "fix it"}],
        event_name="pre_tool_use",
        task_mode="feature",
        message_count=1,
        wants_tests=True,
        tests_modified=False,
        tool_name="write_file",
        tool_args={"path": "src/app.py"},
        file_path="src/app.py",
        is_write=True,
    )
    group = parse_hook_condition(
        'tool == "write_file" && args.path ~= "*.py" && task_mode == "feature" && is_write == "true"'
    )

    assert group is not None
    assert group.evaluate(ctx) is True


def test_load_configured_hooks_from_settings(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.verification.hooks import load_configured_hooks_from_settings

    settings_dir = tmp_path / ".nz-coder"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "id": "reopen-missing-tests",
                        "event": "no_tool_response",
                        "if": 'missing_requested_test_paths_count != "0"',
                        "action": {"type": "prompt", "message": "Add the missing tests first."},
                        "continue": True,
                        "once": True,
                    },
                    {
                        "id": "block-write",
                        "event": "pre_tool_use",
                        "if": 'same_basename_conflict == "true"',
                        "action": {"type": "prompt", "message": "Do not create same-basename files elsewhere."},
                        "reject": True,
                        "on_error": "reject",
                        "error_message": "Policy hook failed: $ERROR",
                    },
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "WORKDIR", tmp_path)

    hooks = load_configured_hooks_from_settings(settings_dir / "settings.json")

    assert [hook.id for hook in hooks] == ["reopen-missing-tests", "block-write"]
    assert hooks[0].event == "no_tool_response"
    assert hooks[0].continue_run is True
    assert hooks[0].once is True
    assert hooks[1].reject is True
    assert hooks[1].on_error == "reject"


def test_base_context_tracks_requested_path_conflict_and_missing_tests():
    from nz_coder.runtime.verification.hooks import AgentHooks

    loop = _stub_loop(
        requested_paths=["src/app.py", "tests/test_app.py"],
        actual_output_paths=["src/app.py"],
        modified_files=["src/app.py"],
        wants_tests=True,
        tests_modified=False,
    )
    hooks = AgentHooks()

    ctx = hooks._base_context(
        loop,
        [{"role": "user", "content": "update src/app.py and add tests/test_app.py"}],
        "pre_tool_use",
        file_path="other/app.py",
        tool_name="write_file",
        tool_args={"path": "other/app.py"},
        is_write=True,
    )

    assert ctx.same_basename_conflict is True
    assert ctx.conflicting_requested_path == "src/app.py"
    assert ctx.requested_path_exact_match is False
    assert ctx.requested_basename_match is True
    assert ctx.missing_requested_test_paths == ["tests/test_app.py"]
    assert ctx.get_field("missing_requested_test_paths_count") == "1"
    assert ctx.get_field("wants_tests") == "true"


def test_default_hooks_finish_first_non_tool_response_like_infcode():
    from nz_coder.runtime.verification.hooks import build_default_hooks

    calls = []

    def _check_reflection(messages, status, content_text):
        calls.append((status, content_text, len(messages)))
        return status

    loop = SimpleNamespace(
        vm=SimpleNamespace(should_gate=lambda: False),
        tracer=SimpleNamespace(log=lambda *args, **kwargs: None),
        _check_reflection_gate=_check_reflection,
    )
    hooks = build_default_hooks()

    status = hooks.handle_no_tool_response(
        loop,
        [{"role": "assistant", "content": "done"}],
        message="done",
    )

    assert status == "completed"
    assert calls == []
    assert hooks.before_no_tool_response_hooks == []


def test_stop_hook_reanimates_with_isolated_snapshot_and_bounded_context():
    from nz_coder.protocol.message_schema import SYNTHETIC_USER_KEY
    from nz_coder.runtime.verification.hooks import AgentHooks

    seen = []

    def _stop_hook(context):
        seen.append(context)
        context.transcript[0]["content"] = "mutated snapshot"
        return {"reanimate": "Run the focused regression test.", "source": "test-policy"}

    loop = _stub_loop()
    messages = [{"role": "assistant", "content": "done"}]
    hooks = AgentHooks(stop_hooks=[_stop_hook])

    status = hooks.handle_no_tool_response(loop, messages, message="done")

    assert status == "continue"
    assert messages[0]["content"] == "done"
    assert seen[0].last_assistant_text == "done"
    assert seen[0].signal == "natural-end"
    assert seen[0].reanimate_count == 0
    assert seen[0].reanimate_budget == 2
    assert messages[-1][SYNTHETIC_USER_KEY] is True
    assert messages[-1]["_nz_stop_hook"] is True
    assert "Run the focused regression test." in messages[-1]["content"]


def test_stop_hook_abort_and_reanimate_budget_are_explicit():
    from nz_coder.runtime.verification.hooks import AgentHooks

    loop = _stub_loop()
    messages = [{"role": "assistant", "content": "done"}]
    aborting = AgentHooks(
        stop_hooks=[lambda _context: {"abort": True, "reason": "Policy rejected completion."}]
    )

    assert aborting.handle_no_tool_response(loop, messages, message="done") == "stopped_by_hook"
    assert aborting.stop_hook_reason == "Policy rejected completion."

    bounded = AgentHooks(stop_hooks=[lambda _context: "Check once more."], stop_hook_reanimate_budget=1)
    assert bounded.handle_no_tool_response(loop, messages, message="done") == "continue"
    assert bounded.handle_no_tool_response(loop, messages, message="done again") == "stopped_by_hook"
    assert bounded.stop_hook_reason == "Stop-hook reanimate budget exhausted (1/1)."

    bounded.reset_run_state()
    assert bounded.stop_hook_reason == ""
    assert bounded.handle_no_tool_response(loop, messages, message="new run") == "continue"


def test_stop_hook_exception_and_invalid_shape_fail_open_with_trace():
    from nz_coder.runtime.verification.hooks import AgentHooks

    loop = _stub_loop()
    events = []
    loop.tracer = SimpleNamespace(log=lambda event, **payload: events.append((event, payload)))

    def broken(_context):
        raise RuntimeError("sidecar unavailable")

    exception_hooks = AgentHooks(stop_hooks=[broken])
    invalid_hooks = AgentHooks(stop_hooks=[lambda _context: {"unknown": True}])

    assert exception_hooks.handle_no_tool_response(
        loop, [{"role": "assistant", "content": "done"}], message="done"
    ) == "completed"
    assert invalid_hooks.handle_no_tool_response(
        loop, [{"role": "assistant", "content": "done"}], message="done"
    ) == "completed"
    assert [event for event, _payload in events] == ["stop_hook_error", "stop_hook_error"]


def test_async_stop_hook_awaits_revise_and_preserves_snapshot_isolation():
    """Catches coroutine decisions being normalized before they are awaited."""
    import asyncio

    from nz_coder.protocol.message_schema import SYNTHETIC_USER_KEY
    from nz_coder.runtime.verification.hooks import AgentHooks, StopHookDecision

    seen = []

    async def verifier(context):
        seen.append(context)
        context.transcript[0]["content"] = "mutated snapshot"
        await asyncio.sleep(0)
        return StopHookDecision(
            action="reanimate",
            message="Add the missing import.",
            source="sidecar-verifier",
        )

    messages = [{"role": "assistant", "content": "done"}]
    hooks = AgentHooks(stop_hooks=[verifier])

    status = asyncio.run(hooks.handle_no_tool_response_async(
        _stub_loop(),
        messages,
        message="done",
    ))

    assert status == "continue"
    assert messages[0]["content"] == "done"
    assert seen[0].last_assistant_text == "done"
    assert messages[-1][SYNTHETIC_USER_KEY] is True
    assert messages[-1]["_nz_stop_hook"] is True
    assert "Add the missing import." in messages[-1]["content"]


def test_async_stop_hook_exception_fails_open_to_later_consumer():
    """Catches a broken verifier preventing the deterministic next hook."""
    import asyncio

    from nz_coder.runtime.verification.hooks import AgentHooks

    events = []
    loop = _stub_loop()
    loop.tracer = SimpleNamespace(log=lambda event, **payload: events.append((event, payload)))

    async def broken(_context):
        raise RuntimeError("verifier offline")

    hooks = AgentHooks(stop_hooks=[broken, lambda _context: "Run verification."])
    status = asyncio.run(hooks.handle_no_tool_response_async(
        loop,
        [{"role": "assistant", "content": "done"}],
        message="done",
    ))

    assert status == "continue"
    assert [event for event, _payload in events] == [
        "stop_hook_error",
        "stop_hook_reanimate",
    ]


def test_hook_error_policy_prompt_queues_fallback_message(monkeypatch):
    from nz_coder.runtime.verification.hooks import AgentHooks, ConfiguredHook, HookAction

    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    hooks = AgentHooks(
        configured_hooks=[
            ConfiguredHook(
                id="broken-hook",
                event="turn_start",
                action=HookAction(type="prompt", message="Keep going."),
                on_error="prompt",
                error_message="Hook failed cleanly: $ERROR",
            )
        ]
    )
    monkeypatch.setattr(AgentHooks, "_render_hook_message", _raise)

    hooks.on_turn_start(_stub_loop(), [{"role": "user", "content": "fix it"}])

    assert hooks.consume_prompt_messages() == ["Hook failed cleanly: boom"]


def test_pre_tool_hook_error_policy_reject_blocks_tool(monkeypatch):
    from nz_coder.runtime.verification.hooks import AgentHooks, ConfiguredHook, HookAction

    def _raise(*args, **kwargs):
        raise RuntimeError("bad render")

    hooks = AgentHooks(
        configured_hooks=[
            ConfiguredHook(
                id="reject-on-error",
                event="pre_tool_use",
                action=HookAction(type="prompt", message="Block this tool."),
                on_error="reject",
                error_message="Hook crashed: $ERROR",
            )
        ]
    )
    monkeypatch.setattr(AgentHooks, "_render_hook_message", _raise)

    decision = hooks.before_tool_use(
        _stub_loop(),
        [{"role": "user", "content": "fix it"}],
        "write_file",
        {"path": "src/app.py"},
        file_path="src/app.py",
        is_write=True,
    )

    assert decision is not None
    assert decision.rejected is True
    assert decision.hook_id == "reject-on-error"
    assert decision.message == "Hook crashed: bad render"


def test_hook_failure_is_written_to_trace(tmp_path, monkeypatch):
    from nz_coder.runtime.verification.hooks import AgentHooks, ConfiguredHook, HookAction
    from nz_coder.state.trace import TraceRecorder

    def _raise(*args, **kwargs):
        raise RuntimeError("bad render")

    tracer = TraceRecorder(trace_dir=tmp_path / "traces", enabled=True)
    hooks = AgentHooks(
        configured_hooks=[
            ConfiguredHook(
                id="trace-fail",
                event="pre_tool_use",
                action=HookAction(type="prompt", message="Block this tool."),
                on_error="reject",
                error_message="Hook crashed: $ERROR",
            )
        ]
    )
    monkeypatch.setattr(AgentHooks, "_render_hook_message", _raise)

    decision = hooks.before_tool_use(
        _stub_loop(tracer=tracer),
        [{"role": "user", "content": "fix it"}],
        "write_file",
        {"path": "src/app.py"},
        file_path="src/app.py",
        is_write=True,
    )

    text = tracer.path.read_text(encoding="utf-8")
    assert decision is not None and decision.rejected is True
    assert '"event": "hook_failed"' in text
    assert '"hook_event": "pre_tool_use"' in text
    assert '"decision": "reject"' in text
