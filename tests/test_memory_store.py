"""长期记忆权威库的事务、身份和维护行为。"""
from __future__ import annotations

import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor

import pytest

from nz_coder.state.memory_store import SQLiteMemoryStore


def ctx(operation, *, capability="manage", namespace="session-a", scope="project-a"):
    return {"capability": capability, "scope_id": scope, "actor_id": "actor-a",
            "session_id": "session-a", "parent_session_id": "parent-a", "task_id": "task-a",
            "operation_namespace": namespace, "operation_id": operation}


def create(store, operation="create", **fields):
    return store.mutate({"action": "create", "name": "中文长期记忆", "type": "project",
                         "description": "部署条件", "content": "禁止联网时使用本地缓存", **fields}, ctx(operation))


def update_in_process(memory_dir, memory_id, operation, content, gate):
    store = SQLiteMemoryStore(memory_dir, scope_id="project-a")
    gate.wait(timeout=15)
    return store.mutate({"action": "update", "memory_id": memory_id,
                         "expected_revision": 1, "content": content}, ctx(operation))


def assess_in_process(memory_dir, memory_id, result, gate):
    store = SQLiteMemoryStore(memory_dir, scope_id="project-a")
    gate.wait(timeout=15)
    return store.mutate({"action": "assess", "memory_id": memory_id, "expected_revision": 1,
                         "judgment": {"coverage": "whole", "result": result, "assessment": result}}, ctx(result))


@pytest.fixture
def store(tmp_path):
    return SQLiteMemoryStore(tmp_path, scope_id="project-a")


def test_create_keeps_similar_names_as_separate_ids_and_searches_chinese(store):
    first = create(store)
    second = create(store, "other", content="允许联网时更新远程缓存")
    assert first["status"] == second["status"] == "saved"
    assert first["memory_id"] != second["memory_id"]
    assert len(store.list_records()["items"]) == 2
    assert {r["id"] for r in store.search("缓存")} == {first["memory_id"], second["memory_id"]}
    assert store.search('" OR * NEAR()') == []
    assert store.generation() == (2, 0)


def test_revision_cas_and_operation_digest_are_persistent(store, tmp_path):
    original = create(store)
    assert create(SQLiteMemoryStore(tmp_path, scope_id="project-a")) == original
    assert create(store, content="同键不同参数")["status"] == "operation_conflict"
    target = original["memory_id"]
    changed = store.mutate({"action": "update", "memory_id": target, "expected_revision": 1,
                            "content": "修订正文"}, ctx("update"))
    assert changed["revision"] == 2
    conflict = store.mutate({"action": "archive", "memory_id": target,
                             "expected_revision": 1}, ctx("stale"))
    assert conflict["status"] == "conflict"
    assert conflict["revision"] == 2
    assert store.read(target, 1)["content"] == "禁止联网时使用本地缓存"
    assert store.read(target)["content"] == "修订正文"
    assert len(store.history(target)) == 2
    namespaced = store.mutate({"action": "create", "name": "第二会话", "content": "正文"},
                              ctx("create", namespace="session-b"))
    assert namespaced["status"] == "saved"


def test_two_processes_cannot_overwrite_same_revision(store, tmp_path):
    target = create(store)["memory_id"]
    mp = multiprocessing.get_context("spawn")
    with mp.Manager() as manager:
        gate = manager.Barrier(2)
        with ProcessPoolExecutor(max_workers=2, mp_context=mp) as pool:
            futures = [pool.submit(update_in_process, str(tmp_path), target, f"p-{i}", str(i), gate)
                       for i in range(2)]
            results = [f.result(timeout=25) for f in futures]
    assert sorted(r["status"] for r in results) == ["conflict", "saved"]
    assert store.read(target)["revision"] == 2
    assert len(store.history(target)) == 2


def test_read_and_wrong_scope_cannot_write(store):
    assert store.mutate({"action": "create", "name": "x", "content": "y"},
                        ctx("read", capability="read"))["status"] == "denied"
    assert store.mutate({"action": "create", "name": "x", "content": "y"},
                        ctx("scope", scope="other"))["status"] == "denied"
    assert store.list_records()["items"] == []


