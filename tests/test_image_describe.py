"""InfCode-aligned non-vision image-description preflight tests."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nz_coder.protocol.attachments import make_image_attachment
from nz_coder.protocol.message_schema import (
    PARTS_KEY,
    attach_file_parts,
    attach_message_identity,
    attach_text_part,
    message_records,
)
from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.capabilities.vision import ProviderImageDescriber, describe_images


_PNG = b"\x89PNG\r\n\x1a\nimage-describe"


def _user(message_id: str = "msg-image-source") -> dict:
    message = {"role": "user", "content": "Explain the error in this screenshot."}
    attach_message_identity(message, message_id, session_id="session-vision")
    attach_file_parts(message, [
        make_image_attachment(_PNG, "image/png", filename="error.png")
    ])
    return message


def _assistant(message_id: str = "msg-image-owner") -> dict:
    message = {"role": "assistant", "content": ""}
    attach_message_identity(message, message_id, session_id="session-vision")
    return message


def _loop(describer) -> AgentLoop:
    agent = object.__new__(AgentLoop)
    agent.model_capabilities = ModelCapabilities(provider="test", model_id="text")
    agent.image_describer = describer
    agent.session_id = "session-vision"
    agent.tracer = SimpleNamespace(log=lambda *args, **kwargs: None)
    agent._emit_session_event = lambda *args, **kwargs: None
    agent._checkpoint_messages = lambda *args, **kwargs: None
    return agent


def test_describe_images_keeps_per_item_failure_and_completes_batch():
    files = [
        make_image_attachment(_PNG + bytes([index]), "image/png", filename=f"{index}.png")
        for index in range(2)
    ]
    calls = 0
    snapshots = []

    async def describe(_file, _prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("vision unavailable")
        return "A Python traceback is visible."

    state = asyncio.run(describe_images(
        files,
        source_ids=["part-source-1", "part-source-2"],
        describe=describe,
        on_progress=snapshots.append,
    ))

    assert state["status"] == "completed"
    assert [item["status"] for item in state["items"]] == ["error", "completed"]
    assert state["items"][0]["error"] == "An internal error occurred."
    assert snapshots[0]["status"] == "running"
    assert snapshots[-1]["status"] == "completed"


def test_describe_images_persists_interrupted_terminal_state():
    snapshots = []

    async def never_finishes(_file, _prompt):
        await asyncio.Event().wait()
        return "unreachable"

    async def scenario():
        task = asyncio.create_task(describe_images(
            [make_image_attachment(_PNG, "image/png", filename="wait.png")],
            source_ids=["part-source"],
            describe=never_finishes,
            on_progress=snapshots.append,
        ))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert snapshots[-1]["status"] == "interrupted"
    assert snapshots[-1]["items"][0]["status"] == "error"
    assert "Interrupted" in snapshots[-1]["items"][0]["error"]


def test_nonvision_preflight_is_durable_idempotent_and_reinjected():
    calls = 0

    async def describe(_file, prompt):
        nonlocal calls
        calls += 1
        assert "Explain the error" in prompt
        return "The screenshot shows ValueError on line 12."

    agent = _loop(describe)
    source = _user()
    first_owner = _assistant()
    messages = [source, first_owner]

    result = asyncio.run(agent._prepare_user_image_descriptions(messages, first_owner))

    assert result == "described"
    description = next(
        part for part in first_owner[PARTS_KEY]
        if part.get("metadata", {}).get("image_describe")
    )
    detail = description["metadata"]["image_describe"]
    assert detail["status"] == "completed"
    assert detail["source_message_id"] == source["_nz_message_id"]
    assert "ValueError on line 12" in description["text"]
    projected = agent._sanitize_messages(messages)
    assert "<image_describe filename=\"error.png\">" in projected[0]["content"]
    assert "ValueError on line 12" in projected[0]["content"]
    assert "_nz_user_attachments" not in projected[0]

    attach_text_part(first_owner, {
        "id": "part-main-response",
        "message_id": first_owner["_nz_message_id"],
        "type": "text",
        "text": "Use the traceback to fix line 12.",
    })
    assert [part["id"] for part in first_owner[PARTS_KEY]][:2] == [
        "part-main-response",
        description["id"],
    ]

    second_owner = _assistant("msg-image-owner-2")
    messages.append(second_owner)
    assert asyncio.run(
        agent._prepare_user_image_descriptions(messages, second_owner)
    ) == "skipped"
    assert calls == 1
    assert not any(
        part.get("metadata", {}).get("image_describe")
        for part in second_owner[PARTS_KEY]
    )


def test_vision_model_skips_description_and_keeps_original_media():
    async def unexpected(_file, _prompt):
        raise AssertionError("vision model must not invoke fallback")

    agent = _loop(unexpected)
    agent.model_capabilities = ModelCapabilities(
        provider="test",
        model_id="vision",
        supports_image_input=True,
    )
    source = _user()
    owner = _assistant()

    assert asyncio.run(
        agent._prepare_user_image_descriptions([source, owner], owner)
    ) == "skipped"
    assert agent._sanitize_messages([source, owner])[0]["_nz_user_attachments"]


def test_provider_image_describer_uses_vision_capability_and_no_tools():
    requests = []
    observed = []

    class Provider:
        name = "test"

        def capabilities(self, model_id):
            return ModelCapabilities(
                provider="test",
                model_id=model_id,
                supports_image_input=True,
            )

        def create_client(self):
            return object()

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            message = SimpleNamespace(content="A terminal error is visible.")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    descriptor = ProviderImageDescriber(
        "test",
        "vision-model",
        provider=Provider(),
        client=object(),
        max_tokens=321,
        observer=lambda name, payload: observed.append((name, payload)),
    )
    result = asyncio.run(descriptor(
        make_image_attachment(_PNG, "image/png", filename="terminal.png"),
        "Read this screenshot.",
    ))

    assert result == "A terminal error is visible."
    assert requests[0]["model"] == "vision-model"
    assert requests[0]["max_tokens"] == 321
    assert requests[0]["stream"] is False
    assert "tools" not in requests[0]
    assert requests[0]["messages"][0]["_nz_user_attachments"]
    finishes = [payload for name, payload in observed if name == "model_call_finish"]
    assert len(finishes) == 1
    assert finishes[0]["purpose"] == "vision"


def test_provider_image_describer_cancels_gateway_poll_on_task_cancel():
    """Cancelling image preflight must not wait for the Provider hard timeout."""
    import threading

    started = threading.Event()
    release = threading.Event()
    observed = []

    class Provider:
        name = "test"

        def capabilities(self, model_id):
            return ModelCapabilities(
                provider="test",
                model_id=model_id,
                supports_image_input=True,
            )

        def create_client(self):
            return object()

        def create_completion(self, _client, **_kwargs):
            started.set()
            release.wait(timeout=2)
            message = SimpleNamespace(content="late")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    descriptor = ProviderImageDescriber(
        "test",
        "vision-model",
        provider=Provider(),
        client=object(),
        observer=lambda name, payload: observed.append((name, payload)),
    )

    async def scenario():
        task = asyncio.create_task(descriptor(
            make_image_attachment(_PNG, "image/png", filename="terminal.png"),
            "Read this screenshot.",
        ))
        while not started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(scenario())
    finally:
        release.set()

    finishes = [payload for name, payload in observed if name == "model_call_finish"]
    assert len(finishes) == 1
    assert finishes[0]["purpose"] == "vision"
    assert finishes[0]["status"] == "cancelled"


def test_agent_run_describes_before_main_nonvision_request(tmp_path, monkeypatch):
    from nz_coder.foundation import config

    requests = []

    class Provider:
        name = "test"

        def capabilities(self, model_id):
            return ModelCapabilities(provider="test", model_id=model_id)

        def create_client(self):
            return object()

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            message = SimpleNamespace(
                content="I will use the visible traceback.",
                tool_calls=[],
                reasoning_content=None,
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    async def describe(_file, _prompt):
        return "The image contains RuntimeError: boom."

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        provider=Provider(),
        client=object(),
        image_describer=describe,
        trace_enabled=False,
        session_id="session-image-run",
    )
    source = _user("msg-image-run-source")
    source["_nz_session_id"] = "session-image-run"
    messages = [source]

    result = asyncio.run(agent.run(messages, stream=False))

    assert result["status"] == "completed"
    user_request = next(
        message for message in requests[0]["messages"] if message["role"] == "user"
    )
    assert "RuntimeError: boom" in user_request["content"]
    assert "_nz_user_attachments" not in user_request
    owner = next(message for message in messages if message["role"] == "assistant")
    assert len([part for part in owner[PARTS_KEY] if part["type"] == "text"]) == 2


def test_image_describe_metadata_survives_public_session_projection():
    source = _user()
    owner = _assistant()
    owner[PARTS_KEY].append({
        "id": "part-image-describe",
        "message_id": owner["_nz_message_id"],
        "type": "text",
        "text": '<image_describe filename="error.png">ok</image_describe>',
        "metadata": {"image_describe": {
            "status": "completed",
            "source_message_id": source["_nz_message_id"],
            "items": [{
                "source_id": next(
                    part["id"] for part in source[PARTS_KEY] if part["type"] == "file"
                ),
                "filename": "error.png",
                "mime": "image/png",
                "status": "completed",
                "text": "ok",
            }],
        }},
    })

    records = message_records([source, owner], "session-vision")

    metadata = records[1]["parts"][0]["metadata"]["image_describe"]
    assert metadata["status"] == "completed"
    assert metadata["items"][0]["text"] == "ok"


def _image_settings(tmp_path, workspace, environment):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.runtime.core.run_settings import RunSettings

    return RunSettings.from_snapshot(load_config_snapshot(
        workspace,
        environ=environment,
        user_config_path=tmp_path / "missing-user.env",
    ))


def test_image_provider_empty_specific_key_uses_target_run_credential(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = _image_settings(tmp_path, workspace, {
        "MODEL_PROVIDER": "deepseek",
        "API_KEY": "RUN-SECRET",
        "NZ_IMAGE_DESCRIBE_PROVIDER": "deepseek",
        "NZ_IMAGE_DESCRIBE_MODEL": "vision",
    })

    descriptor = ProviderImageDescriber.configured(run_settings=settings)

    assert descriptor.connection.api_key == "RUN-SECRET"
    assert descriptor.connection.credential_source == "environment"


def test_image_provider_endpoint_does_not_bleed_between_workspaces(tmp_path):
    first_workspace = tmp_path / "a"
    second_workspace = tmp_path / "b"
    first_workspace.mkdir()
    second_workspace.mkdir()
    first = ProviderImageDescriber.configured(run_settings=_image_settings(
        tmp_path, first_workspace, {
            "API_KEY": "A-SECRET",
            "API_BASE_URL": "https://a.invalid/v1",
            "NZ_IMAGE_DESCRIBE_MODEL": "vision",
        },
    ))
    second = ProviderImageDescriber.configured(run_settings=_image_settings(
        tmp_path, second_workspace, {
            "API_KEY": "B-SECRET",
            "API_BASE_URL": "https://b.invalid/v1",
            "NZ_IMAGE_DESCRIBE_MODEL": "vision",
        },
    ))

    assert (first.connection.api_key, first.connection.base_url) == (
        "A-SECRET", "https://a.invalid/v1",
    )
    assert (second.connection.api_key, second.connection.base_url) == (
        "B-SECRET", "https://b.invalid/v1",
    )
    assert first.runtime_identity != second.runtime_identity


def test_image_runtime_rotates_after_specific_key_change(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = ProviderImageDescriber.configured(run_settings=_image_settings(
        tmp_path, workspace, {
            "API_KEY": "shared",
            "NZ_IMAGE_DESCRIBE_API_KEY": "IMAGE-A",
            "NZ_IMAGE_DESCRIBE_MODEL": "vision",
        },
    ))
    second = ProviderImageDescriber.configured(run_settings=_image_settings(
        tmp_path, workspace, {
            "API_KEY": "shared",
            "NZ_IMAGE_DESCRIBE_API_KEY": "IMAGE-B",
            "NZ_IMAGE_DESCRIBE_MODEL": "vision",
        },
    ))

    assert first.runtime_identity != second.runtime_identity
    assert "IMAGE-A" not in repr(first.connection)


def test_image_workspace_endpoint_requires_credential_delegation(tmp_path):
    from nz_coder.foundation.workspace_trust import (
        ConfigValidationError,
        WorkspaceTrustStore,
        load_config_snapshot,
    )
    from nz_coder.runtime.core.run_settings import RunSettings

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "NZ_IMAGE_DESCRIBE_BASE_URL=https://workspace.invalid/v1\n",
        encoding="utf-8",
    )
    trust = WorkspaceTrustStore(tmp_path / "trust.json")
    pending = load_config_snapshot(
        workspace, environ={"API_KEY": "OWNER-SECRET"}, trust_store=trust,
    )
    trust.trust(workspace, "workspace-config", pending.workspace_fingerprint)
    trusted_config = load_config_snapshot(
        workspace, environ={"API_KEY": "OWNER-SECRET"}, trust_store=trust,
    )

    with pytest.raises(ConfigValidationError, match="delegation"):
        ProviderImageDescriber.configured(
            run_settings=RunSettings.from_snapshot(trusted_config),
        )


def test_image_provider_construction_failure_closes_created_provider(monkeypatch):
    closed = []

    class Provider:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "nz_coder.capabilities.vision.create_provider", lambda *_args, **_kwargs: Provider(),
    )
    monkeypatch.setattr(
        "nz_coder.capabilities.vision.resolve_model_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("primary")),
    )
    descriptor = ProviderImageDescriber("deepseek", "vision")

    with pytest.raises(RuntimeError, match="primary"):
        descriptor._describe_sync(
            make_image_attachment(_PNG, "image/png", filename="failure.png"),
            "describe",
        )

    assert closed == [True]
