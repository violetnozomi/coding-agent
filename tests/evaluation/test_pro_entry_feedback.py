"""Pro入口的工作目录和策略拒绝必须与实际执行条件一致。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def entry():
    path = Path(__file__).resolve().parents[2] / "docs/evidence/context-continuity-fixes-2026-10-22/native-entry.py"
    spec = importlib.util.spec_from_file_location("pro_entry_feedback", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_task_location_matches_mount_without_rewriting_issue_code(entry):
    prefix = "A code repository is available in the `/app` directory."
    issue = "\n\nRequirements: retain the literal path `/app/assets` in config.py."
    assert entry.render_task(prefix + issue, Path("/workspace")) == (
        "A code repository is available in the `/workspace` directory." + issue
    )
    assert entry.render_task("Use `/app/assets` as test data.", Path("/workspace")) == "Use `/app/assets` as test data."


def test_cd_is_rejected_with_actionable_reason_before_prompt(entry, tmp_path):
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.permissioning import PermissionManager

    asked = []
    manager = PermissionManager(mode="auto", asker=lambda *args: asked.append(args))
    original = PermissionManager.check
    log = tmp_path / "permissions.jsonl"
    with scoped_workdir(tmp_path), entry.permission_policy(log):
        result = ToolExecutor(manager).execute_one({
            "id": "cd-test", "type": "function", "function": {
                "name": "bash", "arguments": {"command": "cd . && python -m pytest tests/test_example.py"},
            },
        }, 0)
    assert not result.executed and result.permission_denied
    assert "bash.workdir" in result.output and "cd" in result.output
    assert "by user" not in result.output
    assert asked == []
    assert json.loads(log.read_text().splitlines()[0])["reason"] in result.output
    assert PermissionManager.check is original


def test_formatter_and_direct_tests_reach_existing_permission_policy(entry, tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.permissioning import PermissionManager

    with scoped_workdir(tmp_path), entry.permission_policy(tmp_path / "permissions.jsonl"):
        manager = PermissionManager(mode="auto")
        for command in ["gofmt -l file.go", "go test ./pkg", "python -m pytest tests/test_example.py"]:
            assert manager.check("bash", {"command": command})["behavior"] != "deny"


def test_entry_cannot_override_existing_deny_rule(entry, tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.permissioning import PermissionManager
    from nz_coder.tool_platform.permissioning.rules import parse_rules

    with scoped_workdir(tmp_path), entry.permission_policy(tmp_path / "permissions.jsonl"):
        manager = PermissionManager(mode="auto")
        manager._deny_rules = parse_rules(["Bash(prefix:git )"], "deny")
        decision = manager.check("bash", {"command": "git status --short"})
    assert decision["behavior"] == "deny" and "rule" in decision["reason"]


@pytest.mark.parametrize("name,arguments", [
    ("webfetch", {"url": "https://example.com"}),
    ("read_file", {"path": ".nz-coder/runtime-state.json"}),
    ("bash", {"command": "curl https://example.com"}),
])
def test_evaluation_restrictions_remain_in_force(entry, name, arguments, tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.permissioning import PermissionManager

    with scoped_workdir(tmp_path), entry.permission_policy(tmp_path / "permissions.jsonl"):
        decision = PermissionManager(mode="auto").check(name, arguments)
    assert decision["behavior"] == "deny" and decision["reason"]


def test_formal_entry_uses_rendered_task_and_policy_during_run(entry, tmp_path, monkeypatch):
    """替换SDK执行出口，检查正式main接线；不发模型请求。"""
    from dataclasses import dataclass
    from enum import Enum
    from types import SimpleNamespace
    from nz_coder.tool_platform.permissioning import PermissionManager

    workspace = tmp_path / "workspace"
    output = tmp_path / "result"
    workspace.mkdir()
    output.mkdir()
    config_path = tmp_path / "run.json"
    config_path.write_text(json.dumps({
        "task": "A code repository is available in the `/app` directory.\nKeep /app/assets as data.",
        "model": "fake-local", "session_id": "entry-wiring", "max_turns": 2,
    }))
    paths = {"/input/run.json": config_path, "/result": output, "/workspace": workspace}
    monkeypatch.setattr(entry, "Path", lambda value: paths.get(str(value), Path(value)))
    monkeypatch.setenv("PYTHONPATH", "agent-bootstrap-only")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    # main中的替换只属于该容器；测试结束后还原宿主模块。
    for module in (entry.native_sdk, entry.loop, entry.gateway):
        monkeypatch.setattr(module, "resolve_model_runtime", module.resolve_model_runtime)
    monkeypatch.setattr(entry.gateway.ProductionModelGateway, "_request_kwargs",
                        entry.gateway.ProductionModelGateway._request_kwargs)
    clients = []
    client_type = entry.OpenAI

    def create_client(**kwargs):
        client = client_type(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(entry, "OpenAI", create_client)
    captured = {}
    trace = tmp_path / "trace.jsonl"
    trace.write_text("")
    environment = SimpleNamespace(
        runtime_state=SimpleNamespace(to_dict=lambda: {}),
        tracer=SimpleNamespace(path=trace), close=lambda: None,
        environment_cleanup_complete=True, environment_cleanup_failures={},
    )

    def build(request, options):
        captured["request"] = request
        return environment

    class Status(Enum):
        COMPLETED = "completed"

    @dataclass
    class Result:
        status: Status = Status.COMPLETED
        error: str = ""

    class Runner:
        def __init__(self, _environment):
            pass

        async def run_result(self, request, options):
            captured["decision"] = PermissionManager(mode="auto").check(
                "bash", {"command": "cd . && echo ready"},
            )
            return Result()

    monkeypatch.setattr(entry.native_sdk, "build_product_run_environment", build)
    monkeypatch.setattr(entry.native_sdk, "NativeSDKRunner", Runner)
    original_policy = PermissionManager.check
    try:
        entry.main()
    finally:
        for client in clients:
            client.close()
    request = captured["request"]
    assert request.workspace == workspace
    assert f"`{workspace}`" in request.messages[0]["content"]
    assert "Keep /app/assets as data." in request.messages[0]["content"]
    assert str(workspace) in request.agent.instructions
    assert captured["decision"]["behavior"] == "deny"
    assert "bash.workdir" in captured["decision"]["reason"]
    assert json.loads((output / "task-rendering.json").read_text())["location_preamble_changed"]
    assert PermissionManager.check is original_policy