def test_child_writes_stay_pending_and_parent_rechecks_revisions(store):
    target = create(store)["memory_id"]
    command = {"action": "update", "memory_id": target, "expected_revision": 1,
               "content": "子任务候选"}
    pending = store.mutate(command, ctx("child", capability="propose"))
    assert pending["status"] == "pending_review"
    assert store.read(target)["content"] == "禁止联网时使用本地缓存"
    proposal = store.read_proposal(pending["proposal_id"])
    assert proposal["context"]["parent_session_id"] == "parent-a"
    assert store.proposals({"parent_session_id": "parent-a"})["items"][0]["id"] == proposal["id"]
    assert store.mutate({"action": "delete", "memory_id": target, "expected_revision": 1},
                        ctx("deny", capability="propose"))["status"] == "denied"
    create_revision = store.mutate({"action": "update", "memory_id": target,
                                    "expected_revision": 1, "description": "父方已改"}, ctx("parent-change"))
    assert create_revision["revision"] == 2
    apply = {"action": "apply_proposal", "proposal_id": proposal["id"],
             "expected_proposal_revision": 1, "expected_revision": 1}
    assert store.mutate(apply, ctx("apply-stale"))["status"] == "conflict"
    assert store.read_proposal(proposal["id"])["status"] == "pending"
    apply["expected_revision"] = 2
    assert store.mutate(apply, ctx("apply-current"))["status"] == "saved"
    assert store.read(target)["content"] == "子任务候选"
    assert store.read_proposal(proposal["id"])["revision"] == 2


def assessment(target, result="supported", **fields):
    return {"action": "assess", "memory_id": target, "expected_revision": 1,
            "judgment": {"coverage": "whole", "result": result, "assessment": result}, **fields}


def test_whole_and_partial_opposite_assessments_coexist_until_explicit_resolution(store):
    target = create(store)["memory_id"]
    supported = store.mutate(assessment(target), ctx("supported"))
    partial_command = assessment(target, "contradicted")
    partial_command["judgment"].update(coverage="partial", covered_claims="只核查联网限制")
    partial = store.mutate(partial_command, ctx("partial"))
    assert supported["status"] == partial["status"] == "saved"
    assert store.read(target)["revision"] == 1
    events = store.read(target)["assessments"]
    assert len(events) == 2
    assert {e["judgment"]["result"] for e in events} == {"supported", "contradicted"}
    assert all(not e["resolved"] for e in events)
    assert len({e["event_id"] for e in events}) == 2
    assert events[0]["seq"] < events[1]["seq"]
    resolved = store.mutate({"action": "assess", "memory_id": target, "expected_revision": 1,
                              "resolves_event_ids": partial["event_ids"],
                              "resolution_reason": "限制只适用于旧部署"}, ctx("resolve"))
    assert resolved["status"] == "saved"
    events = store.read(target)["assessments"]
    assert next(e for e in events if e["event_id"] == partial["event_ids"][0])["resolved"]
    assert not next(e for e in events if e["event_id"] == supported["event_ids"][0])["resolved"]


def test_related_conflict_is_visible_from_both_ends_and_cas_checked(store):
    left = create(store)["memory_id"]
    right = create(store, "right", name="另一结论")["memory_id"]
    command = {"action": "assess", "memory_id": left, "expected_revision": 1,
               "related_records": [{"memory_id": right, "expected_revision": 1}]}
    saved = store.mutate(command, ctx("relation"))
    assert saved["status"] == "saved"
    assert store.read(right)["related_records"][0]["memory_id"] == left
    store.mutate({"action": "update", "memory_id": right, "expected_revision": 1,
                  "content": "另一结论已修订"}, ctx("right-update"))
    assert store.read(left)["related_records"][0]["historical"] is True
    assert store.mutate(command, ctx("stale-relation"))["status"] == "conflict"
    resolution = {"action": "assess", "memory_id": left, "expected_revision": 1,
                  "resolves_event_ids": saved["event_ids"], "resolution_reason": "条件已经澄清"}
    assert store.mutate(resolution, ctx("missing-endpoint"))["status"] == "error"
    resolution["related_records"] = [{"memory_id": right, "expected_revision": 2}]
    assert store.mutate(resolution, ctx("resolve-relation"))["status"] == "saved"
    assert store.read(right)["related_records"][0]["resolved"] is True


def test_child_assess_is_not_published_until_parent_accepts(store):
    target = create(store)["memory_id"]
    pending = store.mutate(assessment(target), ctx("child-assess", capability="propose"))
    assert store.read(target)["assessments"] == []
    result = store.mutate({"action": "apply_proposal", "proposal_id": pending["proposal_id"],
                            "expected_proposal_revision": 1, "expected_revision": 1}, ctx("parent-assess"))
    assert result["status"] == "saved"
    assert len(store.read(target)["assessments"]) == 1


