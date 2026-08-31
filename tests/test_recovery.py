"""Tests for API recovery and consecutive tool-call loop protection."""

import pytest

from nz_coder.runtime.verification.recovery import RecoveryState, is_context_overflow_error


class _APIError(RuntimeError):
    def __init__(self, message: str, status_code: int, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}


def test_retry_policy_rejects_auth_and_context_errors_but_accepts_transient_statuses():
    assert not RecoveryState.is_retryable(_APIError("unauthorized", 401))
    assert not RecoveryState.is_retryable(RuntimeError("context length overflow"))
    assert RecoveryState.is_retryable(_APIError("overloaded", 503))
    assert RecoveryState.is_retryable(_APIError("rate limit", 429))


def test_context_overflow_classifier_excludes_ordinary_bad_request():
    assert is_context_overflow_error(
        RuntimeError("context_length_exceeded: maximum context length is 8192 tokens")
    )
    assert is_context_overflow_error("Input exceeds context window of this model")
    assert not is_context_overflow_error("invalid json in tool arguments")


def test_retry_delay_honors_provider_headers():
    milliseconds = _APIError("rate limit", 429, {"Retry-After-Ms": "1500"})
    seconds = _APIError("rate limit", 429, {"Retry-After": "3"})

    assert RecoveryState.retry_after_seconds(milliseconds) == 1.5
    assert RecoveryState.retry_after_seconds(seconds) == 3.0


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "broken"])
def test_retry_delay_ignores_nonpositive_or_nonfinite_headers(value):
    """Malformed provider headers must fall back instead of stalling a run."""
    error = _APIError("rate limit", 429, {"Retry-After": value})

    assert RecoveryState.retry_after_seconds(error) is None


def test_retry_delay_caps_absurd_provider_headers():
    """A provider cannot suspend an interactive run indefinitely."""
    seconds = _APIError("rate limit", 429, {"Retry-After": "3600"})
    milliseconds = _APIError(
        "rate limit", 429, {"Retry-After-Ms": "3600000"},
    )

    assert RecoveryState.retry_after_seconds(seconds) == 120.0
    assert RecoveryState.retry_after_seconds(milliseconds) == 120.0


def test_observe_tool_call_counts_canonical_equivalent_arguments():
    state = RecoveryState()

    first = state.observe_tool_call("read_file", {"path": "app.py", "start": 1}, threshold=3)
    second = state.observe_tool_call("read_file", {"start": 1, "path": "app.py"}, threshold=3)
    third = state.observe_tool_call("read_file", {"path": "app.py", "start": 1}, threshold=3)

    assert first == {"count": 1, "should_block": False}
    assert second == {"count": 2, "should_block": False}
    assert third == {"count": 3, "should_block": True}


def test_observe_tool_call_resets_after_different_call():
    state = RecoveryState()

    state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)
    state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)
    different = state.observe_tool_call("grep_search", {"query": "run"}, threshold=3)
    next_read = state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)

    assert different == {"count": 1, "should_block": False}
    assert next_read == {"count": 1, "should_block": False}


def test_observe_tool_call_does_not_block_nonconsecutive_calls():
    state = RecoveryState()

    observations = [
        state.observe_tool_call("read_file", {"path": path}, threshold=3)
        for path in ("app.py", "other.py", "app.py", "third.py", "app.py")
    ]

    assert all(item == {"count": 1, "should_block": False} for item in observations)


def test_reset_tool_call_history_allows_reread_after_workspace_change():
    state = RecoveryState()
    for path in ("app.py", "other.py", "app.py", "third.py"):
        state.observe_tool_call("read_file", {"path": path}, threshold=3)

    state.reset_tool_call_history(reason="workspace_changed")
    reread = state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)

    assert reread == {"count": 1, "should_block": False}


def test_agent_guard_allows_rotating_read_file_cycle():
    from nz_coder.loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.recovery = RecoveryState()

    def observe(call_id: str, path: str):
        return agent._find_repeated_tool_calls([{
            "id": call_id,
            "function": {
                "name": "read_file",
                "arguments": {"path": path},
            },
        }])

    assert observe("1", "app.py") == {}
    assert observe("2", "other.py") == {}
    assert observe("3", "app.py") == {}
    assert observe("4", "third.py") == {}
    assert observe("5", "app.py") == {}


