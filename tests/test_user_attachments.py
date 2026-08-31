"""Durable user FilePart and same-turn Provider projection tests."""
from __future__ import annotations

from types import SimpleNamespace

from nz_coder.protocol.attachments import MAX_IMAGE_BYTES, make_image_attachment
from nz_coder.state.context import prompt_budget
from nz_coder.protocol.message_schema import (
    attach_file_parts,
    attach_message_identity,
    ensure_message_identities,
    message_records,
)
from nz_coder.providers.anthropic import _convert_messages as anthropic_messages
from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.providers.gemini import _convert_messages as gemini_messages
from nz_coder.providers.openai_responses import _message_input
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.state.input_expansion import (
    resolve_and_apply_budget,
    tag_file_attachments,
)
from nz_coder.protocol.attachments import openai_chat_messages


_PNG = b"\x89PNG\r\n\x1a\nuser-image"


def _attachment(filename: str = "screen.png") -> dict:
    return make_image_attachment(_PNG, "image/png", filename=filename)


def _user_message(message_id: str = "msg-user-image") -> dict:
    message = {"role": "user", "content": "What is shown?"}
    attach_message_identity(message, message_id, session_id="session-image")
    attach_file_parts(message, [_attachment()])
    return message


def test_attachment_tagging_splits_image_filepart_from_text_expansion(tmp_path):
    (tmp_path / "screen.png").write_bytes(_PNG)
    (tmp_path / "notes.txt").write_text("important text", encoding="utf-8")
    message = {"role": "user", "content": "review both"}
    attach_message_identity(message, "msg-mixed", session_id="session-image")

    count = tag_file_attachments(
        message,
        "review both",
        [
            SimpleNamespace(path="screen.png", size=len(_PNG)),
            SimpleNamespace(path="notes.txt", size=14),
        ],
        workspace=tmp_path,
    )
    stats = resolve_and_apply_budget(
        [message],
        prompt_budget(context_tokens=64_000, output_tokens=8_000),
        tmp_path,
    )

    assert count == 2
    assert [item["kind"] for item in message["_nz_input_expansions"]] == [
        "image",
        "file",
    ]
    assert "[Attached image: screen.png]" in message["content"]
    assert "important text" in message["content"]
    assert stats["resolved"] == 1
    file_part = next(part for part in message["_nz_parts"] if part["type"] == "file")
    assert file_part["mime"] == "image/png"
    assert file_part["filename"] == "screen.png"


def test_oversized_and_fifth_user_images_are_notes_not_inline_payloads(tmp_path):
    attachments = []
    for index in range(5):
        path = tmp_path / f"image-{index}.png"
        path.write_bytes(_PNG)
        attachments.append(SimpleNamespace(path=path.name, size=len(_PNG)))
    large = tmp_path / "large.png"
    large.write_bytes(_PNG)
    large.open("ab").truncate(MAX_IMAGE_BYTES)
    attachments.append(SimpleNamespace(path=large.name, size=MAX_IMAGE_BYTES))
    message = {"role": "user", "content": "review"}
    attach_message_identity(message, "msg-many", session_id="session-image")

    tag_file_attachments(message, "review", attachments, workspace=tmp_path)

    files = [part for part in message["_nz_parts"] if part["type"] == "file"]
    notes = [item["text"] for item in message["_nz_input_expansions"]]
    assert len(files) == 4
    assert "exceeded the attachment count" in notes[4]
    assert "10 MB or larger" in notes[5]


def test_attachment_size_uses_workspace_stat_not_client_hint(tmp_path):
    """A stale remote size hint must not make the server read a huge image."""
    large = tmp_path / "stale-size.png"
    large.write_bytes(_PNG)
    large.open("ab").truncate(MAX_IMAGE_BYTES)
    message = {"role": "user", "content": "review"}
    attach_message_identity(message, "msg-stale-size", session_id="session-image")

    tag_file_attachments(
        message,
        "review",
        [SimpleNamespace(path=large.name, size=1)],
        workspace=tmp_path,
    )

    assert message["_nz_parts"] == []
    assert "10 MB or larger" in message["_nz_input_expansions"][0]["text"]


def test_user_filepart_survives_projection_and_invalid_remote_is_removed():
    message = _user_message()
    record = message_records([message], "session-image")[0]
    file_part = next(part for part in record["parts"] if part["type"] == "file")
    assert file_part["url"] == _attachment()["url"]

    message["_nz_parts"].append({
        "id": "part-remote",
        "message_id": "msg-user-image",
        "type": "file",
        "mime": "image/png",
        "url": "https://example.test/image.png",
    })
    ensure_message_identities([message], "session-image")
    assert len([part for part in message["_nz_parts"] if part["type"] == "file"]) == 1


def test_agent_projects_user_media_only_for_vision_models_and_keeps_merge():
    first = _user_message("msg-user-first")
    second = _user_message("msg-user-second")
    second["content"] = "Compare it with this."
    vision = object.__new__(AgentLoop)
    vision.model_capabilities = ModelCapabilities(
        provider="test",
        model_id="vision",
        supports_image_input=True,
    )
    text = object.__new__(AgentLoop)
    text.model_capabilities = ModelCapabilities(provider="test", model_id="text")

    projected = vision._sanitize_messages([first, second])

    assert len(projected) == 1
    assert len(projected[0]["_nz_user_attachments"]) == 2
    assert "Compare it with this." in projected[0]["content"]
    assert "_nz_user_attachments" not in text._sanitize_messages([first])[0]
    assert "_nz_user_attachments" not in vision._sanitize_messages(
        [first],
        include_attachments=False,
    )[0]


def test_user_media_uses_same_turn_in_all_provider_shapes():
    message = {
        "role": "user",
        "content": "Inspect this image.",
        "_nz_user_attachments": [_attachment()],
    }

    chat = openai_chat_messages([message])
    responses = _message_input([message])
    _anthropic_system, anthropic = anthropic_messages([message])
    _gemini_system, gemini = gemini_messages([message])

    assert chat[0]["role"] == "user"
    assert chat[0]["content"][0] == {
        "type": "text",
        "text": "Inspect this image.",
    }
    assert chat[0]["content"][1]["type"] == "image_url"
    assert responses[0]["content"][1]["type"] == "input_image"
    assert anthropic[0]["content"][1]["type"] == "image"
    assert gemini[0]["parts"][1]["inlineData"]["mimeType"] == "image/png"