def test_archive_restore_and_delete_remove_all_stored_body_copies(store):
    secret = "unique-secret-content-43289"
    target = create(store, content=secret)["memory_id"]
    command = assessment(target)
    command["judgment"]["assessment"] = secret
    store.mutate(command, ctx("secret-assess"))
    store.mutate({"action": "update", "memory_id": target, "expected_revision": 1,
                  "content": secret + " child"}, ctx("child-secret", capability="propose"))
    archived = store.mutate({"action": "archive", "memory_id": target, "expected_revision": 1}, ctx("archive"))
    assert archived["revision"] == 2
    assert store.search("43289") == []
    restored = store.mutate({"action": "restore", "memory_id": target, "expected_revision": 2}, ctx("restore"))
    assert restored["revision"] == 3
    assert store.search("43289")[0]["id"] == target
    result = store.mutate({"action": "delete", "memory_id": target, "expected_revision": 3}, ctx("delete"))
    assert result["status"] == "saved"
    assert store.read(target) is None
    assert store.history(target) == []
    assert store.proposals()["items"] == []
    with sqlite3.connect(store.db_path) as conn:
        assert secret not in "\n".join(conn.iterdump())
    replayed = create(store, content=secret)
    assert replayed["status"] == "saved"
    assert replayed["memory_id"] == target
    assert store.read(target) is None


def test_import_is_exclusive_idempotent_and_does_not_invent_assessments(tmp_path):
    store = SQLiteMemoryStore(tmp_path, scope_id="project-a", ready=False)
    records = [{"id": "legacy-id", "name": "旧记忆", "type": "reference", "content": "原正文",
                "legacy_metadata": {"filename": "old.md"}}]
    manifest = {"version": 1, "files": [{"path": "old.md", "sha256": "fixed"}]}
    assert store.import_legacy(records, [], [], manifest)["status"] == "saved"
    assert store.read("legacy-id")["source_refs"] == []
    assert store.read("legacy-id")["assessments"] == []
    assert store.import_legacy(records, [], [], manifest)["status"] == "already_imported"
    assert store.import_legacy([], [], [], {"version": 2})["status"] == "conflict"
    assert store.migration_manifest() == manifest
    assert store.export_snapshot()["records"][0]["id"] == "legacy-id"


def test_failed_import_leaves_no_partial_records_and_not_ready(tmp_path):
    store = SQLiteMemoryStore(tmp_path, scope_id="project-a", ready=False)
    def changed():
        raise ValueError("source changed")
    with pytest.raises(ValueError, match="source changed"):
        store.import_legacy([{"id": "x", "name": "x", "content": "x"}], [], [], {}, verify_sources=changed)
    assert store.migration_manifest() is None
    assert store.mutate({"action": "create", "name": "x", "content": "x"}, ctx("not-ready"))["status"] == "unavailable"


def test_embeddings_are_derived_current_revision_only(store):
    target = create(store)["memory_id"]
    candidate = store.embedding_candidates("encoder-a", 5)[0]
    assert store.save_embedding(target, 1, candidate["content_hash"], "encoder-a", [1.0, 0.0])
    assert store.generation() == (1, 1)
    assert store.embedding_candidates("encoder-a", 5) == []
    assert store.embeddings("encoder-a")[0]["vector"] == [1.0, 0.0]
    store.mutate({"action": "update", "memory_id": target, "expected_revision": 1,
                  "content": "内容更新"}, ctx("new-version"))
    assert store.embeddings("encoder-a") == []
    assert not store.save_embedding(target, 1, candidate["content_hash"], "encoder-a", [1.0, 0.0])


def test_deleting_related_endpoint_removes_pending_assessment_body(store):
    left = create(store)["memory_id"]
    right = create(store, "related", content="related-secret-97231")["memory_id"]
    proposed = assessment(left)
    proposed["judgment"]["assessment"] = "related-secret-97231"
    proposed["related_records"] = [{"memory_id": right, "expected_revision": 1}]
    pending = store.mutate(proposed, ctx("related-proposal", capability="propose"))
    assert pending["status"] == "pending_review"
    assert store.mutate({"action": "delete", "memory_id": right, "expected_revision": 1}, ctx("delete-related"))["status"] == "saved"
    assert store.read_proposal(pending["proposal_id"]) is None
    with sqlite3.connect(store.db_path) as conn:
        assert "related-secret-97231" not in "\n".join(conn.iterdump())


