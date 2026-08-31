"""Source-level Read tool image-description fallback tests."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nz_coder.foundation import config
from nz_coder.protocol.attachments import make_image_attachment
from nz_coder.protocol.message_schema import attach_message_identity
from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.runtime.execution.tool_executor import ToolExecutionResult


_PNG = b"\x89PNG\r\n\x1a\nread-image-describe"


def _attachment() -> dict:
    return make_image_attachment(_PNG, "image/png", filename="trace.png")


def _result(name: str = "read_file") -> ToolExecutionResult:
    return ToolExecutionResult(
        name=name,
        tool_input={"path": "trace.png"},
        output="Image read successfully",
        executed=True,
        dispatch_failed=False,
        command_failed=False,
        is_write=False,
        title="Read trace.png",
        metadata={"preview": "Image read successfully"},
        attachments=[_attachment()],
    )


def _harness(describer, *, vision: bool = False) -> AgentLoop:
    agent = object.__new__(AgentLoop)
    agent.model_capabilities = ModelCapabilities(
        provider="test",
        model_id="vision" if vision else "text",
        supports_image_input=vision,
    )
    agent.image_describer = describer
    agent.tracer = SimpleNamespace(log=lambda *args, **kwargs: None)
    return agent


def test_read_result_appends_hints_and_infcode_metadata():
    async def describe(_attachment, prompt):
        assert prompt == "Please diagnose this screenshot."
        return "A ValueError traceback points to parser.py:18."

    agent = _harness(describe)
    result = _result()
    dispatched = [(0, {"id": "call-read", "function": {}}, result)]

    interrupted = asyncio.run(agent._describe_read_tool_results_async(
        dispatched,
        [{"role": "user", "content": "Please diagnose this screenshot."}],
    ))

    assert interrupted is False
    assert "Image read successfully" in result.output
    assert '<image_describe filename="trace.png">' in result.output
    assert "parser.py:18" in result.output
    metadata = result.metadata["imageDescribe"]
    assert metadata["tag"] == "image_describe"
    assert metadata["data"]["status"] == "completed"
    item = metadata["data"]["items"][0]
    assert item["source_id"].startswith("part-")
    assert item["status"] == "completed"
    assert result.attachments == [_attachment()]


def test_read_description_failure_is_one_completed_error_hint():
    async def fail(_attachment, _prompt):
        raise RuntimeError("vision endpoint offline")

    agent = _harness(fail)
    result = _result()

    asyncio.run(agent._describe_read_tool_results_async(
        [(0, {"id": "call-read", "function": {}}, result)],
        [{"role": "user", "content": "inspect"}],
    ))

    assert "Image describe failed: vision endpoint offline" in result.output
    item = result.metadata["imageDescribe"]["data"]["items"][0]
    assert item["status"] == "error"


def test_read_fallback_skips_vision_models_and_non_read_tools():
    calls = 0

    async def unexpected(_attachment, _prompt):
        nonlocal calls
        calls += 1
        return "unexpected"

    vision = _harness(unexpected, vision=True)
    read = _result()
    asyncio.run(vision._describe_read_tool_results_async(
        [(0, {"id": "call-read", "function": {}}, read)],
        [],
    ))

    text = _harness(unexpected)
    webfetch = _result("webfetch")
    asyncio.run(text._describe_read_tool_results_async(
        [(0, {"id": "call-web", "function": {}}, webfetch)],
        [],
    ))

    assert calls == 0
    assert read.output == webfetch.output == "Image read successfully"
    assert "imageDescribe" not in read.metadata
    assert "imageDescribe" not in webfetch.metadata


def test_interrupted_description_leaves_read_result_untouched():
    started = asyncio.Event()

    async def wait_forever(_attachment, _prompt):
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    agent = _harness(wait_forever)
    result = _result()

    async def scenario():
        task = asyncio.create_task(agent._describe_read_tool_results_async(
            [(0, {"id": "call-read", "function": {}}, result)],
            [{"role": "user", "content": "inspect"}],
        ))
        await started.wait()
        task.cancel()
        assert await task is True

    asyncio.run(scenario())

    assert result.output == "Image read successfully"
    assert result.metadata == {"preview": "Image read successfully"}
    assert result.attachments == [_attachment()]


def test_async_tool_pipeline_persists_described_read_result(tmp_path, monkeypatch):
    image = tmp_path / "trace.png"
    image.write_bytes(_PNG)
    monkeypatch.setattr(config, "WORKDIR", tmp_path)

    async def describe(_attachment, _prompt):
        return "The screenshot shows RuntimeError: boom."

    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=object(),
        image_describer=describe,
        trace_enabled=False,
        session_id="session-read-describe",
    )
    tool_call = {
        "id": "call-read-image",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": '{"path":"trace.png"}',
        },
    }
    assistant = {"role": "assistant", "content": "", "tool_calls": [tool_call]}
    attach_message_identity(
        assistant,
        "msg-read-describe",
        session_id="session-read-describe",
    )
    processor = SessionProcessor(assistant)
    processor.start_step()
    processor.register_tool_calls([tool_call])
    messages = [{"role": "user", "content": "Diagnose it."}, assistant]

    asyncio.run(agent._execute_tools_async(
        [tool_call],
        messages,
        processor=processor,
    ))

    tool_part = next(
        part for part in assistant["_nz_parts"] if part["type"] == "tool"
    )
    assert tool_part["state"]["status"] == "completed"
    assert "RuntimeError: boom" in tool_part["state"]["output"]
    assert tool_part["state"]["metadata"]["imageDescribe"]["tag"] == "image_describe"
    assert tool_part["state"]["attachments"] == [_attachment()]
    tool_message = next(message for message in messages if message["role"] == "tool")
    assert "RuntimeError: boom" in tool_message["content"]
    assert "_nz_attachments" not in agent._sanitize_messages(messages)[-1]
    agent.close()


def test_pipeline_cancellation_persists_original_read_before_stopping(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "trace.png").write_bytes(_PNG)
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    started = asyncio.Event()

    async def wait_forever(_attachment, _prompt):
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=object(),
        image_describer=wait_forever,
        trace_enabled=False,
        session_id="session-read-cancel",
    )
    tool_call = {
        "id": "call-read-cancel",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": '{"path":"trace.png"}',
        },
    }
    assistant = {"role": "assistant", "content": "", "tool_calls": [tool_call]}
    attach_message_identity(
        assistant,
        "msg-read-cancel",
        session_id="session-read-cancel",
    )
    processor = SessionProcessor(assistant)
    processor.start_step()
    processor.register_tool_calls([tool_call])
    messages = [{"role": "user", "content": "Read it."}, assistant]

    async def scenario():
        task = asyncio.create_task(agent._execute_tools_async(
            [tool_call],
            messages,
            processor=processor,
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    tool_part = next(
        part for part in assistant["_nz_parts"] if part["type"] == "tool"
    )
    assert tool_part["state"]["status"] == "completed"
    assert tool_part["state"]["output"] == "Image read successfully"
    assert "imageDescribe" not in tool_part["state"]["metadata"]
    assert any(
        message.get("role") == "tool"
        and message.get("content") == "Image read successfully"
        for message in messages
    )
    agent.close()
