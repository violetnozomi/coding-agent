"""长期记忆真实门面、工具和请求缓存行为。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid

from nz_coder.state.memory import MemoryManager, bind_memory_manager


def _context(manager, **overrides):
    return {"scope_id": manager.scope_id, "capability": "manage", "session_id": "parent",
            "actor_id": "parent", "operation_namespace": "test:parent",
            "operation_id": uuid.uuid4().hex, **overrides}


def _create(manager, content="The parser preserves buffered bytes.", **overrides):
    return manager.mutate({"action": "create", "name": "解析调查", "type": "project",
                           "description": "调查解析缓冲数据", "content": content,
                           "applicability": "Only the streaming parser.", **overrides},
                          trusted_context=_context(manager))


def test_manager_reads_other_instance_changes_without_reload(tmp_path):
    a = MemoryManager(tmp_path)
    b = MemoryManager(tmp_path)
    assert not b.has_memories()
    saved = _create(a)
    assert saved["status"] == "saved"
    assert b.has_memories()
    assert b.read(saved["memory_id"])["content"] == "The parser preserves buffered bytes."


def test_memory_failures_never_publish_private_exception_text(tmp_path, monkeypatch):
    from nz_coder.runtime.core.memory_context import MemoryExecutionContext, MemoryRecallState
    from nz_coder.runtime.execution.services import ProductionMemoryService
    from nz_coder.state import memory as memory_module

    manager = MemoryManager(tmp_path)
    secret = "private-database-detail-should-never-be-visible"
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError(secret)

    monkeypatch.setattr(manager.store, "generation", fail)
    monkeypatch.setattr(manager.store, "list_records", fail)
    monkeypatch.setattr(manager.store, "search_snapshot", fail)
    monkeypatch.setattr(manager.store, "mutate", fail)
    context = MemoryExecutionContext(manager=manager, session_id="failure", client=None,
                                     model_id="m", tracer=None, lineage=None, recall=MemoryRecallState())
    with bind_memory_manager(manager):
        values = [manager.load_all(), manager.mutate({"action": "create"}, trusted_context=_context(manager)),
                  memory_module._list_memories(), memory_module._recall_memory(query="parser"),
                  ProductionMemoryService().prompt_block(context, "parser")]
    for value in values:
        assert secret not in str(value)
        assert "unavailable" in str(value)


def test_same_description_does_not_merge_different_conclusions(tmp_path):
    manager = MemoryManager(tmp_path)
    first = _create(manager)
    second = _create(manager, "The batch parser discards buffered bytes.")
    assert first["memory_id"] != second["memory_id"]
    assert len(manager.list_records()["items"]) == 2


def test_generation_cache_changes_after_other_manager_updates(tmp_path):
    from nz_coder.runtime.core.memory_context import MemoryExecutionContext, MemoryRecallState
    from nz_coder.runtime.execution.services import ProductionMemoryService

    a = MemoryManager(tmp_path)
    b = MemoryManager(tmp_path)
    saved = _create(a)
    context = MemoryExecutionContext(manager=b, session_id="cache", client=None, model_id="model",
                                     tracer=None, lineage=None, recall=MemoryRecallState())
    service = ProductionMemoryService()
    before = service.prompt_block(context, "解析")
    assert "调查解析缓冲数据" in before
    result = a.mutate({"action": "update", "memory_id": saved["memory_id"],
                       "expected_revision": 1, "description": "解析结论已经更正"},
                      trusted_context=_context(a))
    assert result["status"] == "saved"
    after = service.prompt_block(context, "解析")
    assert "解析结论已经更正" in after and "调查解析缓冲数据" not in after


def test_empty_cache_does_not_hide_later_memory(tmp_path):
    from nz_coder.runtime.core.memory_context import MemoryExecutionContext, MemoryRecallState
    from nz_coder.runtime.execution.services import ProductionMemoryService

    manager = MemoryManager(tmp_path)
    context = MemoryExecutionContext(manager=manager, session_id="empty", client=None, model_id="m",
                                     tracer=None, lineage=None, recall=MemoryRecallState())
    service = ProductionMemoryService()
    assert service.prompt_block(context, "解析") == ""
    _create(MemoryManager(tmp_path))
    assert "解析调查" in service.prompt_block(context, "解析")


def test_automatic_prompt_has_no_source_io_and_retains_conditions(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

    manager = MemoryManager(tmp_path / "memory")
    _create(manager, source_refs=[{"kind": "file", "locator": "private.py"}])
    def forbidden(*_a, **_k):
        raise AssertionError("automatic memory projection must not read source files")
    monkeypatch.setattr(WorkspaceFileAccess, "read_bytes_with_identity", forbidden)
    block = manager.build_prompt_block(query="解析")
    assert "Only the streaming parser." in block
    assert "not_checked" in block


def test_legacy_files_require_explicit_migration(tmp_path):
    (tmp_path / "old.md").write_text("---\nname: old\ntype: project\ndescription: old fact\n---\nOld body")
    manager = MemoryManager(tmp_path)
    result = manager.mutate({"action": "create", "name": "new", "type": "project",
                             "description": "new fact", "content": "body"},
                            trusted_context=_context(manager))
    assert result["status"] == "unavailable"
    assert "migrat" in result["error"].lower()
    assert not (tmp_path / "memory.sqlite3").exists()


def test_terminal_memory_service_does_not_learn_or_call_models(tmp_path):
    from nz_coder.runtime.core.memory_context import MemoryExecutionContext, MemoryRecallState
    from nz_coder.runtime.execution.services import ProductionMemoryService

    class ForbiddenClient:
        def __getattr__(self, name):
            raise AssertionError(name)
    manager = MemoryManager(tmp_path)
    context = MemoryExecutionContext(manager=manager, session_id="terminal", client=ForbiddenClient(),
                                     model_id="model", tracer=None, lineage=None, recall=MemoryRecallState())
    asyncio.run(ProductionMemoryService().finalize(context, [{"role": "user", "content": "记住这个"}], "completed"))
    assert not manager.has_memories()


def test_model_tool_roundtrip_uses_id_and_version(tmp_path):
    from nz_coder.tools import ToolOutput, TOOL_HANDLERS

    with bind_memory_manager(MemoryManager(tmp_path)):
        saved = TOOL_HANDLERS["save_memory"](action="create", name="stream", description="stream parser",
                                           type="project", content="The parser buffers bytes.")
        result = json.loads(str(saved))
        assert isinstance(saved, ToolOutput)
        assert result["status"] == "saved"
        record = json.loads(str(TOOL_HANDLERS["recall_memory"](kind="record", id=result["memory_id"])))
        assert record["content"] == "The parser buffers bytes."
        deleted = json.loads(str(TOOL_HANDLERS["delete_memory"](memory_id=result["memory_id"], expected_revision=1)))
        assert deleted["status"] == "saved"


def test_scope_is_serializable_and_does_not_accept_paths(tmp_path):
    from nz_coder.state.memory_scope import MemoryScope
    scope = MemoryScope.for_workspace(tmp_path)
    assert MemoryScope.from_dict(scope.to_dict()) == scope
    assert "/" not in scope.scope_id


def test_child_access_cannot_publish_assessment(tmp_path):
    manager = MemoryManager(tmp_path)
    saved = _create(manager)
    command = {"action": "assess", "memory_id": saved["memory_id"], "expected_revision": 1,
               "judgment": {"coverage": "whole", "result": "supported", "assessment": "reviewed"}}
    candidate = manager.mutate(command, trusted_context=_context(manager, capability="propose", session_id="child", parent_session_id="parent"))
    assert candidate["status"] == "pending_review"
    assert not manager.read(saved["memory_id"]).get("assessments")
    denied = manager.mutate({"action": "apply_proposal", "proposal_id": candidate["proposal_id"],
                             "expected_proposal_revision": 1},
                            trusted_context=_context(manager, capability="propose", session_id="child"))
    assert denied["status"] == "denied"


def test_optional_embedding_provider_can_recall_different_words(tmp_path):
    from nz_coder.state.memory_retrieval import reindex_memory
    class Provider:
        identity = "offline-fixture"
        available = True
        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]
    provider = Provider()
    manager = MemoryManager(tmp_path, embedding_provider=provider)
    saved = _create(manager)
    assert reindex_memory(manager.store, provider)["status"] == "ready"
    result = manager.search_result("unrelated wording")
    assert result["semantic_status"] == "ready"
    assert result["items"][0]["id"] == saved["memory_id"]


def test_prompt_snapshot_pairs_records_with_observed_database_generation(tmp_path):
    manager = MemoryManager(tmp_path)
    _create(manager)
    projection = manager.build_prompt_snapshot(query="解析")
    assert projection["generation"] == manager.generation()
    assert "解析调查" in projection["block"]


def test_manager_preserves_trusted_cancel_and_deadline(tmp_path):
    import threading
    import time
    manager = MemoryManager(tmp_path)
    cancel = threading.Event()
    cancel.set()
    command = {"action": "create", "name": "cancelled", "type": "project", "content": "must not persist"}
    result = manager.mutate(command, trusted_context=_context(manager, cancel_event=cancel))
    assert result["status"] == "unavailable"
    assert not manager.has_memories()
    result = manager.mutate(command, trusted_context=_context(manager, deadline=time.monotonic() - 1))
    assert result["status"] == "unavailable"
    assert not manager.has_memories()


def test_partial_migration_file_never_switches_legacy_library_to_empty(tmp_path):
    from nz_coder.state.memory_migration import migrate_legacy
    (tmp_path / "old.md").write_text("---\nname: old\ntype: project\ndescription: old fact\n---\nImportant original knowledge")
    (tmp_path / "memory.sqlite3").touch()
    manager = MemoryManager(tmp_path)
    assert manager.load_all()["status"] == "unavailable"
    assert migrate_legacy(tmp_path, manager.scope_id)["status"] == "migrated"
    assert manager.list_records()["items"][0]["content"] == "Important original knowledge"


def test_environment_restores_persisted_scope_and_capability_before_opening_store(tmp_path):
    from types import SimpleNamespace
    from nz_coder.state.memory_scope import MemoryScope
    from nz_coder.state.sessions import save_session
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.runtime.execution.composition import build_coding_agent
    owner = tmp_path / "owner"
    execution = tmp_path / "execution"
    owner.mkdir()
    execution.mkdir()
    scope = MemoryScope.for_workspace(owner)
    with scoped_workdir(execution):
        save_session([], session_id="restored-memory-scope", activate=False,
                     session_metadata={"memory_scope": scope.to_dict(), "memory_capability": "read"})
        agent = build_coding_agent("Inspect only.", client=SimpleNamespace(), session_id="restored-memory-scope")
        try:
            assert agent.memory_scope == scope
            assert agent.memory_capability == "read"
            assert agent._mm.memory_dir == scope.directory(execution)
        finally:
            agent.close()


def test_source_check_respects_host_read_authorizer(tmp_path):
    import hashlib
    from nz_coder.state.memory import bind_memory_runtime, _source_context
    from nz_coder.state.memory_retrieval import check_sources
    (tmp_path / "restricted.py").write_bytes(b"secret = 1\n")
    refs = [{"kind": "file", "locator": "restricted.py", "binding": "bound",
             "content_hash": hashlib.sha256(b"secret = 1\n").hexdigest()}]
    with bind_memory_runtime(execution_root=tmp_path, source_authorizer=lambda _path: False):
        checked = check_sources(refs, _source_context())
    assert checked[0]["status"] == "unavailable"
    assert "identity" not in checked[0]
