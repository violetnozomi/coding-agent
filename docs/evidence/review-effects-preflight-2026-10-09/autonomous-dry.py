"""生产入口的首请求干跑；传输层只记录请求，绝不返回动作或补丁。"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path
import sys

import httpx
from openai import OpenAI
import pytest


class DryBoundary(BaseException):
    """在实际 HTTP 序列化后停止；不进入 Provider 错误重试。"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), "preserve completed dry-run"
    sys.path.insert(0, str(args.code))
    from nz_coder.providers.openai_compatible import OpenAICompatibleProvider
    from nz_coder.runtime.core import MAIN_PROFILE
    from nz_coder.runtime.core.request import AgentDefinition, RunRequest, RunOptions
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.conversation import prompt
    from nz_coder.runtime.model_gateway import ResolvedModelRuntime
    from nz_coder.runtime.verification import sidecar_verifier

    captured = {"kind": "request_dry_run", "paid_requests": 0, "model_response": None,
                "usage": None, "autonomous_execution": False}

    def hold(request):
        captured["actual_serialized_main_request"] = json.loads(request.content)
        captured["url"] = str(request.url)
        raise DryBoundary()

    provider = OpenAICompatibleProvider(api_key="local-offline", base_url="http://localhost/v1")
    client = OpenAI(api_key="local-offline", base_url="http://localhost/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(hold), trust_env=False))
    runtime = ResolvedModelRuntime(provider_id=provider.name, model_id="deepseek-v4-flash",
        request_model_id="deepseek-v4-flash", variant=None, provider=provider, client=client,
        capabilities=provider.capabilities("deepseek-v4-flash"), owns_client=True)
    request = RunRequest(agent=AgentDefinition(name="coding-task", instructions=prompt.build(memory_block="", skill_descriptions="")),
        profile=MAIN_PROFILE, workspace=args.workspace, session_id="coding-task", stream=True,
        provider=provider.name, model="deepseek-v4-flash", reasoning_effort=None,
        messages=({"role": "user", "content": args.task_file.read_text()},),
        metadata={"permission_mode": "auto", "persist_session": False, "max_turns": 12})
    options = RunOptions(permission_asker=lambda *_args: False)
    with pytest.MonkeyPatch.context() as patch:
        for target in (native_sdk,):
            patch.setattr(target, "resolve_model_runtime", lambda *_a, **_k: runtime)
        patch.setattr("nz_coder.runtime.execution.loop.resolve_model_runtime", lambda *_a, **_k: runtime)
        patch.setattr("nz_coder.runtime.model_gateway.resolve_model_runtime", lambda *_a, **_k: runtime)
        env = native_sdk.build_product_run_environment(request, options)
        try:
            captured["production_strategy"] = env.repo_retrieval_strategy
            captured["requested_main_turn_limit"] = request.metadata["max_turns"]
            # 原生审查参数也过实际 Provider 序列化；这不是一次审查判决。
            base = sidecar_verifier._verifier_capability_options(runtime)
            reviewer = {"model": runtime.request_model_id, "messages": [{"role": "user", "content": "<packet not sent>"}],
                        "tools": [{"type": "function", "function": sidecar_verifier.VERIFIER_REPORT_TOOL}],
                        "tool_choice": {"type": "function", "function": {"name": "emit_sidecar_verdict"}},
                        "max_tokens": 1024, **base}
            try:
                provider.create_completion(client, **reviewer)
            except DryBoundary:
                captured["serialized_review_parameter_probe"] = captured.pop("actual_serialized_main_request")
            result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
            captured["dry_runner_status"] = result.status.value
        except DryBoundary:
            captured["dry_runner_status"] = "held_at_transport"
        finally:
            captured["state_at_boundary"] = copy.deepcopy(env.runtime_state.to_dict())
            captured["runtime"] = [json.loads(line) for line in env.tracer.path.read_text().splitlines()]
            env.close()
    assert "actual_serialized_main_request" in captured
    assert not any(e.get("event") == "tool_call" for e in captured["runtime"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(captured, ensure_ascii=False, indent=2, default=str) + "\n")
    main_fields = captured["actual_serialized_main_request"]
    review_fields = captured["serialized_review_parameter_probe"]
    print(json.dumps({"strategy": captured["production_strategy"], "main": {k: main_fields.get(k) for k in
                     ("model", "stream", "max_tokens", "thinking", "reasoning_effort")},
                     "review": {k: review_fields.get(k) for k in ("stream", "max_tokens", "thinking", "reasoning_effort")},
                     "paid_requests": 0}))


if __name__ == "__main__":
    main()
