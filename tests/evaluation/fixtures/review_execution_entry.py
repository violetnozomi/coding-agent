"""受限容器内的生产入口；只适配Unix Provider，不重建执行循环。"""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
from dataclasses import asdict
import json
from pathlib import Path
import uuid

import httpx
from openai import OpenAI

from nz_coder.providers.openai_compatible import OpenAICompatibleProvider
from nz_coder.runtime.conversation import prompt
from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunOptions, RunRequest
from nz_coder.runtime.execution import native_sdk, loop
from nz_coder.runtime.model_gateway import ResolvedModelRuntime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/input/run.json"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    output = Path("/result")
    purpose = ContextVar("model_purpose", default="coding")
    provider = OpenAICompatibleProvider(api_key="local-transport-only", base_url="http://localhost/v1")

    def request_id(request):
        request.headers["X-NZ-Call-Id"] = uuid.uuid4().hex

    clients = {name: OpenAI(api_key="local-transport-only", base_url="http://localhost/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.HTTPTransport(uds="/model/" + name + ".sock"),
                                 trust_env=False, event_hooks={"request": [request_id]})) for name in ("main", "auxiliary")}
    original_complete = provider.create_completion

    def complete(_client, **fields):
        role = "main" if purpose.get() == "coding" else "auxiliary"
        return original_complete(clients[role], **fields)

    provider.create_completion = complete
    runtime = ResolvedModelRuntime(provider_id=provider.name, model_id="deepseek-v4-flash",
        request_model_id="deepseek-v4-flash", variant=None, provider=provider, client=clients["main"],
        capabilities=provider.capabilities("deepseek-v4-flash"), owns_client=True)
    for target in (native_sdk, loop):
        target.resolve_model_runtime = lambda *_a, **_k: runtime
    import nz_coder.runtime.model_gateway as gateway
    gateway.resolve_model_runtime = lambda *_a, **_k: runtime
    original_kwargs = gateway.ProductionModelGateway._request_kwargs

    def request_kwargs(self, call):
        # 非流式调用在Gateway工作线程中构造请求；trace线程的ContextVar不能代替它。
        purpose.set(call.purpose.value)
        return original_kwargs(self, call)

    gateway.ProductionModelGateway._request_kwargs = request_kwargs
    task = config["task"]
    request = RunRequest(agent=AgentDefinition(name="coding-task", instructions=prompt.build(memory_block="", skill_descriptions="")),
        profile=MAIN_PROFILE, workspace=Path("/workspace"), session_id="isolated-coding-task", stream=True,
        provider=provider.name, model="deepseek-v4-flash", reasoning_effort=None,
        messages=({"role": "user", "content": task},),
        metadata={"permission_mode": "auto", "persist_session": False, "max_turns": 12})

    def permission(name, arguments):
        allowed = name == "bash" and arguments.get("command") in config["bash_commands"]
        with (output / "permissions.jsonl").open("a") as handle:
            handle.write(json.dumps({"tool": name, "input": arguments, "allowed": allowed}) + "\n")
        return allowed

    options = RunOptions(permission_asker=permission)
    environment = native_sdk.build_product_run_environment(request, options)

    async def execute():
        task = asyncio.create_task(native_sdk.NativeSDKRunner(environment).run_result(request, options))

        async def cancellation():
            while not task.done():
                if Path("/control/cancel").exists():
                    # 使用生产异步任务取消入口，不能把仅记录在RunOptions中的对象当作取消动作。
                    task.cancel()
                    return
                await asyncio.sleep(0.01)

        watcher = asyncio.create_task(cancellation())
        try:
            return await task
        except asyncio.CancelledError:
            (output / "cancellation.json").write_text(json.dumps({"exception": "CancelledError", "run_result": None}))
            return None
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    try:
        result = asyncio.run(execute())
        if result is not None:
            (output / "result.json").write_text(json.dumps(asdict(result), default=str, indent=2))
        (output / "state.json").write_text(json.dumps(environment.runtime_state.to_dict(), default=str, indent=2))
        (output / "runtime.jsonl").write_bytes(environment.tracer.path.read_bytes())
        print(json.dumps({"status": result.status.value if result else "cancelled_exception", "error": result.error if result else "CancelledError"}))
    finally:
        environment.close()
        clients["auxiliary"].close()


if __name__ == "__main__":
    main()
