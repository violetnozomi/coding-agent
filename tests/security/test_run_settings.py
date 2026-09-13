"""Security contracts for immutable run-scoped execution settings."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


def _snapshot(workspace: Path, **values):
    from nz_coder.foundation.workspace_trust import load_config_snapshot

    workspace.mkdir(exist_ok=True)
    return load_config_snapshot(
        workspace,
        environ={key: str(value) for key, value in values.items()},
        user_config_path=workspace.parent / "absent-user-config",
    )


def test_config_snapshot_is_immutable_under_concurrent_reads(tmp_path):
    snapshot = _snapshot(
        tmp_path / "project",
        BASH_TIMEOUT_SECONDS="17",
        TRACE_ENABLED="not-a-valid-boolean-sensitive-sentinel",
    )
    values_before = dict(snapshot.values)
    issues_before = snapshot.issues

    def read_snapshot(_index):
        return (
            snapshot.get_int("BASH_TIMEOUT_SECONDS", 120),
            snapshot.get_bool("TRACE_ENABLED", True),
            snapshot.public_json(),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(read_snapshot, range(100)))

    assert {(timeout, trace) for timeout, trace, _public in results} == {(17, True)}
    assert dict(snapshot.values) == values_before
    assert snapshot.issues is issues_before
    assert isinstance(snapshot.issues, tuple)
    with pytest.raises(TypeError):
        snapshot.values["BASH_TIMEOUT_SECONDS"] = snapshot.value("BASH_TIMEOUT_SECONDS")
    with pytest.raises(FrozenInstanceError):
        snapshot.workspace = tmp_path
    assert "not-a-valid-boolean-sensitive-sentinel" not in snapshot.public_json()


def test_run_settings_are_isolated_between_workspace_epochs(tmp_path):
    from nz_coder.runtime.core.run_settings import (
        RunSettings,
        current_run_settings,
        scoped_run_settings,
    )

    first = RunSettings.from_snapshot(_snapshot(
        tmp_path / "first",
        BASH_TIMEOUT_SECONDS="11",
        ALLOW_BASH_PACKAGE_INSTALLS="0",
        MAX_TOOL_CALLS_PER_RESPONSE="3",
        NZ_PROCESS_MAX_PER_WORKSPACE="2",
        NZ_WRITE_BATCH_MAX_FILE_BYTES="101",
        MAX_CONTEXT_TOKENS="9000",
    ))
    second = RunSettings.from_snapshot(_snapshot(
        tmp_path / "second",
        BASH_TIMEOUT_SECONDS="29",
        ALLOW_BASH_PACKAGE_INSTALLS="1",
        MAX_TOOL_CALLS_PER_RESPONSE="7",
        NZ_PROCESS_MAX_PER_WORKSPACE="5",
        NZ_WRITE_BATCH_MAX_FILE_BYTES="303",
        MAX_CONTEXT_TOKENS="19000",
    ))

    def observe(settings):
        with scoped_run_settings(settings):
            current = current_run_settings()
            return (
                current.bash_timeout,
                current.allow_package_installs,
                current.max_tool_calls,
                current.process_max_per_workspace,
                current.write_batch_file_bytes,
                current.max_context_tokens,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        observations = list(pool.map(observe, (first, second)))

    assert observations == [
        (11, False, 3, 2, 101, 9000),
        (29, True, 7, 5, 303, 19000),
    ]
    with pytest.raises(FrozenInstanceError):
        first.bash_timeout = 99


def test_execution_context_limits_use_bound_run_settings(tmp_path):
    from nz_coder.runtime.core.execution_context import max_agent_turns, max_parallel_tasks
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings

    settings = RunSettings.from_snapshot(_snapshot(
        tmp_path / "project",
        MAX_AGENT_TURNS="13",
        MAX_PARALLEL_TASKS="2",
    ))
    with scoped_run_settings(settings):
        assert max_agent_turns() == 13
        assert max_parallel_tasks() == 2


def test_compound_config_is_redacted_in_config_show(tmp_path, monkeypatch):
    from nz_coder.interface.config_cli import collect_effective_config

    sentinel = "compound-secret-must-not-be-public"
    workspace = tmp_path / "project"
    workspace.mkdir()
    values = {
        "NZ_MCP_SERVERS_JSON": (
            '{"private":{"command":"server","env":{"TOKEN":"'
            + sentinel + '"}}}'
        ),
        "MODEL_CAPABILITIES_JSON": (
            '{"model":{"headers":{"Authorization":"' + sentinel + '"}}}'
        ),
        "MODEL_CATALOG_JSON": (
            '{"models":{"x":{"endpoint":"https://' + sentinel + '.invalid"}}}'
        ),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    public = __import__("json").dumps(collect_effective_config(workspace))
    assert sentinel not in public
    assert public.count("<configured>") >= 3


def test_image_provider_does_not_bleed_between_workspaces(tmp_path):
    from nz_coder.capabilities.vision import ProviderImageDescriber
    from nz_coder.runtime.core.run_settings import RunSettings

    first = RunSettings.from_snapshot(_snapshot(
        tmp_path / "first-image",
        NZ_IMAGE_DESCRIBE_PROVIDER="openai-compatible",
        NZ_IMAGE_DESCRIBE_MODEL="vision-a",
        NZ_IMAGE_DESCRIBE_API_KEY="secret-a",
        NZ_IMAGE_DESCRIBE_BASE_URL="https://a.invalid/v1",
    ))
    second = RunSettings.from_snapshot(_snapshot(
        tmp_path / "second-image",
        NZ_IMAGE_DESCRIBE_PROVIDER="anthropic",
        NZ_IMAGE_DESCRIBE_MODEL="vision-b",
        NZ_IMAGE_DESCRIBE_API_KEY="secret-b",
        NZ_IMAGE_DESCRIBE_BASE_URL="https://b.invalid/v1",
    ))

    first_describer = ProviderImageDescriber.configured(run_settings=first)
    second_describer = ProviderImageDescriber.configured(run_settings=second)

    assert (
        first_describer.provider_name,
        first_describer.model_id,
        first_describer.base_url,
    ) == ("openai-compatible", "vision-a", "https://a.invalid/v1")
    assert (
        second_describer.provider_name,
        second_describer.model_id,
        second_describer.base_url,
    ) == ("anthropic", "vision-b", "https://b.invalid/v1")
    assert first_describer.api_key != second_describer.api_key


def test_bash_package_policy_uses_target_run_settings(tmp_path, monkeypatch):
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools import bash as bash_module

    denied = RunSettings.from_snapshot(_snapshot(
        tmp_path / "denied", ALLOW_BASH_PACKAGE_INSTALLS="0",
    ))
    allowed = RunSettings.from_snapshot(_snapshot(
        tmp_path / "allowed", ALLOW_BASH_PACKAGE_INSTALLS="1",
    ))
    launches = []

    def fail_launch(*_args, **_kwargs):
        launches.append(True)
        raise OSError("launch-secret-must-not-escape")

    monkeypatch.setattr(bash_module.subprocess, "Popen", fail_launch)
    with scoped_workdir(tmp_path / "denied"), scoped_run_settings(denied):
        assert "Package install blocked" in bash_module.run_bash("pip install package")
    with scoped_workdir(tmp_path / "allowed"), scoped_run_settings(allowed):
        result = bash_module.run_bash("pip install package")
        assert launches == [True]
        assert result == "Error: An internal error occurred."
        assert "launch-secret" not in result


def test_bash_timeout_uses_target_run_settings(tmp_path, monkeypatch):
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools import bash as bash_module

    short = RunSettings.from_snapshot(_snapshot(
        tmp_path / "short", BASH_TIMEOUT_SECONDS="2",
    ))
    long = RunSettings.from_snapshot(_snapshot(
        tmp_path / "long", BASH_TIMEOUT_SECONDS="20",
    ))
    launches = []

    def fail_launch(*_args, **_kwargs):
        launches.append(True)
        raise OSError("launch-secret-must-not-escape")

    monkeypatch.setattr(bash_module.subprocess, "Popen", fail_launch)
    with scoped_workdir(tmp_path / "short"), scoped_run_settings(short):
        assert "between 1 and 2s" in bash_module.run_bash("echo ok", timeout=10)
    with scoped_workdir(tmp_path / "long"), scoped_run_settings(long):
        result = bash_module.run_bash("echo ok", timeout=10)
        assert launches == [True]
        assert result == "Error: An internal error occurred."
        assert "launch-secret" not in result


def test_tool_call_limit_uses_target_run_settings(tmp_path):
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
    from nz_coder.runtime.tool_runtime.policy import ProductionToolPolicy

    calls = [
        {"function": {"name": name, "arguments": "{}"}}
        for name in ("read_file", "read_file", "write_file")
    ]
    policy = ProductionToolPolicy()
    context = object()
    limited = RunSettings.from_snapshot(_snapshot(
        tmp_path / "limited-tools", MAX_TOOL_CALLS_PER_RESPONSE="2",
    ))
    expanded = RunSettings.from_snapshot(_snapshot(
        tmp_path / "expanded-tools", MAX_TOOL_CALLS_PER_RESPONSE="3",
    ))
    with scoped_run_settings(limited):
        assert policy.tool_batch_has_write(context, calls) is False
    with scoped_run_settings(expanded):
        assert policy.tool_batch_has_write(context, calls) is True


def test_process_limits_use_target_run_settings(tmp_path):
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
    from nz_coder.runtime.process.process_service import ProcessService

    workspace = tmp_path / "process"
    first = RunSettings.from_snapshot(_snapshot(
        workspace, NZ_PROCESS_MAX_PER_WORKSPACE="2",
    ))
    second = RunSettings.from_snapshot(_snapshot(
        tmp_path / "process-config-2", NZ_PROCESS_MAX_PER_WORKSPACE="7",
    ))
    service = ProcessService(workspace)
    with scoped_run_settings(first):
        assert service._effective_max_processes() == 2
    with scoped_run_settings(second):
        assert service._effective_max_processes() == 7


def test_file_write_quota_uses_target_run_settings(tmp_path):
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import write_files_batch

    workspace = tmp_path / "files"
    restricted = RunSettings.from_snapshot(_snapshot(
        workspace,
        NZ_WRITE_BATCH_MAX_FILE_BYTES="4",
        NZ_WRITE_BATCH_MAX_TOTAL_BYTES="8",
    ))
    permissive = RunSettings.from_snapshot(_snapshot(
        tmp_path / "files-config-2",
        NZ_WRITE_BATCH_MAX_FILE_BYTES="40",
        NZ_WRITE_BATCH_MAX_TOTAL_BYTES="80",
    ))
    request = [{"path": "result.txt", "content": "ten-bytes!"}]
    with scoped_workdir(workspace), scoped_run_settings(restricted):
        assert str(write_files_batch(request)).startswith("Error: file too large")
    with scoped_workdir(workspace), scoped_run_settings(permissive):
        assert not str(write_files_batch(request)).startswith("Error:")


def test_prompt_budget_uses_target_run_settings(tmp_path):
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
    from nz_coder.state.context import prompt_budget

    settings = RunSettings.from_snapshot(_snapshot(
        tmp_path / "budget",
        MAX_CONTEXT_TOKENS="12000",
        MAX_OUTPUT_TOKENS="2000",
    ))
    with scoped_run_settings(settings):
        budget = prompt_budget()
    assert budget.context_tokens == 12_000
    assert budget.output_reserve_tokens == 2_000


def test_planning_and_reflection_use_target_run_settings(tmp_path):
    from nz_coder.runtime.core.run_settings import RunSettings

    disabled = RunSettings.from_snapshot(_snapshot(
        tmp_path / "disabled-planning",
        NZ_PLANNING_ENABLED="0",
        NZ_REFLECTION_ENABLED="0",
    ))
    enabled = RunSettings.from_snapshot(_snapshot(
        tmp_path / "enabled-planning",
        NZ_PLANNING_ENABLED="1",
        NZ_REFLECTION_ENABLED="1",
    ))
    assert (disabled.planning_enabled, disabled.reflection_enabled) == (False, False)
    assert (enabled.planning_enabled, enabled.reflection_enabled) == (True, True)


def test_lsp_diagnostic_wait_uses_target_run_settings(tmp_path):
    from nz_coder.runtime.core.run_settings import RunSettings

    first = RunSettings.from_snapshot(_snapshot(
        tmp_path / "first-lsp", NZ_LSP_DIAGNOSTIC_WAIT_SECONDS="0.25",
    ))
    second = RunSettings.from_snapshot(_snapshot(
        tmp_path / "second-lsp", NZ_LSP_DIAGNOSTIC_WAIT_SECONDS="4.5",
    ))
    assert first.lsp_diagnostic_wait == 0.25
    assert second.lsp_diagnostic_wait == 4.5
