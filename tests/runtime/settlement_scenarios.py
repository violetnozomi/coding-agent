"""真实持久会话场景；每次解析都生成新的离线Runtime，避免复用已退休对象。"""

import json
from dataclasses import replace

from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot
from nz_coder.protocol.message_schema import MODEL_NOTE_KEY
from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state import sessions
from tests.runtime.test_native_product_runtime import _runtime, _request
from tests.runtime.test_working_memory_reminders import _reply as _reply, _call as _call


def check_settled_reply(message, original):
    assert message["role"] == "assistant" and message[MODEL_NOTE_KEY] == original
    answer = message["content"]
    assert "本轮没有已采信的测试结果" in answer and original not in answer
    return answer


def setup(monkeypatch, workspace, provider):
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(workspace.parent / (workspace.name + "-" + name)))
    config_path = workspace.parent / (workspace.name + "-XDG_CONFIG_HOME") / "config.env"
    store_path = config_path.with_name("workspace-trust.json")
    monkeypatch.setenv("NZ_CODER_USER_CONFIG", str(config_path))
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(store_path))

    def runtime(_selection):
        return replace(_runtime(), provider=provider)

    monkeypatch.setattr(native_sdk, "resolve_model_runtime", runtime)
    monkeypatch.setattr(
        "nz_coder.runtime.execution.loop.resolve_model_runtime", runtime
    )
    snapshot = load_config_snapshot(
        workspace,
        user_config_path=config_path,
        trust_store=WorkspaceTrustStore(store_path),
        environ={
            "NZ_PLANNING_ENABLED": "0",
            "RUNTIME_STATE_PERSIST": "1",
            "NZ_WORKING_MEMORY_REMINDER_STEPS": "0",
        },
    )
    request = replace(
        _request(workspace),
        session_id="resume-test",
        metadata={
            "permission_mode": "auto",
            "persist_session": True,
        },
    )
    return request, RunOptions(
        config_snapshot=snapshot, permission_asker=lambda *_: "once"
    )


def payload(workspace, request):
    with scoped_workdir(workspace):
        return json.loads(
            (sessions.session_dir() / (request.session_id + ".json")).read_text()
        )
