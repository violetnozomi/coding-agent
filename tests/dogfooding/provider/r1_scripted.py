"""Offline transport for F01-F04; real product runtime executes all tool calls."""
from __future__ import annotations

import json
import re
import shlex
import sys
import time
import uuid

from openai.types.chat import ChatCompletion, ChatCompletionChunk
from nz_coder.providers.openai_compatible import OpenAICompatibleProvider


class ScriptedProvider(OpenAICompatibleProvider):
    """No network client and no hidden task implementations."""

    def create_client(self):
        return object()

    def create_completion(self, client, **kwargs):
        messages = kwargs["messages"]
        start = next(i for i in range(len(messages)-1, -1, -1)
                     if messages[i].get("role") == "user" and "R1:F" in str(messages[i].get("content")))
        prompt = str(messages[start].get("content"))
        # Product transport can coalesce consecutive user messages after a host
        # failure. Select the last explicit fixture command, not a historical tag.
        marker = list(re.finditer(r"R1:F\d\d", prompt))[-1]
        prompt = prompt[marker.start():]
        results = [m for m in messages[start+1:] if m.get("role") == "tool"]
        call = None
        content = "R1 final: session usable. 中文显示正常。\n```python\nprint('done')\n```"
        if results:
            content = "R1 final: actual tool result received: " + str(results[-1].get("content"))[-350:]
        elif "F01" in prompt:
            call = ("write_file", {"path":"permission-note.txt", "content":"R1 approved write\n"})
        elif "F02" in prompt or "F03" in prompt:
            duration = 20 if "F02" in prompt else 5
            source = ("import os,time; from pathlib import Path; "
                      "Path('slow.pid').write_text(str(os.getpid())); "
                      "print('R1_TOOL_STARTED',flush=True); "
                      f"time.sleep({duration}); "
                      "Path('late.txt').write_text('R1_TOOL_FINISHED'); "
                      "print('R1_TOOL_FINISHED',flush=True)")
            call = ("bash", {"command":f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}", "timeout":30})
        elif "F04" in prompt:
            source = "import sys; [print('synthetic line %04d 中文 ' % i + 'x'*80) for i in range(3000)]; print('R1_CRITICAL_TAIL_ERROR',file=sys.stderr); sys.exit(7)"
            call = ("bash", {"command":f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}", "timeout":30})
        identifier = "chatcmpl-r1-" + uuid.uuid4().hex
        message = {"role":"assistant", "content":None if call else content}
        if call:
            message["tool_calls"] = [{"id":"call_"+uuid.uuid4().hex, "type":"function",
                                      "function":{"name":call[0], "arguments":json.dumps(call[1])}}]
        finish = "tool_calls" if call else "stop"
        if not kwargs.get("stream"):
            return ChatCompletion.model_validate({"id":identifier, "object":"chat.completion", "created":int(time.time()),
                "model":"r1-scripted", "choices":[{"index":0, "message":message, "finish_reason":finish}],
                "usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}})
        delta = dict(message)
        if call:
            delta["tool_calls"] = [{"index":0, **message["tool_calls"][0]}]
        return iter([
            ChatCompletionChunk.model_validate({"id":identifier, "object":"chat.completion.chunk", "created":int(time.time()),
                "model":"r1-scripted", "choices":[{"index":0,"delta":delta,"finish_reason":None}]}),
            ChatCompletionChunk.model_validate({"id":identifier, "object":"chat.completion.chunk", "created":int(time.time()),
                "model":"r1-scripted", "choices":[{"index":0,"delta":{},"finish_reason":finish}],
                "usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}),
        ])


def factory(**kwargs):
    return ScriptedProvider(**kwargs)
