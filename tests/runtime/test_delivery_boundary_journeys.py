"""真实 Runner 的补救、未验证正文及结构化发布闭环。"""
from __future__ import annotations

import asyncio
import io
import json
from dataclasses import replace
from types import SimpleNamespace

from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request
from test_delivery_assessment_runner import accepted, run_probe
from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.verification.hooks import AgentHooks, StopHookDecision
from nz_coder.runtime.verification.sidecar_verifier import ResolvedVerifierProvider, create_sidecar_verifier_hook


def test_unavailable_review_keeps_delivery_instructions_in_message_details(monkeypatch, tmp_path):
    from rich.console import Console
    from nz_coder.interface.commands.handlers.core import handle_message_detail
    from nz_coder.runtime.process.workdir import scoped_workdir

    candidate = "Run python app.py --help to inspect the available commands."
    result, _hook, provider, _snapshot = run_probe(monkeypatch, tmp_path,
        reports=lambda *_: (accepted("unknown"), "tool_calls"), candidate=candidate)
    assert result.status.value == "completed_unverified"
    assert "本轮没有已采信的测试结果" in result.final_text and "/message" in result.final_text
    assert candidate not in result.final_text and provider.mains == provider.reviews == 1
    printed = io.StringIO()
    context = SimpleNamespace(args="1", session_id=result.session_id, history=list(result.messages),
                              console=Console(file=printed, width=120, color_system=None))
    with scoped_workdir(tmp_path):
        asyncio.run(handle_message_detail(context))
    assert "模型说明" in printed.getvalue() and candidate in printed.getvalue()


def test_user_stop_hook_runs_even_with_no_reanimation_budget():
    seen = []
    def stop(_context):
        seen.append(True)
        return StopHookDecision(action="abort", message="User-defined stop", source="user")
    hooks = AgentHooks(stop_hooks=[stop], stop_hook_reanimate_budget=0)
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    loop = SimpleNamespace(runtime_state=RuntimeState(), tracer=None)
    result = asyncio.run(hooks.handle_delivery_boundary_async(loop, [], message="Done",
        review=False, guidance="Required command has not passed."))
    assert seen == [True]
    assert result == "blocked"
    assert hooks.stop_hook_reason == "User-defined stop"


def structured_run(monkeypatch, root, *, verdict):
    class Provider:
        name = "offline"
        mains = 0
        def create_completion(self, _client, **kwargs):
            if any(item.get("function", {}).get("name") == "emit_sidecar_verdict" for item in kwargs.get("tools", [])):
                payload = accepted() if verdict == "accept" else accepted("unknown") if verdict == "unavailable" else {
                    "verdict": "revise", "reason": "The reported test count contradicts the observed evidence."}
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="",
                    tool_calls=[SimpleNamespace(function=SimpleNamespace(name="emit_sidecar_verdict",
                        arguments=json.dumps(payload)))]), finish_reason="tool_calls")])
            self.mains += 1
            assert self.mains <= 3, "Unexpected extra main request"
            return _completion('{"tests_passed":999}')
    provider = Provider()
    _install_runtime(monkeypatch, provider)
    request = _request(root)
    request = replace(request, agent=replace(request.agent, output_schema={"type": "object",
        "properties": {"tests_passed": {"type": "integer"}}, "required": ["tests_passed"]}),
        messages=({"role": "user", "content": "Return the actual number of passing tests."},))
    options = RunOptions(permission_asker=lambda *_: True)
    environment = native_sdk.build_product_run_environment(request, options)
    hook = create_sidecar_verifier_hook(environment,
        ResolvedVerifierProvider(provider, object(), "offline-model", "offline", "inherit-main"))
    environment._configured_sidecar_verifier = hook
    try:
        return asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options)), environment
    finally:
        environment.close()


def test_denied_structured_candidate_is_not_republished(monkeypatch, tmp_path):
    result, _environment = structured_run(monkeypatch, tmp_path, verdict="revise")
    assert result.status.value == "blocked"
    assert "structured" not in result.metadata
    assert "structured_output_source" not in result.metadata["verification"]
    assert "999" not in result.final_text
    assert "reported test count contradicts" in result.final_text
    assert not result.metadata["authored_content"]


def test_unavailable_structured_review_preserves_schema_value_with_outer_status(monkeypatch, tmp_path):
    result, _environment = structured_run(monkeypatch, tmp_path, verdict="unavailable")
    assert result.status.value == "completed_unverified"
    assert result.metadata["structured"] == {"tests_passed": 999}
    assert result.metadata["verification"]["structured_output_source"] == "model"
    assert result.metadata["verification"]["delivery_review"] is None


def test_accepted_structured_value_and_candidate_are_bound(monkeypatch, tmp_path):
    from nz_coder.runtime.verification.domain.models import candidate_fingerprint
    result, _environment = structured_run(monkeypatch, tmp_path, verdict="accept")
    assert result.status.value == "completed"
    assert json.loads(result.final_text) == result.metadata["structured"] == {"tests_passed": 999}
    verification = result.metadata["verification"]
    assert "denied_commands" not in verification
    assert verification["structured_output_source"] == "model"
    assert verification["tests_passed"] is None and verification["last_verification"] is None
    assert verification["candidate_fingerprint"] == candidate_fingerprint(
        '{"tests_passed":999}', {"tests_passed": 999})


def test_cancelled_user_hook_reconciles_workspace_before_retiring_candidate(monkeypatch, tmp_path):
    source = tmp_path / 'source.py'
    source.write_text('VALUE = 1\n')
    class Provider:
        name = 'offline'
        def create_completion(self, _client, **kwargs):
            return _completion('D1')
    _install_runtime(monkeypatch, Provider())
    request = replace(_request(tmp_path), messages=({'role': 'user', 'content': 'Inspect source.py.'},))
    options = RunOptions()
    environment = native_sdk.build_product_run_environment(request, options)
    async def hook(_context):
        source.write_text('VALUE = 2\n')
        raise asyncio.CancelledError('hook cancelled after workspace mutation')
    environment.hooks.register_stop_hook(hook)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
    finally:
        environment.close()
    assert result.status.value == 'cancelled'
    assert result.metadata['verification']['mutation_generation'] == 1
    assert result.metadata['authored_content'] == ''