def test_reset_tool_call_history_starts_a_fresh_streak():
    state = RecoveryState()
    state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)
    state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)

    state.reset_tool_call_history()
    fresh = state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)

    assert fresh == {"count": 1, "should_block": False}


def test_observe_tool_call_can_be_disabled_and_reset():
    state = RecoveryState()
    state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)
    state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)

    disabled = state.observe_tool_call("read_file", {"path": "app.py"}, threshold=0)
    reset_event = state.consume_tool_streak_event()
    enabled_again = state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)

    assert disabled == {"count": 0, "should_block": False}
    assert reset_event is not None
    assert reset_event["reason"] == "guard_disabled"
    assert reset_event["previous_count"] == 2
    assert enabled_again == {"count": 1, "should_block": False}


def test_doom_loop_diagnostic_requires_a_different_conservative_approach():
    state = RecoveryState()

    diagnostic = state.tool_failure_diagnostic(
        "edit_file",
        "Denied: Doom loop detected: identical call repeated 3 times.",
    )

    assert "<doom-loop-diagnostic>" in diagnostic
    assert "Do not submit the same call again" in diagnostic
    assert "preserve public APIs" in diagnostic
    assert "smallest evidence-backed change" in diagnostic


def test_old_text_failure_offers_safe_eof_append_for_additive_changes():
    state = RecoveryState()

    diagnostic = state.tool_failure_diagnostic(
        "apply_patch",
        "Error: change 0 old_text not found in tests/test_app.py",
    )

    assert "copy the exact current snippet" in diagnostic
    assert "op=append" in diagnostic
    assert "only adding new content at end-of-file" in diagnostic


def test_import_failure_diagnostic_limits_environment_probe_before_source_changes():
    state = RecoveryState()

    diagnostic = state.tool_failure_diagnostic(
        "bash",
        "Command exited with code 2\n"
        "ImportError while importing test module '/tmp/work/tests/test_parser.py'.\n"
        "ModuleNotFoundError: No module named 'cron_engine'",
    )

    assert "classification: import_or_package_layout" in diagnostic
    assert "one minimal workspace-local probe" in diagnostic
    assert "Do not install packages" in diagnostic
    assert "Do not change source code unless the probe proves" in diagnostic
    assert "stop after that probe" in diagnostic


def test_subprocess_package_root_failure_targets_test_helper_not_global_environment():
    state = RecoveryState()

    diagnostic = state.tool_failure_diagnostic(
        "bash",
        "Command exited with code 1\n"
        "FAILED cron_engine/tests/test_cli.py::TestCLIParse::test_parse_wildcard\n"
        "CompletedProcess(args=['python', '-m', 'cron_engine'], returncode=1, "
        "stderr='python: No module named cron_engine\\n')",
    )

    assert "classification: subprocess_package_root" in diagnostic
    assert "subprocess helper" in diagnostic
    assert "directory that contains the package" in diagnostic
    assert "Active workspace root:" in diagnostic
    assert "package directory: `cron_engine`" in diagnostic
    assert "repair_target: cron_engine/tests/test_cli.py" in diagnostic
    assert "Use that exact workspace root as `cwd`" in diagnostic
    assert "Do not inspect global environments" in diagnostic


def test_subprocess_package_root_failure_gives_exact_portable_workspace_expression(
    tmp_path,
):
    """A failed module subprocess gets a literal helper-relative cwd repair."""
    from nz_coder.runtime.process.workdir import scoped_workdir

    state = RecoveryState()
    with scoped_workdir(tmp_path):
        diagnostic = state.tool_failure_diagnostic(
            "bash",
            "Command exited with code 1\n"
            "FAILED cron_engine/tests/test_cli.py::TestCLIParse::test_parse_wildcard\n"
            "CompletedProcess(args=['python', '-m', 'cron_engine'], returncode=1, "
            "stderr='python: No module named cron_engine\\n')",
        )

    assert "Path(__file__).resolve().parents[2]" in diagnostic
    assert f"resolves to `{tmp_path.resolve()}`" in diagnostic


