"""Spawn-safe offline model boundary for real child routing regression tests."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import socket
import time
from unittest.mock import patch

from nz_coder.runtime.agent.process_supervisor import (
    _run_subagent_process as _production_process_entry,
)


@contextmanager
def capture_model_boundary(workspace: Path):
    """Only replace Provider construction/transport; keep route selection real."""
    from openai.types.chat import ChatCompletion
    from nz_coder.providers.capabilities import ModelCapabilities
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.model_gateway import ResolvedModelRuntime

    class Provider:
        name = "offline-model-capture"

        def create_completion(self, _client, **kwargs):
            prompt = next(
                message["content"]
                for message in kwargs["messages"]
                if message.get("role") == "user"
                and "MODEL_CAPTURE:" in str(message.get("content", ""))
            )
            label = prompt.split("MODEL_CAPTURE:", 1)[1].split()[0]
            directory = workspace / ".nz-coder"
            directory.mkdir(exist_ok=True)
            facts = {
                "selection_model": self.selection_model,
                "request_model": kwargs["model"],
            }
            (directory / f"model-capture-{label}.json").write_text(
                json.dumps(facts), encoding="utf-8",
            )
            if label == "blocker":
                (directory / "model-blocker-ready").touch()
                deadline = time.monotonic() + 10
                while not (directory / "model-blocker-release").exists():
                    if time.monotonic() >= deadline:
                        raise AssertionError("test did not release the Provider gate")
                    time.sleep(0.005)
            return ChatCompletion(
                id="offline-model-capture", created=0,
                model=self.selection_model, object="chat.completion",
                choices=[{
                    "index": 0, "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": f"Captured model {self.selection_model}.",
                    },
                }],
            )

    def resolve(selection, **_kwargs):
        provider = selection.provider
        provider.selection_model = selection.model_id
        return ResolvedModelRuntime(
            provider_id=selection.provider_name,
            model_id=selection.model_id,
            request_model_id=selection.model_id,
            variant=None,
            provider=provider,
            client=object(),
            owns_client=False,
            capabilities=ModelCapabilities(
                provider=selection.provider_name,
                model_id=selection.model_id,
                supports_streaming=False,
            ),
        )

    original_connect = socket.socket.connect

    def connect(stream, address, *args, **kwargs):
        if stream.family in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError("offline model regression attempted network access")
        return original_connect(stream, address, *args, **kwargs)

    with (
        patch.object(subagent, "create_provider", lambda **_kwargs: Provider()),
        patch.object(subagent, "resolve_model_runtime", resolve),
        patch.object(socket.socket, "connect", connect),
    ):
        yield


def spawned_model_capture(result_path, payload, endpoint):
    """Run the production spawn entry with the same offline boundary as threads."""
    with capture_model_boundary(Path(payload["workspace"])):
        _production_process_entry(result_path, payload, endpoint)
