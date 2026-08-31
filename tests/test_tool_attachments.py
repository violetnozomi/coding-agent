"""End-to-end tests for read-image FileParts and provider replay."""
from __future__ import annotations

import pytest

from nz_coder.protocol.attachments import (
    MAX_IMAGE_BYTES,
    make_image_attachment,
    openai_chat_messages,
)
from nz_coder.protocol.message_schema import attach_message_identity, message_records
from nz_coder.providers.anthropic import _convert_messages as anthropic_messages
from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.providers.gemini import _convert_messages as gemini_messages
from nz_coder.providers.openai_responses import _message_input
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools import ToolOutput
from nz_coder.tools.files import read_file


_PNG = b"\x89PNG\r\n\x1a\n" + b"test-image-payload"


def _attachment() -> dict:
    return make_image_attachment(_PNG, "image/png", filename="pixel.png")


def _history() -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-image",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"pixel.png"}'},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-image",
            "content": "Image read successfully",
            "_nz_attachments": [_attachment()],
        },
    ]


def test_read_file_returns_validated_image_attachment(tmp_path):
    (tmp_path / "pixel.png").write_bytes(_PNG)

    with scoped_workdir(tmp_path):
        result = read_file("pixel.png")

    assert isinstance(result, ToolOutput)
    assert result == "Image read successfully"
    assert result.title == "Read pixel.png"
    assert result.attachments == [_attachment()]


def test_read_file_rejects_oversized_image_before_loading_payload(tmp_path):
    image = tmp_path / "large.png"
    image.write_bytes(_PNG)
    image.open("ab").truncate(MAX_IMAGE_BYTES)

    with scoped_workdir(tmp_path):
        result = read_file("large.png")

    assert result.startswith("Error: Image size must be less than 10 MB")


def test_attachment_validation_rejects_remote_and_malformed_payloads():
    with pytest.raises(ValueError, match="data URL"):
        ToolOutput(
            "unsafe",
            attachments=[{"type": "file", "mime": "image/png", "url": "https://x/y"}],
        )
    with pytest.raises(ValueError, match="base64"):
        ToolOutput(
            "unsafe",
            attachments=[{
                "type": "file",
                "mime": "image/png",
                "url": "data:image/png;base64,not+valid===",
            }],
        )


def test_completed_tool_attachment_survives_session_projection():
    assistant = _history()[0]
    attach_message_identity(assistant, "msg-image", session_id="session-image")
    processor = SessionProcessor(assistant)
    processor.register_tool_calls(assistant["tool_calls"])
    processor.start_tools(assistant["tool_calls"])
    processor.complete_tool(
        "call-image",
        "Image read successfully",
        title="Read pixel.png",
        attachments=[_attachment()],
    )

    projected = message_records([assistant], "session-image")[0]["parts"]
    tool = next(part for part in projected if part["type"] == "tool")
    assert tool["state"]["attachments"] == [_attachment()]


def test_agent_execution_persists_attachment_on_tool_part_and_history(tmp_path):
    (tmp_path / "pixel.png").write_bytes(_PNG)
    assistant = _history()[0]
    attach_message_identity(assistant, "msg-image-run", session_id="session-image-run")
    processor = SessionProcessor(assistant)
    processor.start_step()
    processor.register_tool_calls(assistant["tool_calls"])
    messages = [{"role": "user", "content": "read image"}, assistant]

    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=object(),
            trace_enabled=False,
            session_id="session-image-run",
        )
        agent._execute_tools(
            assistant["tool_calls"],
            messages,
            processor=processor,
        )
        agent.model_capabilities = ModelCapabilities(
            provider="test",
            model_id="vision",
            supports_image_input=True,
        )
        sanitized = agent._sanitize_messages(messages)
        agent.close()

    tool_message = next(item for item in messages if item.get("role") == "tool")
    assert "_nz_attachments" not in tool_message
    tool_part = next(part for part in assistant["_nz_parts"] if part["type"] == "tool")
    assert tool_part["state"]["attachments"] == [_attachment()]
    provider_tool = next(item for item in sanitized if item.get("role") == "tool")
    assert provider_tool["_nz_attachments"] == [_attachment()]


def test_agent_filters_media_by_model_capability_and_compaction():
    vision = object.__new__(AgentLoop)
    vision.model_capabilities = ModelCapabilities(
        provider="test",
        model_id="vision",
        supports_image_input=True,
    )
    text = object.__new__(AgentLoop)
    text.model_capabilities = ModelCapabilities(provider="test", model_id="text")

    assert vision._sanitize_messages(_history())[-1]["_nz_attachments"] == [_attachment()]
    assert "_nz_attachments" not in text._sanitize_messages(_history())[-1]
    compacted = _history()
    compacted[-1]["_nz_tool_compacted_at"] = 1.0
    assert "_nz_attachments" not in vision._sanitize_messages(compacted)[-1]
    assert "_nz_attachments" not in vision._sanitize_messages(
        _history(),
        include_attachments=False,
    )[-1]


def test_openai_chat_and_responses_inject_media_after_tool_results():
    chat = openai_chat_messages(_history())
    assert [item["role"] for item in chat] == ["assistant", "tool", "user"]
    assert chat[-1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": _attachment()["url"]},
    }
    response_input = _message_input(_history())
    assert response_input[-1]["role"] == "user"
    assert response_input[-1]["content"][1] == {
        "type": "input_image",
        "image_url": _attachment()["url"],
    }


def test_openai_media_waits_for_all_consecutive_tool_results():
    messages = [
        _history()[0],
        _history()[1],
        {"role": "tool", "tool_call_id": "call-text", "content": "done"},
    ]

    chat = openai_chat_messages(messages)

    assert [item["role"] for item in chat] == ["assistant", "tool", "tool", "user"]


def test_anthropic_keeps_image_inside_native_tool_result():
    _system, converted = anthropic_messages(_history())
    tool_result = converted[-1]["content"][0]

    assert tool_result["type"] == "tool_result"
    assert tool_result["content"][0]["text"] == "Image read successfully"
    assert tool_result["content"][1]["type"] == "image"
    assert tool_result["content"][1]["source"]["media_type"] == "image/png"


def test_gemini_replays_function_response_and_inline_image():
    _system, converted = gemini_messages(_history())
    parts = converted[-1]["parts"]

    assert parts[0]["functionResponse"]["id"] == "call-image"
    assert parts[1]["text"].startswith("The following images")
    assert parts[2]["inlineData"]["mimeType"] == "image/png"
    assert parts[2]["inlineData"]["data"]