def test_subprocess_workspace_drift_reports_static_old_cwd(tmp_path):
    """G5: successful stale-package imports are diagnosed from helper AST."""
    from nz_coder.runtime.process.workdir import scoped_workdir

    old_root = tmp_path.parent / "old-cron-fixture"
    old_root.mkdir(exist_ok=True)
    helper = tmp_path / "cron_engine" / "tests" / "test_cli.py"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "from pathlib import Path\n"
        "import subprocess, sys\n"
        f"STALE_ROOT = Path({str(old_root)!r})\n"
        "def test_cli():\n"
        "    result = subprocess.run([sys.executable, '-m', 'cron_engine'], "
        "cwd=STALE_ROOT, capture_output=True)\n"
        "    assert result.returncode == 0\n",
        encoding="utf-8",
    )
    state = RecoveryState()

    with scoped_workdir(tmp_path):
        diagnostic = state.tool_failure_diagnostic(
            "bash",
            "Command exited with code 1\n"
            "FAILED cron_engine/tests/test_cli.py::test_cli - AssertionError",
        )

    assert "classification: subprocess_workspace_drift" in diagnostic
    assert "cron_engine/tests/test_cli.py" in diagnostic
    assert str(old_root) in diagnostic
    assert str(tmp_path) in diagnostic
    assert "package: `cron_engine`" in diagnostic
    assert "pip install" not in diagnostic.casefold()
    assert "production source" not in diagnostic.casefold()


def test_subprocess_workspace_drift_gives_exact_portable_workspace_expression(tmp_path):
    """A nested helper must not make the model guess its parent depth."""
    from nz_coder.runtime.process.workdir import scoped_workdir

    old_root = tmp_path.parent / "old-portable-fixture"
    old_root.mkdir(exist_ok=True)
    helper = tmp_path / "cron_engine" / "tests" / "test_cli.py"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "import subprocess, sys\n"
        f"OLD_ROOT = {str(old_root)!r}\n"
        "def test_cli():\n"
        "    result = subprocess.run([sys.executable, '-m', 'cron_engine'], "
        "cwd=OLD_ROOT, capture_output=True)\n"
        "    assert result.returncode == 0\n",
        encoding="utf-8",
    )
    state = RecoveryState()

    with scoped_workdir(tmp_path):
        diagnostic = state.tool_failure_diagnostic(
            "bash",
            "Command exited with code 1\n"
            "FAILED cron_engine/tests/test_cli.py::test_cli - AssertionError",
        )

    assert "Path(__file__).resolve().parents[2]" in diagnostic
    assert f"resolves to `{tmp_path.resolve()}`" in diagnostic
    assert "do not translate it into guessed dirname calls" in diagnostic


def test_workspace_drift_is_primary_while_widespread_regression_is_preserved(tmp_path):
    """A resolved stale helper outranks but does not erase broad evidence."""
    from nz_coder.runtime.process.workdir import scoped_workdir

    old_root = tmp_path.parent / "old-wide-fixture"
    old_root.mkdir(exist_ok=True)
    helper = tmp_path / "cron_engine" / "tests" / "test_cli.py"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "from pathlib import Path\n"
        "import subprocess, sys\n"
        f"OLD_ROOT = Path({str(old_root)!r})\n"
        "def test_cli():\n"
        "    subprocess.run([sys.executable, '-m', 'cron_engine'], cwd=OLD_ROOT)\n",
        encoding="utf-8",
    )
    state = RecoveryState()
    output = (
        "Command exited with code 1\n"
        "FAILED cron_engine/tests/test_cli.py::test_cli - AssertionError\n"
        "FAILED cron_engine/tests/test_parser.py::test_numeric - AssertionError\n"
        "FAILED cron_engine/tests/test_scheduler.py::test_next - AssertionError\n"
    )

    with scoped_workdir(tmp_path):
        diagnostic = state.tool_failure_diagnostic("bash", output)

    assert "primary_classification: subprocess_workspace_drift" in diagnostic
    assert "supporting_classification: widespread_test_regression" in diagnostic
    assert "repair_target: cron_engine/tests/test_cli.py" in diagnostic
    assert "Update that helper's `cwd`" in diagnostic
    assert "Do not patch individual test helpers" not in diagnostic


