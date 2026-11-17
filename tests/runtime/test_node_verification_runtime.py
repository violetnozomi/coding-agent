"""Offline production Runner regressions for declared Node test evidence."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shutil
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunOptions, RunRequest
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.model_gateway import ResolvedModelRuntime


@pytest.fixture
def node_project(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node >=18 is required for a real node:test subprocess")
    version = subprocess.check_output([node, "--version"], text=True).strip()
    if int(version.lstrip("v").split(".")[0]) < 18:
        pytest.skip(f"Node >=18 required; local runtime is {version}")
    (tmp_path / "index.js").write_text("module.exports = x => x.replace(/-/g, '\\\\-');\n")
    (tmp_path / "literal.test.cjs").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert/strict');\n"
        "const escape = require('./index.js');\n"
        "test('Unicode hyphen', () => {\n"
        "  assert.equal(new RegExp('^' + escape('a-b') + '$', 'u').test('a-b'), true);\n"
        "});\n"
    )
    return tmp_path


def _tool(name, **arguments):
    return (name, arguments)


READ = [_tool("read_file", path="index.js")]
TEST = [_tool("bash", command="node --test literal.test.cjs")]
EDIT = [_tool("edit_file", path="index.js", old_text="x.replace(/-/g, '\\\\-')", new_text="x")]


def _run(monkeypatch, workspace, actions, *, task=None, limit=None, permission=True, allowed_commands=(), planning_response=None, semantic_response="accept", prior_messages=(), stream=False):
    """Replace only model/network boundaries, retain production state and tools."""
    requests, auxiliary, permissions = [], [], []
    planning_responses = list(planning_response) if isinstance(planning_response, list) else None
    semantic_responses = list(semantic_response) if isinstance(semantic_response, list) else None
    home = workspace.parent / (workspace.name + "-isolated-home")
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    for name in ("API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "API_BASE_URL", "KODAX_VERIFIER_PROVIDER", "KODAX_VERIFIER_MODEL"):
        monkeypatch.delenv(name, raising=False)

    def deny_network(*_args, **_kwargs):
        raise AssertionError("offline regression attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    capabilities = ModelCapabilities(provider="offline", model_id="offline-model", supports_streaming=stream)

    class Provider:
        name = "offline"

        def capabilities(self, _model):
            return capabilities

        def create_client(self):
            return object()

        def create_completion(self, _client, **kwargs):
            names = [t.get("function", {}).get("name") for t in kwargs.get("tools", [])]
            system = (kwargs.get("messages") or [{}])[0].get("content", "")
            if not names and system in {
                "You are a concise coding task planner.",
                "You are a concise coding task re-planner.",
            }:
                auxiliary.append(copy.deepcopy(kwargs))
                # 只替换规划模型边界，后续合同解析和工作集注入仍由生产代码执行。
                if isinstance(planning_response, BaseException):
                    raise planning_response
                assert planning_response is not None, "unexpected planning request"
                action = planning_responses.pop(0) if planning_responses is not None else planning_response
            elif names == ["emit_sidecar_verdict"]:
                auxiliary.append(copy.deepcopy(kwargs))
                # This is a controlled semantic judgement, not proof of model
                # quality. Actual execution/requirements must still gate it.
                verdict = semantic_responses.pop(0) if semantic_responses is not None else semantic_response
                if isinstance(verdict, BaseException):
                    raise verdict
                action = [_tool("emit_sidecar_verdict", verdict=verdict,
                    reason="Controlled offline semantic review",
                    delivery_assessment={"status": "reviewed", "issues": []} if verdict == "accept" else None)]
            else:
                requests.append(copy.deepcopy(kwargs))
                assert len(requests) <= len(actions), "unexpected extra main model request"
                action = actions[len(requests) - 1]
            calls = [] if isinstance(action, str) else [
                SimpleNamespace(id=f"call-{len(requests)}-{i}", type="function",
                    function=SimpleNamespace(name=name, arguments=json.dumps(args)))
                for i, (name, args) in enumerate(action)
            ]
            response = SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=action if isinstance(action, str) else "", tool_calls=calls),
                    finish_reason="tool_calls" if calls else "stop")],
                usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2, total_tokens=6),
            )
            if not kwargs.get("stream"):
                return response

            def chunks():
                for index, call in enumerate(calls):
                    delta_call = SimpleNamespace(index=index, id=call.id, type="function", function=call.function)
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                        content=None, tool_calls=[delta_call], reasoning_content=None), finish_reason=None)])
                if isinstance(action, str):
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                        content=action, tool_calls=None, reasoning_content=None), finish_reason=None)])
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                    content=None, tool_calls=None, reasoning_content=None),
                    finish_reason="tool_calls" if calls else "stop")], usage=response.usage)

            return chunks()

    provider = Provider()
    runtime = ResolvedModelRuntime(provider_id="offline", model_id="offline-model",
        request_model_id="offline-model", variant=None, provider=provider,
        client=SimpleNamespace(close=lambda: None), capabilities=capabilities, owns_client=True)
    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda *_a, **_k: runtime)
    monkeypatch.setattr("nz_coder.runtime.execution.loop.resolve_model_runtime", lambda *_a, **_k: runtime)
    monkeypatch.setattr("nz_coder.runtime.model_gateway.resolve_model_runtime", lambda *_a, **_k: runtime)

    def allow(name, arguments):
        # 额外命令只调整受控用户授权，实际Bash准入和执行保持真实。
        allowed = permission and name in {"bash", "process"} and arguments.get("command") in (
            "node --test literal.test.cjs", *allowed_commands,
        )
        permissions.append((name, arguments, allowed))
        return allowed

    request = RunRequest(agent=AgentDefinition(name="node-evidence", instructions="Use the declared test and report truthfully."),
        profile=MAIN_PROFILE, workspace=workspace, session_id="node-evidence", stream=stream,
        provider="offline", model="offline-model",
        messages=(*prior_messages, {"role": "user", "content": task or "Fix index.js Unicode literal handling. Run node --test literal.test.cjs and report the result."}),
        metadata={"persist_session": False, "permission_mode": "auto", "max_turns": limit or len(actions)})
    options = RunOptions(permission_asker=allow)
    environment = native_sdk.build_product_run_environment(request, options)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        state = copy.deepcopy(environment.runtime_state.to_dict())
        trace = [json.loads(line) for line in environment.tracer.path.read_text().splitlines()]
        return result, state, trace, requests, auxiliary, permissions
    finally:
        environment.close()


@pytest.mark.parametrize("stream", [False, True])
def test_declared_node_pass_at_hard_cap_preserves_evidence_without_extra_request(monkeypatch, node_project, stream):
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, node_project, [READ, TEST, EDIT, TEST], limit=4, stream=stream,
    )
    assert result.status.value == "max_turns", (result.error, result.final_text, state)
    assert len(requests) == 4
    contract = state["verification_contract"]
    assert contract["passed"] is True
    assert contract["attempted_generation"] == state["acceptance_mutation_generation"]
    assert "Invalid regular expression" in str(requests[2]["messages"])
    checks = [e for e in trace if e.get("event") == "verification_result"]
    assert [e["status"] for e in checks] == ["failed", "passed"]
    assert result.metadata["verification"]["tests_passed"] is True
    assert auxiliary == []  # 没有自然交付候选，不为省略的模型正文启动评审。
    assert permissions and all(p[2] for p in permissions)


@pytest.mark.parametrize("stream", [False, True])
def test_node_pass_returns_to_model_for_followup_work_and_delivery(monkeypatch, node_project, stream):
    docs = [_tool("write_file", path="README.md", content="# Unicode escaping\nNode tests pass.\n")]
    result, state, trace, requests, *_ = _run(
        monkeypatch, node_project, [READ, TEST, EDIT, TEST, docs, "Unicode handling and usage notes are ready."],
        limit=12, stream=stream,
    )
    assert result.status.value == "completed"
    assert len(requests) == 6
    assert (node_project / "README.md").exists()
    assert any("pass" in str(message.get("content", ""))
               for message in requests[4]["messages"] if message.get("role") == "tool")
    assert "usage notes" in result.final_text
    boundary = [e for e in trace if e.get("event") == "terminal_boundary_settled"][-1]
    assert boundary["early_tool_completion_candidate"] is False
    assert boundary["contract_passed"] is True


def test_new_mutation_cannot_reuse_node_pass(monkeypatch, node_project):
    break_again = _tool("edit_file", path="index.js", old_text="=> x;", new_text="=> '[';")
    result, state, trace, requests, *_ = _run(
        monkeypatch, node_project, [READ, TEST, EDIT, [*TEST, break_again]], limit=4,
    )
    assert result.status.value == "max_turns"
    contract = state["verification_contract"]
    assert contract["attempted_generation"] == 1
    assert state["acceptance_mutation_generation"] == 2
    assert contract["passed"] is not True
    assert len(requests) == 4
    checks = [e["status"] for e in trace if e.get("event") == "verification_result"]
    assert checks == ["failed", "passed"]


def test_green_old_tests_do_not_satisfy_new_api_artifact(monkeypatch, node_project):
    # B-type counterexample: old tests are green; the requested API/caller edit
    # has not happened. Even an accepting semantic substitute cannot hide it.
    (node_project / "index.js").write_text("module.exports = x => x;\n")
    (node_project / "consumer.js").write_text("module.exports = require('./index.js');\n")
    result, state, trace, requests, *_ = _run(
        monkeypatch, node_project, [READ, TEST, "Done"], limit=3,
        task="Implement render_product in index.js and update consumer.js. Run node --test literal.test.cjs.",
    )
    assert result.status.value != "completed"
    assert "render_product" not in (node_project / "index.js").read_text()
    assert len(requests) == 3
    assert any(e.get("unresolved_requirements") for e in trace if e.get("event") == "terminal_boundary_settled")


def test_read_only_no_diff_can_finish(monkeypatch, node_project):
    before = (node_project / "index.js").read_bytes()
    result, state, trace, requests, *_ = _run(
        monkeypatch, node_project, [READ, "index.js exports a string replacement function."],
        task="Explain index.js without modifying any files.", limit=2,
    )
    assert result.status.value == "completed"
    assert (node_project / "index.js").read_bytes() == before
    assert state["mutation_generation"] == 0


def test_node_pass_leaves_explicit_documentation_requirement_open(monkeypatch, node_project):
    result, state, trace, requests, *_ = _run(
        monkeypatch, node_project, [READ, TEST, EDIT, TEST, "Done"], limit=5,
        task="Fix index.js Unicode literals and create README.md documentation. Run node --test literal.test.cjs.",
    )
    assert result.status.value == "max_turns"
    assert state["verification_contract"]["passed"] is True
    assert not (node_project / "README.md").exists()
    assert len(requests) == 5  # Passing test did not block reasonable continuation.
    assert any(e.get("unresolved_requirements") for e in trace if e.get("event") == "terminal_boundary_settled")


def test_rejected_node_command_never_becomes_pass(monkeypatch, node_project):
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, node_project, [READ, EDIT, TEST], limit=3, permission=False,
    )
    assert result.status.value == "blocked"
    assert state["verification_contract"]["passed"] is not True
    calls = [e for e in trace if e.get("event") == "tool_call" and e.get("name") == "bash"]
    assert calls and all(e["executed"] is False for e in calls)
    assert all(p[2] is False for p in permissions)


def test_missing_node_dependency_never_becomes_pass(monkeypatch, node_project):
    (node_project / "literal.test.cjs").write_text("require('./missing-dependency');\n")
    result, state, trace, requests, *_ = _run(
        monkeypatch, node_project, [READ, EDIT, TEST], limit=3,
    )
    assert result.status.value == "max_turns"
    assert state["verification_contract"]["passed"] is False
    calls = [e for e in trace if e.get("event") == "tool_call" and e.get("name") == "bash"]
    assert calls and all(e["executed"] and e["command_failed"] for e in calls)


def test_paid_n_action_fragment_uses_current_node_evidence(monkeypatch, node_project):
    fixture = json.loads((Path(__file__).parents[1] / "fixtures" / "paid_n_verification_fragment.json").read_text())
    for name, content in fixture["files"].items():
        (node_project / name).write_text(content)
    result, state, trace, requests, auxiliary, _ = _run(
        monkeypatch, node_project, [*fixture["actions"], "The Unicode literal fix passes the declared Node test."],
        task=fixture["task"], limit=12,
    )
    assert result.status.value == "completed", state.get("requirement_ledger")
    assert len(requests) == 5  # 原动作片段后补受控交付响应，不冒充原付费轨迹。
    assert "Invalid regular expression" in str(requests[2]["messages"])
    assert state["verification_contract"]["passed"] is True
    checks = [e["status"] for e in trace if e.get("event") == "verification_result"]
    assert checks == ["failed", "passed"]
    assert len(auxiliary) == 1  # Real completion hook; controlled model verdict.
    assert state["requested_paths"] == []  # Test command is not a mutation request.
    boundary = [e for e in trace if e.get("event") == "terminal_boundary_settled"][-1]
    assert boundary["early_tool_completion_candidate"] is False
    assert boundary["unresolved_requirements"] == []
    evidence = node_project.parent / "paid-n-replay-evidence"
    evidence.mkdir(exist_ok=True)
    for name, data in (("requests", requests), ("auxiliary-requests", auxiliary),
                       ("runtime", trace), ("state", state)):
        (evidence / (name + ".json")).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    assert hashlib.sha256((node_project / "index.js").read_bytes()).hexdigest() == (
        "4094a0ac1f2bf2b659023ec6fe14133c1b8dfd8e667a6c7937b3d0771ce84520"
    )


@pytest.mark.parametrize('modify_test', [False, True])
def test_explicit_node_test_mutation_is_independent_of_green_execution(monkeypatch, node_project, modify_test):
    (node_project / 'index.js').write_text('module.exports = x => x;\n')
    actions = [[_tool('read_file', path='literal.test.cjs')]]
    if modify_test:
        actions.append([_tool('edit_file', path='literal.test.cjs',
                              old_text="test('Unicode hyphen'", new_text="test('Unicode literal hyphen'")])
    actions += [TEST, 'Node test passed.']
    result, state, trace, *_ = _run(monkeypatch, node_project, actions,
        task='Modify literal.test.cjs and run node --test literal.test.cjs.')
    assert any(e.get('event') == 'verification_result' and e.get('status') == 'passed' for e in trace)
    if modify_test:
        assert state['verification_contract']['passed'] is True
    assert (result.status.value == 'completed') is modify_test
    assert any('literal.test.cjs' in r['expected_artifacts']
               for r in state['task_contract']['requirements'])
