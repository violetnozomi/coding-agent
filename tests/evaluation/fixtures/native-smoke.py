"""Invoke the production Runner; customize only the local Provider transport."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path

import httpx
from openai import OpenAI

from nz_coder.providers.openai_compatible import OpenAICompatibleProvider
from nz_coder.runtime.conversation import prompt
from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunOptions, RunRequest
from nz_coder.runtime.core.execution_context import repo_retrieval_strategy
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.execution import loop
from nz_coder.runtime.model_gateway import ResolvedModelRuntime


def main():
    output = Path(os.environ["SMOKE_OUTPUT"])
    provider = OpenAICompatibleProvider(api_key="local-test-only", base_url="http://localhost/v1")
    client = OpenAI(api_key="local-test-only", base_url="http://localhost/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.HTTPTransport(uds=os.environ["SMOKE_SOCKET"]),
                                             trust_env=False))
    runtime = ResolvedModelRuntime(provider_id=provider.name, model_id="local-smoke",
        request_model_id="local-smoke", variant=None, provider=provider, client=client,
        capabilities=provider.capabilities("local-smoke"), owns_client=True)
    # Production factory resolves its model again at run activation. Both resolve
    # sites use this same real OpenAI client, whose transport is restricted to UDS.
    native_sdk.resolve_model_runtime = lambda *_args, **_kwargs: runtime
    loop.resolve_model_runtime = lambda *_args, **_kwargs: runtime
    request = RunRequest(agent=AgentDefinition(name="product", instructions=prompt.build(
        memory_block="", skill_descriptions="")), profile=MAIN_PROFILE,
        workspace=Path.cwd(), session_id="offline-F", stream=False,
        provider=provider.name, model="local-smoke",
        messages=({"role": "user", "content": os.environ["SMOKE_PROMPT"]},),
        metadata={"permission_mode": "auto", "persist_session": False, "max_turns": 8})

    def event(item):
        with (output / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(asdict(item), default=str) + "\n")

    def permission(name, arguments):
        allowed = (name == "bash" and arguments.get("command") == "python -m pytest -q")
        with (output / "permissions.jsonl").open("a") as stream:
            stream.write(json.dumps({"tool": name, "input": arguments, "allowed": allowed}) + "\n")
        return allowed

    options = RunOptions(on_event=event, permission_asker=permission)
    environment = native_sdk.build_product_run_environment(request, options)
    try:
        (output / "effective.json").write_text(json.dumps({
            "repo_retrieval_strategy_override": environment.repo_retrieval_strategy,
            "repo_retrieval_strategy": environment.repo_retrieval_strategy or repo_retrieval_strategy(),
            "runner": type(environment.runner).__name__,
            "tools": type(environment.runtime_services.tools).__name__,
            "verifier": type(environment.runtime_services.verifier).__name__,
        }, indent=2))
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        (output / "result.json").write_text(json.dumps(asdict(result), default=str, indent=2))
        print(json.dumps({"status": result.status.value, "error": result.error}))
        if result.status.value != "completed":
            raise SystemExit(1)
    finally:
        environment.close()


if __name__ == "__main__":
    main()
