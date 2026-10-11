"""本轮 Pro 容器入口：真实生产 SDK，仅替换模型传输边界。"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import asdict
import json
import os
from pathlib import Path
import shlex
import uuid

import httpx
from openai import OpenAI
from nz_coder.providers.openai_compatible import OpenAICompatibleProvider
from nz_coder.runtime.conversation import prompt
from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunOptions, RunRequest
from nz_coder.runtime.execution import native_sdk, loop
from nz_coder.runtime import model_gateway as gateway
from nz_coder.runtime.model_gateway import ResolvedModelRuntime
from nz_coder.swebench.policy import STRICT_ALLOWED_TOOLS, strict_private_tool_input_violation


def main():
    config = json.loads(Path("/input/run.json").read_text())
    output = Path("/result")
    # Agent 已完成依赖导入，sys.path 继续供 Agent 使用；测试子进程使用原项目依赖。
    bootstrap_path = os.environ.pop("PYTHONPATH", None)
    (output / "child-environment.json").write_text(json.dumps({
        "agent_bootstrap_pythonpath": bootstrap_path, "project_child_pythonpath": None,
        "project_dependency_isolation": "remove_agent_only_bootstrap_path", "harness_revision": 2}))
    purpose = ContextVar("model_purpose", default="coding")

    def nonce(request):
        request.headers["X-NZ-Call-Id"] = uuid.uuid4().hex

    clients = {role: OpenAI(api_key="local-transport-only", base_url="http://localhost/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.HTTPTransport(uds=f"/model/{role}.sock"),
                                 trust_env=False, event_hooks={"request": [nonce]}))
               for role in ("main", "auxiliary")}
    provider = OpenAICompatibleProvider(api_key="local-transport-only", base_url="http://localhost/v1")
    complete = provider.create_completion
    provider.create_completion = lambda _client, **fields: complete(
        clients["main" if purpose.get() == "coding" else "auxiliary"], **fields)
    runtime = ResolvedModelRuntime(provider_id=provider.name, model_id=config["model"],
        request_model_id=config["model"], variant=None, provider=provider, client=clients["main"],
        capabilities=provider.capabilities(config["model"]), owns_client=True)
    for target in (native_sdk, loop, gateway):
        target.resolve_model_runtime = lambda *_args, **_kwargs: runtime
    original = gateway.ProductionModelGateway._request_kwargs

    def request_kwargs(self, call):
        purpose.set(call.purpose.value)
        return original(self, call)

    gateway.ProductionModelGateway._request_kwargs = request_kwargs

    def permission(name, arguments):
        # 所有写入仍经过原工具的路径、事务和版本检查；容器无宿主私有挂载。
        allowed = name in STRICT_ALLOWED_TOOLS and not strict_private_tool_input_violation(name, arguments)
        if allowed and name == "bash":
            try:
                tokens = shlex.split(str(arguments.get("command") or ""))
            except ValueError:
                tokens = []
            allowed = bool(tokens) and Path(tokens[0]).name in {
                "go", "node", "npm", "yarn", "pnpm", "python", "python3", "pytest",
                "git", "rg", "grep", "ls", "cat", "head", "tail", "wc", "find",
                "sed", "awk", "pwd", "diff", "test", "printf", "echo", "make",
            }
        with (output / "permissions.jsonl").open("a") as sink:
            sink.write(json.dumps({"tool": name, "input": arguments, "allowed": allowed}) + "\n")
        return allowed

    def event(event):
        with (output / "events.jsonl").open("a") as sink:
            sink.write(json.dumps(asdict(event), default=str) + "\n")

    request = RunRequest(agent=AgentDefinition(name="coding-task",
        instructions=prompt.build(memory_block="", skill_descriptions=""),
        allowed_tools=tuple(sorted(STRICT_ALLOWED_TOOLS))),
        profile=MAIN_PROFILE, workspace=Path("/workspace"), session_id=config["session_id"],
        stream=False, provider=provider.name, model=config["model"], reasoning_effort=None,
        messages=({"role": "user", "content": config["task"]},),
        metadata={"permission_mode": "auto", "persist_session": True, "max_turns": config["max_turns"]})
    options = RunOptions(permission_asker=permission, on_event=event)
    environment = native_sdk.build_product_run_environment(request, options)

    async def execute():
        return await native_sdk.NativeSDKRunner(environment).run_result(request, options)

    try:
        result = asyncio.run(execute())
        (output / "result.json").write_text(json.dumps(asdict(result), default=str, ensure_ascii=False, indent=2))
        print(json.dumps({"status": result.status.value, "error": result.error}), flush=True)
    finally:
        (output / "state.json").write_text(json.dumps(environment.runtime_state.to_dict(), default=str, indent=2))
        path = environment.tracer.path
        if path.exists():
            (output / "runtime.jsonl").write_bytes(path.read_bytes())
        environment.close()
        (output / "cleanup.json").write_text(json.dumps({"complete": environment.environment_cleanup_complete,
            "failures": environment.environment_cleanup_failures}, default=str))
        clients["auxiliary"].close()


if __name__ == "__main__":
    main()
