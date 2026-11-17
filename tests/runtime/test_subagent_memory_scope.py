"""父子工作树共享逻辑记忆，能力与执行目录保持各自边界。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nz_coder.runtime.agent import subagent
from nz_coder.runtime.agent.process_supervisor import _run_subagent_process as _actual_process_entry
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.session.model import SessionIdentity
from nz_coder.runtime.session.store import LegacyJsonSessionStore
from nz_coder.state.memory_scope import MemoryScope, scoped_memory_scope
from nz_coder.state.memory_store import SQLiteMemoryStore
from tests.test_subagent import FakeClient, FakeMessage, FakeResponse, FakeToolCall


def _ctx(scope, operation):
    return {"capability": "manage", "scope_id": scope.scope_id,
            "operation_namespace": "parent", "operation_id": operation}


def _store(scope, execution_root):
    return SQLiteMemoryStore(scope.directory(execution_root), scope_id=scope.scope_id)


def _fake_memory_client():
    return FakeClient([
        FakeResponse(FakeMessage(tool_calls=[FakeToolCall("save_memory", {
            "action": "create", "name": "child finding", "type": "project",
            "description": "offline observation", "content": "reuse local cache"})])),
        FakeResponse(FakeMessage("Read-only investigation complete.")),
    ])


def _configure(monkeypatch):
    from nz_coder.foundation import config
    monkeypatch.setattr(config, "SUBAGENT_MAX_TURNS", 3)
    monkeypatch.setattr(config, "SUBAGENT_TIMEOUT_SECONDS", 15)
    monkeypatch.setattr(config, "SUBAGENT_WORKTREE_ENABLED", True)
    monkeypatch.setattr(config, "PLANNING_ENABLED", False)


def test_parent_scope_roundtrips_in_child_state_without_changing_workdir(tmp_path):
    owner, root = tmp_path / "owner", tmp_path / "child-worktree"
    owner.mkdir()
    root.mkdir()
    scope = MemoryScope.for_workspace(owner)
    with scoped_workdir(root), scoped_parent(scope):
        state = subagent._new_subagent_state("parent", "explore", None)
        assert state["memory_scope"] == scope.to_dict()
        subagent._save_subagent_state("parent", state, root)
        assert subagent._load_subagent_state("parent", state["session_id"], root)["memory_scope"] == scope.to_dict()
        assert subagent._workspace_root() == root


def scoped_parent(scope):
    return subagent.scoped_parent_context(session_id="parent", memory_scope=scope.to_dict())


@pytest.mark.parametrize("agent_type,background,workflow,capability", [
    ("explore", False, "", "read"),
    ("general-purpose", False, "", "propose"),
    ("explore", True, "", "read"),
    ("general-purpose", True, "workflow-one", "propose"),
])
def test_real_child_uses_parent_database_and_type_capability(tmp_path, monkeypatch, agent_type, background, workflow, capability):
    from nz_coder.runtime.execution.loop import ProductRunEnvironment
    _configure(monkeypatch)
    owner, root = tmp_path / "owner", tmp_path / "execution"
    owner.mkdir()
    root.mkdir()
    (root / "config.py").write_text("VALUE = 1\n")
    scope = MemoryScope.for_workspace(owner)
    store = _store(scope, root)
    parent = store.mutate({"action": "create", "name": "parent note", "content": "keep original"}, _ctx(scope, "create"))
    assert parent["status"] == "saved"
    captured = []
    original_run = ProductRunEnvironment.run

    async def capture(self, messages, **kwargs):
        captured.append((self.memory_scope, Path(self.workdir), self.memory_capability, self._mm.memory_dir))
        return await original_run(self, messages, **kwargs)

    monkeypatch.setattr(ProductRunEnvironment, "run", capture)
    fake = _fake_memory_client()
    monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)
    with scoped_workdir(root), scoped_parent(scope), scoped_memory_scope(scope):
        state = subagent._new_subagent_state("parent", agent_type, None)
        state.update(background=background, workflow_run_id=workflow)
        subagent._save_subagent_state("parent", state, root)
        result = subagent.run_subagent("Investigate the local code and record useful background.",
            session_id=state["session_id"], agent_type=agent_type,
            target_paths=["config.py"] if capability == "propose" else None)
    assert "[Subagent status: completed_unverified]" in result, result
    assert captured and captured[0][0] == scope and captured[0][2] == capability
    assert captured[0][3] == scope.directory(root)
    if capability == "propose":
        assert captured[0][1] != root
    assert [item["id"] for item in store.list_records()["items"]] == [parent["memory_id"]]
    proposals = store.proposals(filters={"source_session": state["session_id"]})["items"]
    assert len(proposals) == (1 if capability == "propose" else 0)
    native = asyncio.run(LegacyJsonSessionStore().load(SessionIdentity(state["session_id"], "parent"), captured[0][1]))
    assert native.metadata["memory_scope"] == scope.to_dict()
    assert result.metadata["memory_proposal_source_session"] == state["session_id"]
    assert {"recall_memory", "list_memories"} <= {tool["function"]["name"] for tool in fake.chat.completions.requests[0]["tools"]}


def test_resume_rejects_scope_mismatch_before_model_or_file_execution(tmp_path, monkeypatch):
    _configure(monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    scope = MemoryScope.for_workspace(tmp_path)
    with scoped_workdir(tmp_path), scoped_parent(scope):
        state = subagent._new_subagent_state("parent", "explore", None)
        state["memory_scope"] = MemoryScope.for_workspace(other).to_dict()
        subagent._save_subagent_state("parent", state, tmp_path)
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: pytest.fail("作用域不匹配不得启动模型"))
        result = subagent.run_subagent("resume", session_id=state["session_id"])
    assert "memory scope" in result.lower()
    assert "Error" in result


# spawn 会重新导入此模块；只替换离线模型，保留真正的进程入口与通道。
def _offline_process_entry(result_path, payload, endpoint):
    subagent.OpenAI = lambda **kwargs: _fake_memory_client()
    _actual_process_entry(result_path, payload, endpoint)


@pytest.mark.parametrize("isolation", ["thread", "process"])
def test_real_background_execution_keeps_inherited_scope_after_parent_unbinds(tmp_path, monkeypatch, isolation):
    from nz_coder.runtime.agent import process_supervisor
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    _configure(monkeypatch)
    owner, root = tmp_path / "owner", tmp_path / "execution"
    owner.mkdir()
    root.mkdir()
    (root / "config.py").write_text("VALUE = 1\n")
    scope = MemoryScope.for_workspace(owner)
    store = _store(scope, root)
    monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: _fake_memory_client())
    if isolation == "process":
        monkeypatch.setattr(process_supervisor, "_run_subagent_process", _offline_process_entry)
    manager = BackgroundAgentManager(root, "parent")
    try:
        with scoped_workdir(root), scoped_parent(scope), scoped_memory_scope(scope):
            started = manager.start([{"prompt": "Investigate code and record useful background.",
                "target_paths": ["config.py"], "isolation": isolation, "workflow_run_id": "workflow-one"}])
        session_id = started.metadata["task_ids"][0]
        assert manager._jobs[session_id].done_event.wait(20), manager.status([session_id])
        state = manager._load_raw(session_id)
        assert state["status"] == "completed_unverified", manager.status([session_id])
        assert state["memory_scope"] == scope.to_dict()
        assert len(store.proposals(filters={"source_session": session_id})["items"]) == 1
        assert store.list_records()["items"] == []
        assert manager._jobs[session_id].process is not None if isolation == "process" else manager._jobs[session_id].process is None
    finally:
        manager.close(timeout=3)


def test_native_child_history_rejects_a_different_saved_scope(tmp_path):
    from nz_coder.runtime.session.model import Session
    from nz_coder.runtime.session.store import EphemeralSessionStore
    other = tmp_path / "other"
    other.mkdir()
    scope = MemoryScope.for_workspace(tmp_path)
    session = Session.create("child", [], workspace=tmp_path, parent_session_id="parent")
    session.metadata["memory_scope"] = MemoryScope.for_workspace(other).to_dict()
    store = EphemeralSessionStore()
    asyncio.run(store.save(session))
    state = {"session_id": "child", "parent_session_id": "parent", "memory_scope": scope.to_dict()}
    with pytest.raises(ValueError, match="memory scope"):
        subagent._child_activation_messages(state, "resume", workspace=tmp_path, store=store)


def test_legacy_child_state_rebinds_only_to_trusted_parent_and_resume_keeps_proposals(tmp_path, monkeypatch):
    from nz_coder.runtime.worktree import WorktreeManager
    _configure(monkeypatch)
    owner, root = tmp_path / "owner", tmp_path / "execution"
    owner.mkdir()
    root.mkdir()
    (root / "config.py").write_text("VALUE = 1\n")
    scope = MemoryScope.for_workspace(owner)
    store = _store(scope, root)
    monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: _fake_memory_client())
    with scoped_workdir(root), scoped_parent(scope), scoped_memory_scope(scope):
        first = subagent.run_subagent("Record useful project background.", agent_type="general-purpose", target_paths=["config.py"])
        child = first.metadata["memory_proposal_source_session"]
        state = subagent._load_subagent_state("parent", child, root)
        state.pop("memory_scope")
        subagent._save_subagent_state("parent", state, root)
        resumed = subagent.run_subagent("Record the next observation.", session_id=child)
        assert "[Subagent status: completed_unverified]" in resumed, resumed
        restored = subagent._load_subagent_state("parent", child, root)
        assert restored["memory_scope"] == scope.to_dict()
        assert len(store.proposals(filters={"source_session": child})["items"]) == 2
        assert store.list_records()["items"] == []
        WorktreeManager(root).remove(subagent._validated_persisted_worktree(root, restored))
    reopened = _store(scope, root)
    assert len(reopened.proposals(filters={"source_session": child})["items"]) == 2


def test_read_child_checks_sources_in_its_own_resumed_worktree(tmp_path, monkeypatch):
    import hashlib
    import json
    _configure(monkeypatch)
    (tmp_path / "config.py").write_bytes(b"VALUE = 1\n")
    scope = MemoryScope.for_workspace(tmp_path)
    store = _store(scope, tmp_path)
    target = store.mutate({"action": "create", "name": "config value", "content": "VALUE is one",
        "source_refs": [{"kind": "file", "locator": "config.py", "binding": "bound",
                         "content_hash": hashlib.sha256(b"VALUE = 1\n").hexdigest()}]}, _ctx(scope, "seed"))["memory_id"]
    with scoped_workdir(tmp_path), scoped_parent(scope), scoped_memory_scope(scope):
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: FakeClient([FakeResponse(FakeMessage("Read-only investigation complete."))]))
        first = subagent.run_subagent("Inspect project configuration.", agent_type="explore")
        child = first.metadata["memory_proposal_source_session"]
        state = subagent._load_subagent_state("parent", child, tmp_path)
        child_root = Path(state["worktree"]["path"])
        assert child_root != tmp_path
        (child_root / "config.py").write_bytes(b"VALUE = 2\n")
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[FakeToolCall("recall_memory", {"id": target, "check_sources": True})])),
            FakeResponse(FakeMessage("Read-only investigation complete.")),
        ])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)
        resumed = subagent.run_subagent("Check whether the old configuration source still matches.", session_id=child)
        assert "[Subagent status: completed_unverified]" in resumed, resumed
    native = asyncio.run(LegacyJsonSessionStore().load(SessionIdentity(child, "parent"), child_root))
    payload = next(json.loads(message["content"]) for message in reversed(native.transcript)
                   if message.get("role") == "tool" and '"source_checks"' in message.get("content", ""))
    check = payload["source_checks"][0]
    assert check["status"] == "changed"
    assert check["execution_root"] == str(child_root)
    assert (tmp_path / "config.py").read_bytes() == b"VALUE = 1\n"
    assert store.read(target)["revision"] == 1


def test_write_child_delete_is_denied_by_shared_store(tmp_path, monkeypatch):
    import json
    _configure(monkeypatch)
    scope = MemoryScope.for_workspace(tmp_path)
    store = _store(scope, tmp_path)
    target = store.mutate({"action": "create", "name": "parent note", "content": "retain"}, _ctx(scope, "seed"))["memory_id"]
    fake = FakeClient([
        FakeResponse(FakeMessage(tool_calls=[FakeToolCall("delete_memory", {"memory_id": target, "expected_revision": 1})])),
        FakeResponse(FakeMessage("Read-only investigation complete.")),
    ])
    monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)
    with scoped_workdir(tmp_path), scoped_parent(scope), scoped_memory_scope(scope):
        result = subagent.run_subagent("Inspect the note.", agent_type="general-purpose", allowed_tools=["delete_memory"], target_paths=["config.py"])
        child = result.metadata["memory_proposal_source_session"]
        state = subagent._load_subagent_state("parent", child, tmp_path)
    native = asyncio.run(LegacyJsonSessionStore().load(SessionIdentity(child, "parent"), Path(state["worktree"]["path"])))
    outputs = [json.loads(message["content"]) for message in native.transcript
               if message.get("role") == "tool" and message.get("tool_call_id") == "call_1"]
    assert outputs[-1]["status"] == "denied"
    assert store.read(target)["content"] == "retain"
    assert store.proposals()["items"] == []


def test_native_scope_mismatch_finishes_task_as_error_instead_of_leaving_running_state(tmp_path, monkeypatch):
    _configure(monkeypatch)
    scope = MemoryScope.for_workspace(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    with scoped_workdir(tmp_path), scoped_parent(scope), scoped_memory_scope(scope):
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: FakeClient([FakeResponse(FakeMessage("Read-only investigation complete."))]))
        first = subagent.run_subagent("Inspect files.", agent_type="explore")
        child = first.metadata["memory_proposal_source_session"]
        state = subagent._load_subagent_state("parent", child, tmp_path)
        store = LegacyJsonSessionStore()
        native = asyncio.run(store.load(SessionIdentity(child, "parent"), Path(state["worktree"]["path"])))
        native.metadata["memory_scope"] = MemoryScope.for_workspace(other).to_dict()
        asyncio.run(store.save(native))
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: pytest.fail("错库恢复不得启动模型"))
        result = subagent.run_subagent("Resume inspection.", session_id=child)
        assert "memory scope" in result
        assert subagent._load_subagent_state("parent", child, tmp_path)["status"] == "error"