def test_rejecting_proposal_is_revision_checked_and_does_not_publish(store):
    pending = store.mutate({"action": "create", "name": "候选", "content": "候选正文"}, ctx("candidate", capability="propose"))
    command = {"action": "reject_proposal", "proposal_id": pending["proposal_id"], "expected_proposal_revision": 9}
    assert store.mutate(command, ctx("bad-reject"))["status"] == "conflict"
    command["expected_proposal_revision"] = 1
    assert store.mutate(command, ctx("reject"))["status"] == "saved"
    assert store.list_records()["items"] == []
    assert store.read_proposal(pending["proposal_id"])["status"] == "rejected"


def test_import_verifies_manifest_counts_and_body_hash_before_ready(tmp_path):
    store = SQLiteMemoryStore(tmp_path, scope_id="project-a", ready=False)
    record = {"id": "legacy-id", "name": "x", "content": "actual"}
    wrong_count = {"counts": {"records": 2, "proposals": 0, "events": 0}}
    with pytest.raises(ValueError, match="count|数量"):
        store.import_legacy([record], [], [], wrong_count)
    wrong_hash = {"records": [{"id": "legacy-id", "content_sha256": "incorrect"}]}
    with pytest.raises(ValueError, match="hash|摘要"):
        store.import_legacy([record], [], [], wrong_hash)
    assert store.migration_manifest() is None
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 0


def test_vector_reads_respect_zero_and_finite_budget(store):
    for i in range(3):
        target = create(store, str(i))["memory_id"]
        candidate = next(item for item in store.embedding_candidates("encoder", 5) if item["id"] == target)
        store.save_embedding(target, 1, candidate["content_hash"], "encoder", [1, 0])
    assert store.embedding_candidates("encoder-b", 0) == []
    assert store.embeddings("encoder", limit=0) == []
    assert len(store.embeddings("encoder", limit=2)) == 2


def test_same_operation_different_payload_racing_in_processes_is_not_swallowed(store, tmp_path):
    target = create(store)["memory_id"]
    mp = multiprocessing.get_context("spawn")
    with mp.Manager() as manager:
        gate = manager.Barrier(2)
        with ProcessPoolExecutor(max_workers=2, mp_context=mp) as pool:
            futures = [pool.submit(update_in_process, str(tmp_path), target, "same-operation", str(i), gate)
                       for i in range(2)]
            results = [f.result(timeout=25) for f in futures]
    assert sorted(result["status"] for result in results) == ["operation_conflict", "saved"]
    assert store.read(target)["revision"] == 2


def test_missing_scope_or_operation_identity_cannot_mutate(store):
    command = {"action": "create", "name": "x", "content": "y"}
    trusted = ctx("x")
    del trusted["operation_namespace"]
    assert store.mutate(command, trusted)["status"] == "error"
    trusted = ctx("x")
    del trusted["scope_id"]
    assert store.mutate(command, trusted)["status"] == "denied"
    assert store.generation() == (0, 0)


def test_migration_manifest_detects_corruption_without_reading_old_sources(tmp_path):
    store = SQLiteMemoryStore(tmp_path, scope_id="project-a", ready=False)
    manifest = {"counts": {"records": 0, "proposals": 0, "events": 0}}
    store.import_legacy([], [], [], manifest)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE memory_imports SET digest='broken'")
    with pytest.raises(RuntimeError, match="清单|manifest"):
        store.migration_manifest()


def test_delete_keeps_only_safe_related_identity_boundary(store):
    left = create(store)["memory_id"]
    right = create(store, "right-delete", content="right-secret-58217")["memory_id"]
    command = assessment(left, "contradicted", related_records=[{"memory_id": right, "expected_revision": 1}])
    command["judgment"]["assessment"] = "right-secret-58217"
    store.mutate(command, ctx("published-relation"))
    store.mutate({"action": "delete", "memory_id": right, "expected_revision": 1}, ctx("delete-endpoint"))
    relation = store.read(left)["related_records"][0]
    assert relation["memory_id"] == right
    assert relation["availability"] == "deleted"
    assert relation["historical"] is True
    with sqlite3.connect(store.db_path) as conn:
        assert "right-secret-58217" not in "\n".join(conn.iterdump())


def test_delete_removes_accepted_create_proposal_copy(store):
    command = {"action": "create", "name": "候选", "content": "accepted-secret-58329"}
    pending = store.mutate(command, ctx("child-create", capability="propose"))
    accepted = store.mutate({"action": "apply_proposal", "proposal_id": pending["proposal_id"],
                             "expected_proposal_revision": 1}, ctx("parent-create"))
    assert accepted["status"] == "saved"
    store.mutate({"action": "delete", "memory_id": accepted["memory_id"], "expected_revision": 1}, ctx("delete-accepted"))
    assert store.read_proposal(pending["proposal_id"]) is None
    with sqlite3.connect(store.db_path) as conn:
        assert "accepted-secret-58329" not in "\n".join(conn.iterdump())