def test_widespread_regression_remains_primary_without_specific_signal():
    state = RecoveryState()
    output = (
        "Command exited with code 1\n"
        "FAILED pkg/tests/test_api.py::test_one - AssertionError\n"
        "FAILED pkg/tests/test_parser.py::test_two - AssertionError\n"
    )

    diagnostic = state.tool_failure_diagnostic("bash", output)

    assert "primary_classification: widespread_test_regression" in diagnostic
    assert "supporting_classification:" not in diagnostic
    assert "shared production code" in diagnostic


def test_subprocess_workspace_drift_resolves_path_parent_expression(tmp_path):
    """The static resolver handles Path(__file__).parent composition safely."""
    from nz_coder.intelligence.subprocess_workspace import (
        diagnose_subprocess_workspace_drift,
    )

    helper_root = tmp_path / "fixture-copy"
    helper = helper_root / "tests" / "test_cli.py"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "from pathlib import Path\n"
        "import subprocess, sys\n"
        "OLD = Path(__file__).parent / 'old-root'\n"
        "def test_cli():\n"
        "    subprocess.check_call([sys.executable, '-m', 'cron_engine'], cwd=OLD)\n",
        encoding="utf-8",
    )

    result = diagnose_subprocess_workspace_drift(
        "FAILED fixture-copy/tests/test_cli.py::test_cli",
        workspace=tmp_path,
    )

    assert result is not None
    assert result.resolved_cwd == (helper.parent / "old-root").resolve()
    assert result.package == "cron_engine"


def test_django_pytest_bootstrap_failure_redirects_to_native_runner(tmp_path):
    """A wrong generic runner must not trigger production-source debugging."""
    from nz_coder.runtime.process.workdir import scoped_workdir

    runner = tmp_path / "tests" / "runtests.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("# Django native test runner\n", encoding="utf-8")
    state = RecoveryState()
    output = (
        "Command exited with code 1\n"
        "django/conf/__init__.py:203: AttributeError\n"
        "AttributeError: 'object' object has no attribute 'DATABASES'\n"
    )

    with scoped_workdir(tmp_path):
        diagnostic = state.tool_failure_diagnostic(
            "bash",
            output,
            tool_input={
                "command": (
                    "python3 -m pytest "
                    "tests/forms_tests/tests/test_media.py -q 2>&1 | tail -20"
                )
            },
        )

    assert "classification: repository_test_runner_mismatch" in diagnostic
    assert (
        "python tests/runtests.py forms_tests.tests.test_media"
        in diagnostic
    )
    assert "Inspect the implicated source file" not in diagnostic


def test_django_checkout_import_failure_retries_with_local_pythonpath(tmp_path):
    """A repository-owned Django package is recoverable without installation."""
    from nz_coder.runtime.process.workdir import scoped_workdir

    (tmp_path / "django").mkdir()
    (tmp_path / "django" / "__init__.py").write_text("", encoding="utf-8")
    runner = tmp_path / "tests" / "runtests.py"
    runner.parent.mkdir()
    runner.write_text("# Django native test runner\n", encoding="utf-8")
    state = RecoveryState()

    with scoped_workdir(tmp_path):
        diagnostic = state.tool_failure_diagnostic(
            "bash",
            "Command exited with code 1\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/runtests.py\", line 14, in <module>\n"
            "    import django\n"
            "ModuleNotFoundError: No module named 'django'",
            tool_input={
                "command": (
                    "python3 tests/runtests.py "
                    "auth_tests.test_migrations -v1"
                )
            },
        )

    assert "primary_classification: repository_package_path" in diagnostic
    assert (
        "PYTHONPATH=. python3 tests/runtests.py "
        "auth_tests.test_migrations -v1"
    ) in diagnostic
    assert "install" not in diagnostic.casefold()


