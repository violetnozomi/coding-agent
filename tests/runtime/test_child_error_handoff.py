"""真实子任务保留本轮安全错误，恢复时不采用上一轮失败。"""
# ruff: noqa: E402
from __future__ import annotations

import copy
import json
import threading

import pytest

# 最小测试环境可缺少模型依赖；先跳过，再加载真实运行时。
httpx = pytest.importorskip("httpx")
openai = pytest.importorskip("openai")

from nz_coder.foundation.capability_lease import capability_leases
from nz_coder.runtime.agent import subagent
from nz_coder.runtime.agent.agent_manager import (
    BackgroundAgentManager,
    scoped_background_agent_manager,
)
from nz_coder.runtime.agent.child_result import CHILD_RESULT_KEY, ChildAgentResult
from nz_coder.runtime.process.workdir import scoped_workdir
from tests.test_subagent import (
    FakeClient,
    FakeMessage,
    FakeResponse,
    _native_child_messages,
)


_PRIVATE_ERROR = "private-provider-token-child-error-handoff"


class _OfflineClient(FakeClient):
    """只隔离外部模型请求；屏障保证读取的是真实在途任务。"""

    def __init__(self, outcome):
        super().__init__([FakeResponse(FakeMessage("current investigation finished"))])
        self.entered = threading.Event()
        self.release = threading.Event()
        original = self.chat.completions.create

        def create(**kwargs):
            self.entered.set()
            assert self.release.wait(5), "test did not release its model request"
            if outcome in {401, 429}:
                response = httpx.Response(
                    outcome,
                    request=httpx.Request("POST", "https://offline.invalid/completions"),
                )
                error_type = openai.AuthenticationError if outcome == 401 else openai.RateLimitError
                raise error_type(_PRIVATE_ERROR, response=response, body={"error": _PRIVATE_ERROR})
            return original(**kwargs)

        self.chat.completions.create = create

    def close(self):
        self.release.set()


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from nz_coder.foundation import config

    monkeypatch.setenv("NZ_PROVIDER_MAX_RETRIES", "0")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "trust.json"))
    monkeypatch.setattr(config, "PROVIDER_MAX_RETRIES", 0)
    monkeypatch.setattr(config, "SUBAGENT_MAX_TURNS", 2)
    monkeypatch.setattr(config, "SUBAGENT_TIMEOUT_SECONDS", 10)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with (
        scoped_workdir(workspace),
        subagent.scoped_parent_context(session_id="parent"),
    ):
        owned = BackgroundAgentManager(workspace, "parent")
        with scoped_background_agent_manager(owned):
            try:
                yield owned
            finally:
                jobs = list(owned._jobs.values())
                owned.close(timeout=3)
                for job in jobs:
                    job.thread.join(2)
                    assert not job.thread.is_alive()


@pytest.fixture
def use_model(monkeypatch):
    clients = []

    def use(outcome):
        client = _OfflineClient(outcome)
        clients.append(client)
        monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: client)
        return client

    try:
        yield use
    finally:
        for client in clients:
            client.release.set()


def _background_failure(manager, client):
    started = manager.start([{"prompt": "inspect the parser", "read_only": True}])
    assert hasattr(started, "metadata"), started
    task_id = started.metadata["task_ids"][0]
    job = manager._jobs[task_id]
    try:
        assert client.entered.wait(3)
        lease_id = job.capability_lease_id
        assert capability_leases().get(lease_id) is not None
        client.release.set()
        assert job.done_event.wait(3)
        job.thread.join(2)
        assert not job.thread.is_alive()
        assert capability_leases().get(lease_id) is None
        assert manager._active_jobs == 0
        assert not job.cancel_event.is_set()
        events = manager.events().metadata["workflow_events"]
        assert sum(event["type"] == "task_terminal" for event in events) == 1
        mail = manager.drain_messages("worker")
        assert len(mail) == 1 and mail[0]["status"] == "error"
        assert manager.drain_messages("worker") == []
        return task_id, manager.wait_for_result(task_id), mail[0]
    finally:
        client.release.set()


def _previous_failure(manager, client):
    task_id, result, _mail = _background_failure(manager, client)
    assert result.status == "error"
    state = manager._load_raw(task_id)
    messages = _native_child_messages(state, "parent", manager.workspace)
    previous = next(
        message for message in reversed(messages)
        if isinstance(message.get("_nz_assistant_error"), dict)
    )
    old_error = previous["_nz_assistant_error"]["data"]["public_error"]
    assert old_error["metadata"]["status_code"] == 401
    # 复现已有版本保存过的旧错误投影；原生历史仍由真实401请求产生。
    state["public_error"] = copy.deepcopy(old_error)
    manager._save(state)
    return task_id, copy.deepcopy(previous)