def test_search_snapshot_pairs_current_candidates_with_generation(store):
    target = create(store)["memory_id"]
    found = store.search_snapshot("缓存", 5)
    assert found["generation"] == (1, 0)
    assert found["items"][0]["id"] == target
    assert store.search_snapshot(None, 0) == {"items": [], "generation": (1, 0)}
    assert store.search_snapshot(None, 5)["items"][0]["id"] == target


def test_database_symlink_cannot_open_another_scope(tmp_path):
    original = SQLiteMemoryStore(tmp_path / "first", scope_id="project-a")
    create(original)
    other = tmp_path / "second"
    other.mkdir()
    (other / "memory.sqlite3").symlink_to(original.db_path)
    with pytest.raises((OSError, RuntimeError), match="符号链接|symlink"):
        SQLiteMemoryStore(other, scope_id="project-b")
    assert original.generation() == (1, 0)


def test_existing_database_scope_cannot_be_switched_by_another_store(store, tmp_path):
    target = create(store)["memory_id"]
    with pytest.raises((ValueError, RuntimeError), match="scope"):
        SQLiteMemoryStore(tmp_path, scope_id="project-b")
    assert store.read(target)["revision"] == 1


def test_opposing_assessments_from_two_processes_both_remain_visible(store, tmp_path):
    target = create(store)["memory_id"]
    mp = multiprocessing.get_context("spawn")
    with mp.Manager() as manager:
        gate = manager.Barrier(2)
        with ProcessPoolExecutor(max_workers=2, mp_context=mp) as pool:
            futures = [pool.submit(assess_in_process, str(tmp_path), target, result, gate)
                       for result in ["supported", "contradicted"]]
            results = [f.result(timeout=25) for f in futures]
    assert [result["status"] for result in results] == ["saved", "saved"]
    claims = store.read(target)["assessments"]
    assert {item["judgment"]["result"] for item in claims} == {"supported", "contradicted"}
    assert all(not item["resolved"] for item in claims)
    assert store.read(target)["revision"] == 1


def test_pending_proposal_exposes_original_operation_and_expected_revision(store):
    target = create(store)["memory_id"]
    result = store.mutate({"action": "update", "memory_id": target, "expected_revision": 1,
                           "content": "child"}, ctx("child-op", capability="propose", namespace="child-session"))
    proposal = store.read_proposal(result["proposal_id"])
    assert proposal["expected_revision"] == 1
    assert proposal["operation_namespace"] == "child-session"
    assert proposal["operation_id"] == "child-op"


def test_proposal_source_session_is_projected_from_trusted_context(store):
    result = store.mutate({"action": "create", "name": "候选", "content": "候选正文",
                           "source_session": "model-forged"}, ctx("child-owned", capability="propose"))
    proposal = store.read_proposal(result["proposal_id"])
    assert proposal["source_session"] == "session-a"
    assert store.proposals({"source_session": "session-a"})["items"][0]["id"] == proposal["id"]
    assert store.proposals({"source_session": "model-forged"})["items"] == []


def test_assessment_keeps_recording_time_separate_from_known_observation_time(store):
    target = create(store)["memory_id"]
    store.mutate(assessment(target), ctx("no-source-time"))
    event = store.read(target)["assessments"][0]
    assert event["recorded_at"]
    assert event["observed_at"] is None
    command = assessment(target, evidence_refs=[{"kind": "file", "locator": "config.py",
                                                  "binding": "bound", "observed_at": 12.5}])
    store.mutate(command, ctx("known-source-time"))
    assert store.read(target)["assessments"][-1]["observed_at"] == 12.5


@pytest.mark.parametrize("error_type,status", [(sqlite3.Error, "unavailable"), (TypeError, "error"),
                                             (ValueError, "error"), (KeyError, "error")])
def test_mutation_faults_do_not_expose_private_exception_text(store, monkeypatch, error_type, status):
    secret = "simulated-private-token-38216"

    def fail_event(*args, **kwargs):
        raise error_type(secret)

    monkeypatch.setattr(store, "_emit", fail_event)
    result = create(store, "fault-injection")
    assert result["status"] == status
    assert result["reason"] == "An internal error occurred."
    assert secret not in str(result)
    assert store.list_records()["items"] == []
    assert store.generation() == (0, 0)


def test_authored_input_validation_remains_public(store):
    result = create(store, "invalid-content", content="")
    assert result == {"status": "error", "reason": "name 和 content 不能为空"}