def test_django_parallel_runtime_mismatch_retries_serially(tmp_path):
    """Host unittest incompatibility must preserve scope and disable parallelism."""
    from nz_coder.runtime.process.workdir import scoped_workdir

    (tmp_path / "django").mkdir()
    (tmp_path / "django" / "__init__.py").write_text("", encoding="utf-8")
    runner = tmp_path / "tests" / "runtests.py"
    runner.parent.mkdir()
    runner.write_text("# Django native test runner\n", encoding="utf-8")
    state = RecoveryState()

    with scoped_workdir(tmp_path):
        diagnostic = state.tool_failure_diagnostic(
            "bash",
            "Command exited with code 1\n"
            "RuntimeWarning: TestResult has no addDuration method",
            tool_input={
                "command": (
                    "PYTHONPATH=. python3 tests/runtests.py "
                    "auth_tests.test_migrations -v1"
                )
            },
        )

    assert "primary_classification: repository_runner_parallelism" in diagnostic
    assert (
        "PYTHONPATH=. python3 tests/runtests.py "
        "auth_tests.test_migrations -v1 --parallel 1"
    ) in diagnostic


@pytest.mark.parametrize(
    "unsafe_target",
    ("../outside", "/tmp/outside", "~/outside", "--pattern=../outside"),
)
def test_django_runner_recovery_rejects_outside_workspace_tokens(
    tmp_path,
    unsafe_target,
):
    """Recovery guidance must not replay a selector or option path escape."""
    from nz_coder.runtime.verification.recovery import repository_test_runner_recovery_command
    from nz_coder.runtime.process.workdir import scoped_workdir

    (tmp_path / "django").mkdir()
    (tmp_path / "django" / "__init__.py").write_text("", encoding="utf-8")
    runner = tmp_path / "tests" / "runtests.py"
    runner.parent.mkdir()
    runner.write_text("# Django native test runner\n", encoding="utf-8")

    with scoped_workdir(tmp_path):
        command = repository_test_runner_recovery_command(
            "bash",
            "ModuleNotFoundError: No module named 'django'",
            tool_input={
                "command": f"python3 tests/runtests.py {unsafe_target} -v1",
            },
        )

    assert command == ""


@pytest.mark.parametrize("external_component", ("package", "runner"))
def test_django_runner_recovery_rejects_external_symlink_targets(
    tmp_path,
    external_component,
):
    """Repository recovery evidence must resolve inside the active workspace."""
    from nz_coder.runtime.verification.recovery import repository_test_runner_recovery_command
    from nz_coder.runtime.process.workdir import scoped_workdir

    external = tmp_path.parent / f"{tmp_path.name}-{external_component}"
    external.mkdir()
    if external_component == "package":
        external_package = external / "django"
        external_package.mkdir()
        (external_package / "__init__.py").write_text("", encoding="utf-8")
        (tmp_path / "django").symlink_to(
            external_package,
            target_is_directory=True,
        )
        runner = tmp_path / "tests" / "runtests.py"
        runner.parent.mkdir()
        runner.write_text("# Django native test runner\n", encoding="utf-8")
    else:
        (tmp_path / "django").mkdir()
        (tmp_path / "django" / "__init__.py").write_text("", encoding="utf-8")
        external_runner = external / "runtests.py"
        external_runner.write_text("# External runner\n", encoding="utf-8")
        local_tests = tmp_path / "tests"
        local_tests.mkdir()
        (local_tests / "runtests.py").symlink_to(external_runner)

    with scoped_workdir(tmp_path):
        command = repository_test_runner_recovery_command(
            "bash",
            "ModuleNotFoundError: No module named 'django'",
            tool_input={
                "command": (
                    "python3 tests/runtests.py "
                    "auth_tests.test_migrations -v1"
                ),
            },
        )

    assert command == ""