def _assert_history_preserved(manager, task_id, previous):
    state = manager._load_raw(task_id)
    messages = _native_child_messages(state, "parent", manager.workspace)
    retained = next(
        message for message in messages
        if message.get("_nz_message_id") == previous["_nz_message_id"]
    )
    assert retained["_nz_assistant_error"] == previous["_nz_assistant_error"]
    assert _PRIVATE_ERROR not in json.dumps(messages)
    return state, messages


def test_background_401_keeps_public_error_in_result_status_and_safe_mail(manager, use_model):
    task_id, result, mail = _background_failure(manager, use_model(401))
    assert result.status == "error"
    assert result.public_error is not None
    assert result.public_error["code"] == "provider_error"
    assert result.public_error["metadata"]["status_code"] == 401
    assert result.public_error["retryable"] is False
    status = manager.status([task_id]).metadata["child_results"][0]
    assert status["public_error"] == result.public_error
    state = manager._load_raw(task_id)
    messages = _native_child_messages(state, "parent", manager.workspace)
    native_error = next(
        message["_nz_assistant_error"]["data"]["public_error"]
        for message in reversed(messages)
        if isinstance(message.get("_nz_assistant_error"), dict)
    )
    assert result.public_error == native_error
    assert result.public_error["message"] in mail["content"]
    assert _PRIVATE_ERROR not in json.dumps([status, state, messages, mail])


def test_resumed_429_replaces_previous_401_state_and_preserves_history(manager, use_model):
    task_id, previous = _previous_failure(manager, use_model(401))
    current = use_model(429)
    current.release.set()
    output = subagent.run_subagent("continue the investigation", session_id=task_id)
    result = ChildAgentResult.from_dict(output.metadata[CHILD_RESULT_KEY])
    assert result.status == "error"
    assert result.public_error is not None
    assert result.public_error["metadata"]["status_code"] == 429
    assert result.public_error["retryable"] is True
    state, messages = _assert_history_preserved(manager, task_id, previous)
    assert state["public_error"] == result.public_error
    current_error = next(
        message for message in reversed(messages)
        if isinstance(message.get("_nz_assistant_error"), dict)
    )
    assert current_error["_nz_interaction_run_id"] != previous["_nz_interaction_run_id"]
    assert current_error["_nz_assistant_error"]["data"]["public_error"] == result.public_error
    assert _PRIVATE_ERROR not in json.dumps(result.to_dict())


def test_resumed_success_does_not_reuse_previous_401_error(manager, use_model):
    task_id, previous = _previous_failure(manager, use_model(401))
    current = use_model("success")
    current.release.set()
    output = subagent.run_subagent("continue the investigation", session_id=task_id)
    result = ChildAgentResult.from_dict(output.metadata[CHILD_RESULT_KEY])
    assert result.status == "completed_unverified"
    assert result.public_error is None
    assert "current investigation finished" in result.final_text
    assert "Subagent error:" not in result.final_text
    state, _messages = _assert_history_preserved(manager, task_id, previous)
    assert "public_error" not in state


def test_resumed_cancellation_does_not_reuse_previous_401_error(manager, use_model):
    task_id, previous = _previous_failure(manager, use_model(401))
    current = use_model("success")
    previous_job = manager._jobs[task_id]
    manager._launch(
        manager._load_raw(task_id),
        {"prompt": "continue the investigation", "read_only": True},
    )
    job = manager._jobs[task_id]
    try:
        assert current.entered.wait(3)
        lease_id = job.capability_lease_id
        assert job.settlement_id != previous_job.settlement_id
        assert capability_leases().get(lease_id) is not None
        stopped = manager.stop([task_id], reason="cancel resumed investigation", timeout_ms=0)
        assert stopped.metadata["requested_task_ids"] == [task_id]
        current.release.set()
        assert job.done_event.wait(3)
        result = manager.wait_for_result(task_id)
    finally:
        current.release.set()
    assert result.status == "cancelled"
    assert result.public_error is None
    assert capability_leases().get(lease_id) is None
    assert manager._active_jobs == 0
    mail = manager.drain_messages("worker")
    assert len(mail) == 1 and mail[0]["status"] == "cancelled"
    assert manager.drain_messages("worker") == []
    state, _messages = _assert_history_preserved(manager, task_id, previous)
    assert "public_error" not in state
