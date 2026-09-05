"""Offline F04 transport only; no task solutions, network, or runtime replacement."""
from __future__ import annotations

import json
from functools import wraps
import os
from pathlib import Path
import shlex
import sys
import time
import uuid

from openai.types.chat import ChatCompletion, ChatCompletionChunk
from nz_coder.providers.openai_compatible import OpenAICompatibleProvider


def observe_capture():
    """Test-only return observer: execute the installed handler unchanged.

    No substitute tool, permission bypass, or fabricated result. The exact
    ToolOutput is returned; the private receipt observes it before projection.
    Only this isolated offline-provider process installs the observer.
    """
    from nz_coder import tools
    from nz_coder.tools.bash import run_bash

    if tools.TOOL_HANDLERS.get("bash") is not run_bash:
        return

    @wraps(run_bash)
    def observed(*args, **kwargs):
        result = run_bash(*args, **kwargs)
        record = {"attempt_id": os.environ["NZ_F04_ATTEMPT"],
                  "tool_call_id": tools.current_tool_call_id(),
                  "output": str(result), "metadata": getattr(result, "metadata", {})}
        try:
            with Path(os.environ["NZ_F04_CAPTURE_RECORD"]).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:
            # Missing evidence fails acceptance, never changes the actual tool.
            pass
        return result

    with tools._STATIC_REGISTRY_LOCK:
        tools.TOOL_HANDLERS["bash"] = observed


class F04Provider(OpenAICompatibleProvider):
    """Return fixed tool requests; record actual model-side tool replies privately."""

    def create_client(self):
        return object()

    def create_completion(self, client, **kwargs):
        messages = kwargs["messages"]
        start = next(i for i in range(len(messages)-1, -1, -1)
                     if messages[i].get("role") == "user" and "R1:F" in str(messages[i].get("content")))
        prompt = str(messages[start].get("content"))
        results = [m for m in messages[start+1:] if m.get("role") == "tool"]
        record = {"attempt_id": os.environ["NZ_F04_ATTEMPT"],
                  "tool_replies": [{"tool_call_id": m.get("tool_call_id"),
                                    "content": m.get("content")} for m in results]}
        with Path(os.environ["NZ_F04_MODEL_RECORD"]).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        call = None
        if not results and "F04" in prompt:
            call = ("bash", {"command": f"{shlex.quote(sys.executable)} emit_failure.py", "timeout": 30})
        elif not results and "F01" in prompt:
            call = ("write_file", {"path": "smoke.txt", "content": "F04 wheel smoke\n"})
        content = "F04 request settled." if "F04" in prompt else "Session reuse settled."
        message = {"role": "assistant", "content": None if call else content}
        if call:
            message["tool_calls"] = [{"id": "call_" + uuid.uuid4().hex, "type": "function",
                "function": {"name": call[0], "arguments": json.dumps(call[1])}}]
        finish = "tool_calls" if call else "stop"
        common = {"id": "chatcmpl-f04-" + uuid.uuid4().hex, "created": int(time.time()),
                  "model": "f04-offline"}
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if not kwargs.get("stream"):
            return ChatCompletion.model_validate({**common, "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})
        delta = dict(message)
        if call:
            delta["tool_calls"] = [{"index": 0, **message["tool_calls"][0]}]
        return iter([
            ChatCompletionChunk.model_validate({**common, "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}),
            ChatCompletionChunk.model_validate({**common, "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage}),
        ])


def factory(**kwargs):
    observe_capture()
    return F04Provider(**kwargs)
