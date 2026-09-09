"""Real Gateway/projection contracts: no retry or identity loss after tools."""
from __future__ import annotations

from dataclasses import replace
import asyncio
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from nz_coder.protocol.message_schema import set_assistant_error
from nz_coder.runtime.conversation.model_result import LLMResult
from nz_coder.runtime.execution.provider_stream import _settle_stream_tools
from nz_coder.providers.normalized import chunk
from tests.runtime.model_gateway.test_streaming_gateway import _gateway, _call
from tests.runtime.test_stream_consumer_timeouts import Clock


@pytest.mark.parametrize("error_type", [TimeoutError, ConnectionError])
def test_post_tool_error_preserves_identity_and_first_cause(error_type):
    secret = "SYNTHETIC_PRIVATE_BODY_KEY"
    error = error_type(secret)
    error.body = {"private": secret}
    error.headers = {"Authorization": secret}

    class Source:
        def __iter__(self):
            yield chunk(tool_calls=[{
                "index": 0, "id": "edit-once", "name": "edit_file",
                "arguments": '{"path":"a.py"}',
            }], finish_reason="tool_calls")
            yield {"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}}
            raise error

        def close(self):
            raise ValueError("SECONDARY_" + secret)

    observed = []
    effects = []
    gateway, provider = _gateway([Source()], observer=lambda name, data: observed.append((name, data)))

    def on_event(event):
        if event.kind == "finish":
            effects.append("executed")
            return True

    outcome = gateway.complete_stream_sync(replace(_call(), timeout_seconds=600), on_event=on_event)
    result = _settle_stream_tools(
        SimpleNamespace(provider_id="fake"), LLMResult(extra={"provider_extra": outcome.provider_metadata}),
        outcome, None, True, "continue", None, 0,
    )
    message = {"role": "assistant", "content": ""}
    set_assistant_error(message, result.post_tool_stream_error, **result.assistant_error)
    public = message["_nz_assistant_error"]["data"]["public_error"]
    assert public["metadata"]["error_type"] == error_type.__name__
    assert public["metadata"]["origin"] == "provider_transport"
    assert public["metadata"]["phase"] == "post_tool_stream"
    assert public["retryable"] is False
    assert "statusCode" not in result.assistant_error["data"]
    assert secret not in repr(message) + repr(result.extra)
    assert "/diff" in public["message"]
    assert effects == ["executed"] and len(provider.requests) == 1
    finishes = [data for name, data in observed if name == "model_call_finish"]
    assert len(finishes) == 1 and finishes[0]["usage"]["total"] == 12
    assert finishes[0]["status"] != "completed"


@pytest.mark.parametrize("same_chunk", [False, True])
def test_tool_finish_and_usage_settle_once(same_chunk):
    usage = {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}
    frames = [chunk(tool_calls=[dict(index=0, id="one", name="read_file", arguments='{"path":"a.py"}')],
                    finish_reason="tool_calls", usage=usage if same_chunk else None)]
    if not same_chunk:
        frames.append({"choices": [], "usage": usage})
    gateway, provider = _gateway([iter(frames)])
    events = []

    def consume(event):
        events.append(event.kind)
        return event.kind == "finish"

    outcome = gateway.complete_stream_sync(_call(), on_event=consume)
    assert events.count("finish") == events.count("usage") == 1
    assert len(provider.requests) == outcome.attempts == 1
    assert outcome.usage.total_tokens == 12


def test_sdk_read_error_and_broken_observer_remain_safe():
    import httpx
    import openai

    def source():
        yield chunk(content="partial")
        raise openai.APITimeoutError(request=httpx.Request("GET", "https://user:SENTINEL@example.invalid"))

    def broken_observer(*_args):
        raise ValueError("SECONDARY_SECRET")

    gateway, provider = _gateway([source()], max_retries=0, observer=broken_observer)
    outcome = gateway.complete_stream_sync(_call())
    public = outcome.provider_metadata["public_error"]
    assert public["metadata"]["error_type"] == "APITimeoutError"
    assert public["metadata"]["origin"] == "provider_transport"
    assert public["metadata"]["phase"] == "read_stream"
    assert "status_code" not in public["metadata"]
    assert "SENTINEL" not in repr(outcome) and "SECONDARY_SECRET" not in repr(outcome)
    assert len(provider.requests) == 1


def test_bridge_keeps_local_first_cause_over_later_transport_error():
    from nz_coder.runtime.execution.services import _StreamToolBridge, StreamToolExecutionFailed
    from nz_coder.runtime.model_gateway import ModelCallOutcome

    async def exercise():
        async def handler(_result):
            raise ValueError("LOCAL_SECRET")
        bridge = _StreamToolBridge(asyncio.get_running_loop(), handler)
        with pytest.raises(StreamToolExecutionFailed) as error:
            await asyncio.to_thread(bridge.execute, LLMResult())
        return error.value

    error = asyncio.run(exercise())
    assert "LOCAL_SECRET" not in str(error)
    result = _settle_stream_tools(SimpleNamespace(provider_id="fake"), LLMResult(),
        ModelCallOutcome.completed(provider_metadata={"stream_error": {"message": "TRANSPORT_SECRET"}}),
        None, True, "", error, 0)
    public = result.assistant_error["data"]["public_error"]
    assert public["metadata"]["error_type"] == "ValueError"
    assert public["metadata"]["origin"] == "tool_execution"
    assert public["metadata"]["phase"] == "execute_tools"
    assert not public["retryable"]
    assert "SECRET" not in repr(result)


def test_stream_client_error_keeps_existing_diagnostic_route():
    from nz_coder.runtime.execution.loop import AgentLoop

    error = ValueError("PRIVATE_CLIENT_BODY")
    error.status_code = 422
    gateway, _ = _gateway([error], max_retries=0)
    outcome = gateway.complete_stream_sync(_call())
    host = SimpleNamespace(provider_id="fake", _make_client_error_diag=lambda message: "diagnostic: " + message)
    result = AgentLoop._gateway_outcome_result(host, outcome)
    assert result.diagnostic and not result.aborted
    assert result.assistant_error["data"]["statusCode"] == 422
    assert "PRIVATE_CLIENT_BODY" not in repr(result)


def test_response_only_http_status_survives_without_body_or_headers():
    error = RuntimeError("PRIVATE_HTTP_MESSAGE")
    error.response = SimpleNamespace(status_code=401, text="PRIVATE_HTTP_BODY", headers={"Authorization": "PRIVATE_KEY"})
    gateway, _ = _gateway([error], max_retries=0)
    outcome = gateway.complete_stream_sync(_call())
    from nz_coder.runtime.execution.loop import AgentLoop
    result = AgentLoop._gateway_outcome_result(SimpleNamespace(provider_id="fake"), outcome)
    assert result.assistant_error["name"] == "ProviderAuthError"
    assert result.assistant_error["data"]["public_error"]["metadata"]["status_code"] == 401
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize("scenario", ["approve", "deny", "read_error", "hard", "cancel"])
def test_native_permission_edit_tail_test_and_final(tmp_path, monkeypatch, scenario):
    """Only SDK completions and the stream clock are controlled; tools are real."""
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.model_gateway import stream as streams
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import load_session
    from nz_coder.protocol.message_schema import ASSISTANT_ERROR_KEY

    clock = Clock()
    monkeypatch.setattr(streams, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(config, "PROVIDER_STREAM_IDLE_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(config, "PROVIDER_HARD_TIMEOUT_SECONDS", 600)
    (tmp_path / "value.py").write_text("def value():\n    return 1\n")
    entered = threading.Event()
    release = threading.Event()
    asks = []
    requests = []
    tail_seen = []
    usage = {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}

    def ask(name, arguments):
        asks.append(name)
        if name == "edit_file":
            entered.set()
            assert release.wait(3), "permission was not explicitly released"
            return scenario not in {"deny", "cancel"}
        return True

    calls = [
        ("edit_file", dict(path="value.py", old_text="return 1", new_text="return 2")),
        ("write_file", dict(path="test_value.py", content="import unittest\nfrom value import value\nclass ValueTest(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(value(), 2)\n")),
        ("bash", dict(command=f"{sys.executable} -m unittest -v test_value")),
    ]

    class Completions:
        def create(self, **kwargs):
            requests.append(kwargs)
            assert kwargs.get("stream") is True
            index = len(requests) - 1
            if scenario in {"deny", "cancel"} and index:
                return iter([chunk(content="Permission declined; no edit executed.", finish_reason="stop", usage=usage)])
            if index >= len(calls):
                return iter([chunk(content="Added and ran test_value: 1 test OK.", finish_reason="stop", usage=usage)])
            name, arguments = calls[index]

            def response():
                yield chunk(tool_calls=[dict(index=0, id=f"call-{index}", name=name, arguments=json.dumps(arguments))], finish_reason="tool_calls")
                tail_seen.append(index)
                yield {"choices": [], "usage": usage}
                if scenario == "read_error" and index == 0:
                    raise TimeoutError("SYNTHETIC_NETWORK_SECRET")
            return response()

    async def run(agent, messages):
        task = asyncio.create_task(agent.run(messages, stream=True))
        assert await asyncio.to_thread(entered.wait, 3)
        assert (tmp_path / "value.py").read_text().endswith("return 1\n")
        clock.advance(601 if scenario == "hard" else 343.000612)
        if scenario == "cancel":
            task.cancel()
        release.set()
        try:
            return await asyncio.wait_for(task, 10)
        except asyncio.CancelledError:
            assert scenario == "cancel"
            return {"status": "cancelled"}
        finally:
            release.set()

    messages = [{"role": "user", "content": "Change value to return 2, add and run a unittest, then report the result."}]
    with scoped_workdir(tmp_path):
        agent = AgentLoop("offline-fake-key", client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
            permission_mode="default", permission_asker=ask, session_id="stream-regression", trace_enabled=False)
        try:
            result = asyncio.run(run(agent, messages))
            persisted = load_session("stream-regression")
        finally:
            release.set()
            agent.close()
    assert asks.count("edit_file") == 1
    assert "SYNTHETIC_NETWORK_SECRET" not in repr(persisted)
    if scenario in {"deny", "cancel"}:
        assert (tmp_path / "value.py").read_text().endswith("return 1\n")
        assert not (tmp_path / "test_value.py").exists()
        assert len(requests) <= 2
        assert "stream_error" not in repr(persisted)
    elif scenario in {"hard", "read_error"}:
        assert result["status"] == "error", result
        assert (tmp_path / "value.py").read_text().endswith("return 2\n")
        assert not (tmp_path / "test_value.py").exists() and len(requests) == 1
        errors = [m[ASSISTANT_ERROR_KEY] for m in persisted["messages"] if m.get(ASSISTANT_ERROR_KEY)]
        public = errors[-1]["data"]["public_error"]
        assert public["metadata"]["error_type"] == "TimeoutError"
        assert public["metadata"]["phase"] == "post_tool_stream"
        assert public["retryable"] is False
        assert "statusCode" not in errors[-1]["data"]
        if scenario == "hard":
            assert public["metadata"]["timeout_kind"] == "hard"
            assert public["metadata"]["origin"] == "local_stream_guard"
    else:
        assert result["status"] == "completed", result
        assert len(requests) == 4 and tail_seen == [0, 1, 2]
        assert asks.count("write_file") == 1
        assert (tmp_path / "value.py").read_text().endswith("return 2\n")
        tool_messages = [m for m in persisted["messages"] if m["role"] == "tool"]
        assert any("Ran 1 test" in m.get("content", "") and "OK" in m.get("content", "") for m in tool_messages)
        assert persisted["messages"][-1]["content"] == "Added and ran test_value: 1 test OK."