def test_workspace_escape_diagnostic_says_to_use_default_workspace():
    state = RecoveryState()

    diagnostic = state.tool_failure_diagnostic(
        "bash",
        "Error: workdir escapes workspace",
    )

    assert "classification: workspace_boundary" in diagnostic
    assert "omit `workdir`" in diagnostic
    assert "Do not inspect or target the outside path" in diagnostic


def test_denied_bash_outside_path_redirects_to_exact_active_workspace(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir

    state = RecoveryState()
    with scoped_workdir(tmp_path):
        diagnostic = state.tool_failure_diagnostic(
            "bash",
            "Denied: Blocked: path outside workspace (/stale/fixture)",
            tool_input={"command": "cd /stale/fixture && python -c 'print(1)'"},
        )

    assert "classification: workspace_boundary" in diagnostic
    assert f"Active workspace root: `{tmp_path.resolve()}`" in diagnostic
    assert "remove the explicit `cd`" in diagnostic
    assert "omit `workdir`" in diagnostic
    assert "Choose a safer, narrower command" not in diagnostic


def test_direct_python_import_from_package_directory_redirects_to_workspace_root(
    tmp_path,
):
    from nz_coder.runtime.process.workdir import scoped_workdir

    (tmp_path / "cron_engine").mkdir()
    state = RecoveryState()
    with scoped_workdir(tmp_path):
        diagnostic = state.tool_failure_diagnostic(
            "bash",
            "Command exited with code 1\n"
            "Traceback (most recent call last):\n"
            "  File \"<string>\", line 1, in <module>\n"
            "ModuleNotFoundError: No module named 'cron_engine'",
            tool_input={
                "command": "python -c 'from cron_engine.parser import parse'",
                "workdir": "cron_engine",
            },
        )

    assert "classification: command_package_root" in diagnostic
    assert f"Active workspace root: `{tmp_path.resolve()}`" in diagnostic
    assert "Detected package directory: `cron_engine`" in diagnostic
    assert "omit `workdir`" in diagnostic
    assert "Do not inspect source files" in diagnostic
    assert "Inspect the implicated source file" not in diagnostic


def test_missing_read_uses_unique_declared_artifact_instead_of_broad_search():
    """A contract-owned basename must recover directly without globbing."""
    state = RecoveryState()

    diagnostic = state.tool_failure_diagnostic(
        "read_file",
        "Error: File not found: README.md",
        tool_input={"path": "README.md"},
        declared_paths=(
            "cron_engine/parser.py",
            "cron_engine/README.md",
        ),
    )

    assert "classification: declared_artifact_path" in diagnostic
    assert "repair_target: cron_engine/README.md" in diagnostic
    assert "read_file" in diagnostic
    assert "Do not run glob" in diagnostic


def test_missing_read_does_not_guess_between_ambiguous_declared_artifacts():
    """Two same-basename artifacts cannot justify a deterministic redirect."""
    state = RecoveryState()

    diagnostic = state.tool_failure_diagnostic(
        "read_file",
        "Error: File not found: README.md",
        tool_input={"path": "README.md"},
        declared_paths=("service/README.md", "client/README.md"),
    )

    assert "classification: declared_artifact_path" not in diagnostic
    assert "repair_target:" not in diagnostic


def test_agent_doom_loop_permission_can_approve_exact_repeat():
    from nz_coder.loop import AgentLoop

    agent = AgentLoop(
        "test",
        permission_mode="default",
        permission_asker=lambda name, payload: (
            "once" if name == "doom_loop" and payload["tool"] == "read_file" else "reject"
        ),
        client=object(),
        trace_enabled=False,
    )
    call = {
        "id": "repeat",
        "function": {"name": "read_file", "arguments": {"path": "app.py"}},
    }

    assert agent._find_repeated_tool_calls([call]) == {}
    assert agent._find_repeated_tool_calls([call]) == {}
    blocked = agent._find_repeated_tool_calls([call])

    assert 0 in blocked
    assert agent._resolve_doom_loop_permissions(blocked, [call]) == {}
    assert agent.recovery.repeated_tool_calls == 0
