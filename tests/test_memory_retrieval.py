from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import hashlib
import importlib
import threading
import time

import pytest

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess


def _api():
    try:
        return importlib.import_module("nz_coder.state.memory_retrieval")
    except ModuleNotFoundError:
        pytest.fail("定向来源核对模块尚未实现")


def _baseline(path="config.py", content=b"old\n"):
    return {
        "kind": "file", "locator": path, "path": path, "binding": "bound",
        "content_hash": hashlib.sha256(content).hexdigest(),
    }


def test_failed_read_after_file_grows_cannot_reuse_the_same_total_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace
    api = _api()
    refs = [_baseline(f"file{i}.py", b"x") for i in range(3)]
    for i in range(3):
        (tmp_path / f"file{i}.py").write_bytes(b"x" * 90)
    actual = []
    original = WorkspaceFileAccess.read_bytes_with_identity
    monkeypatch.setattr(WorkspaceFileAccess, "stat", lambda self, path: SimpleNamespace(size=1))
    def changed_after_read(self, path, **kwargs):
        data, _ = original(self, path, **kwargs)
        actual.append(len(data))
        raise ValueError("file changed while reading")
    monkeypatch.setattr(WorkspaceFileAccess, "read_bytes_with_identity", changed_after_read)
    budget = api.SourceCheckBudget(max_sources=3, max_total_bytes=100)
    context = api.SourceCheckContext(tmp_path, lambda _path: True, budget=budget)
    api.check_sources(refs, context)
    assert sum(actual) <= 100
    assert budget.bytes_read >= sum(actual)


def _messages(tmp_path, *, complete=False):
    (tmp_path / "config.py").write_text("first\nsecond\n", encoding="utf-8")
    _, identity = WorkspaceFileAccess(tmp_path).read_bytes_with_identity("config.py")
    output = "<content>\n2: second\n</content>"
    return [
        {
            "role": "assistant", "content": "", "_nz_session_id": "session-one",
            "_nz_message_id": "msg-owner", "_nz_interaction_run_id": "run-one",
            "_nz_path": {"root": str(tmp_path), "cwd": str(tmp_path)},
            "tool_calls": [{"id": "call-one", "type": "function", "function": {
                "name": "read_file", "arguments": {"path": "config.py", "offset": 2}}}],
            "_nz_parts": [{"type": "tool", "tool": "read_file", "call_id": "call-one",
                "message_id": "msg-owner", "interaction_run_id": "run-one",
                "authoritative": True, "internal": False, "visible": True,
                "state": {"status": "completed", "input": {"path": "config.py"},
                    "output": output, "metadata": {"truncated": False,
                        "model_read_observation": {"path": "config.py", "offset": 2,
                            "limit": 1, "complete": complete, "identity": asdict(identity)}}}}],
        },
        {"role": "tool", "tool_call_id": "call-one", "content": output,
            "_nz_session_id": "session-one", "_nz_message_id": "msg-result",
            "_nz_evidence_kind": "file_read", "_nz_resource": "config.py"},
    ]


def _ref(**extra):
    return {"kind": "file", "locator": "config.py", "session_id": "session-one",
            "message_id": "msg-owner", "tool_call_id": "call-one", **extra}


def test_resolver_binds_host_observation_and_preserves_partial_read(tmp_path):
    api = _api()
    result = api.resolve_source_refs([_ref(content_hash="0" * 64)], _messages(tmp_path))[0]
    assert result["binding"] == "bound"
    assert result["content_hash"] == "dbea9325179efe46ea2add94f7b6b745ca983fabb208dc6d34aa064623d7ee23"
    assert result["execution_root"] == str(tmp_path)
    assert result["observation_kind"] == "content_read"
    assert (result["offset"], result["limit"], result["complete"]) == (2, 1, False)


@pytest.mark.parametrize("duplicate", ["owner", "result", "call", "part"])
def test_resolver_rejects_ambiguous_tool_identity(tmp_path, duplicate):
    api = _api()
    messages = _messages(tmp_path)
    if duplicate == "owner":
        messages.append(deepcopy(messages[0]))
    elif duplicate == "result":
        messages.append(deepcopy(messages[1]))
    elif duplicate == "call":
        messages[0]["tool_calls"].append(deepcopy(messages[0]["tool_calls"][0]))
    else:
        messages[0]["_nz_parts"].append(deepcopy(messages[0]["_nz_parts"][0]))
    result = api.resolve_source_refs([_ref(content_hash="0" * 64)], messages)[0]
    assert result["binding"] == "unbound"
    assert "content_hash" not in result


@pytest.mark.parametrize("mutation", ["external", "pending", "wrong_path", "hidden", "cut"])
def test_resolver_does_not_accept_unsettled_or_external_metadata(tmp_path, mutation):
    api = _api()
    messages = _messages(tmp_path)
    owner, result = messages
    part = owner["_nz_parts"][0]
    if mutation == "external":
        owner["tool_calls"][0]["function"]["name"] = "mcp__foreign__read_file"
        part["tool"] = "mcp__foreign__read_file"
    elif mutation == "pending":
        part["state"]["status"] = "running"
    elif mutation == "wrong_path":
        part["state"]["input"]["path"] = "other.py"
    elif mutation == "hidden":
        part["visible"] = False
    else:
        result["content"] = "only a truncated tool projection"
    resolved = api.resolve_source_refs([_ref()], messages)[0]
    assert resolved["binding"] == "unbound"
    assert "content_hash" not in resolved


def test_resolver_does_not_promote_model_hash_without_metadata():
    api = _api()
    result = api.resolve_source_refs([_ref(binding="bound", observed_fingerprint="0" * 64)], [
        {"role": "tool", "tool_call_id": "call-one", "content": '{"content_hash":"' + "0" * 64 + '"}'}])[0]
    assert result["binding"] == "unbound"
    assert "observed_fingerprint" not in result


def test_source_checks_compare_content_in_each_execution_root(tmp_path):
    api = _api()
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "config.py").write_bytes(b"old\n")
    (second / "config.py").write_bytes(b"new\n")
    contexts = [api.SourceCheckContext(root, lambda path: True) for root in (first, second)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        checks = list(executor.map(lambda context: api.check_sources([_baseline()], context)[0], contexts))
    assert [item["status"] for item in checks] == ["matching", "changed"]
    assert [item["execution_root"] for item in checks] == [str(first), str(second)]
    assert all(item["observation_kind"] == "identity_check" for item in checks)
    assert all(item["complete"] is True for item in checks)
    assert all("content" not in item and "judgment" not in item for item in checks)


def test_source_checks_do_not_fall_back_to_global_cwd(tmp_path, monkeypatch):
    api = _api()
    (tmp_path / "config.py").write_bytes(b"old\n")
    monkeypatch.chdir(tmp_path)
    check = api.check_sources([_baseline()], None)[0]
    assert check["status"] == "not_checked"
    assert check["reason"] == "no_source_context"
    assert "identity" not in check


def test_source_checks_deduplicate_paths_and_enforce_total_budget(tmp_path):
    api = _api()
    (tmp_path / "config.py").write_bytes(b"old\n")
    (tmp_path / "other.py").write_bytes(b"12345")
    budget = api.SourceCheckBudget(max_sources=2, max_total_bytes=8)
    checks = api.check_sources([_baseline(), _baseline("./config.py"), _baseline("other.py")],
        api.SourceCheckContext(tmp_path, lambda path: True, budget=budget))
    assert [item["status"] for item in checks] == ["matching", "unavailable"]
    assert checks[1]["reason"] == "byte_budget_exceeded"
    assert budget.bytes_read == 4
    assert budget.sources_checked == 2
    assert "identity" not in checks[1]


def test_source_checks_keep_missing_permission_and_unbound_boundaries(tmp_path):
    api = _api()
    (tmp_path / "denied.py").write_bytes(b"secret")
    checks = api.check_sources([
        _baseline("missing.py"), _baseline("denied.py"),
        {"kind": "file", "locator": "unbound.py", "content_hash": "0" * 64},
        {"kind": "document", "locator": "https://example.test/manual"},
        _baseline("../outside.py"),
    ], api.SourceCheckContext(tmp_path, lambda path: path != "denied.py"))
    assert [item["status"] for item in checks] == ["missing", "unavailable", "not_checked", "not_checked", "unavailable"]
    assert checks[1]["reason"] == "permission_denied"
    assert all("identity" not in item for item in checks)


@pytest.mark.parametrize("reason", ["cancelled", "deadline_exceeded"])
def test_source_checks_respect_cancel_and_deadline(tmp_path, reason):
    api = _api()
    (tmp_path / "config.py").write_bytes(b"old\n")
    cancelled = threading.Event()
    if reason == "cancelled":
        cancelled.set()
    context = api.SourceCheckContext(tmp_path, lambda path: True, cancel_event=cancelled,
        deadline=time.monotonic() - 1 if reason == "deadline_exceeded" else None)
    result = api.check_sources([_baseline()], context)[0]
    assert result["status"] == "not_checked"
    assert result["reason"] == reason
    assert context.budget.bytes_read == 0


def test_identity_check_metadata_can_bind_failures_without_content_read(tmp_path):
    api = _api()
    messages = _messages(tmp_path)
    owner, result = messages
    call, part = owner["tool_calls"][0], owner["_nz_parts"][0]
    call["function"] = {"name": "recall_memory", "arguments": {"memory_id": "memory-one", "check_sources": True}}
    part["tool"] = "recall_memory"
    part["state"]["input"] = call["function"]["arguments"]
    part["state"]["metadata"] = {"memory_source_checks": [{
        "observation_key": "check-one", "observation_kind": "identity_check", "path": "config.py",
        "execution_root": str(tmp_path), "observed_at": 1234.0, "status": "missing", "complete": False,
    }]}
    result.pop("_nz_evidence_kind")
    bound = api.resolve_source_refs([_ref(observation_key="check-one")], messages)[0]
    assert bound["binding"] == "bound"
    assert bound["observation_kind"] == "identity_check"
    assert bound["status"] == "missing"
    assert bound["complete"] is False
    assert "content_hash" not in bound
    assert bound["observed_at"] == 1234.0


def test_same_path_different_baselines_share_one_current_observation(tmp_path):
    api = _api()
    (tmp_path / "config.py").write_bytes(b"old\n")
    context = api.SourceCheckContext(tmp_path, lambda path: True,
        budget=api.SourceCheckBudget(max_sources=1))
    checks = api.check_sources([_baseline(), _baseline(content=b"new\n")], context)
    assert [check["status"] for check in checks] == ["matching", "changed"]
    assert context.budget.sources_checked == 1


def test_shared_budget_does_not_share_read_authority_between_contexts(tmp_path):
    api = _api()
    (tmp_path / "config.py").write_bytes(b"old\n")
    budget = api.SourceCheckBudget()
    allowed = api.SourceCheckContext(tmp_path, lambda path: True, budget=budget)
    denied = api.SourceCheckContext(tmp_path, lambda path: False, budget=budget)
    assert api.check_sources([_baseline()], allowed)[0]["status"] == "matching"
    assert api.check_sources([_baseline()], denied)[0]["status"] == "unavailable"


def test_missing_observation_is_not_retried_with_same_call_context(tmp_path):
    api = _api()
    context = api.SourceCheckContext(tmp_path, lambda path: True,
        budget=api.SourceCheckBudget(max_sources=1))
    assert api.check_sources([_baseline()], context)[0]["status"] == "missing"
    assert api.check_sources([_baseline()], context)[0]["status"] == "missing"
    assert context.budget.sources_checked == 1


def test_check_cannot_follow_symlink_outside_execution_root(tmp_path):
    api = _api()
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.py").write_bytes(b"old\n")
    (root / "config.py").symlink_to(tmp_path / "secret.py")
    result = api.check_sources([_baseline()], api.SourceCheckContext(root, lambda path: True))[0]
    assert result["status"] == "unavailable"
    assert "identity" not in result


def test_cancelled_completed_read_keeps_budget_without_exposing_identity(tmp_path, monkeypatch):
    api = _api()
    (tmp_path / "config.py").write_bytes(b"old\n")
    cancel = threading.Event()
    original = WorkspaceFileAccess.read_bytes_with_identity

    def read_then_cancel(self, path, **kwargs):
        observed = original(self, path, **kwargs)
        cancel.set()
        return observed

    monkeypatch.setattr(WorkspaceFileAccess, "read_bytes_with_identity", read_then_cancel)
    context = api.SourceCheckContext(tmp_path, lambda path: True, cancel_event=cancel)
    result = api.check_sources([_baseline()], context)[0]
    assert result["status"] == "not_checked"
    assert result["reason"] == "cancelled"
    assert "identity" not in result
    assert context.budget.bytes_read == 4


def test_source_kind_cannot_disguise_file_observation(tmp_path):
    api = _api()
    result = api.resolve_source_refs([_ref(kind="document")], _messages(tmp_path))[0]
    assert result["binding"] == "unbound"
    assert "identity" not in result


def test_hybrid_search_without_provider_uses_only_lexical_candidates():
    api = _api()
    assert hasattr(api, "hybrid_search"), "缺少全文/向量候选融合入口"

    class Store:
        def generation(self):
            return (1, 0)

        def search_snapshot(self, query, limit):
            return {"items": self.search(query, limit), "generation": (1, 0)}

        def search(self, query, limit):
            assert query == "cache" and limit >= 2
            return [{"id": "memory-one", "revision": 1, "name": "cache"}]

    result = api.hybrid_search(Store(), "cache", None, 2)
    assert [item["id"] for item in result["items"]] == ["memory-one"]
    assert result["semantic_status"] == "not_configured"


class _Provider:
    identity = "fixture/local-2d-v1"

    def __init__(self):
        self.encoded_texts = []

    def embed(self, texts):
        self.encoded_texts.extend(texts)
        return [[1.0, 0.0] if "cache" in text or text == "deployment" else [0.0, 1.0]
                for text in texts]


def _store(tmp_path):
    from nz_coder.state.memory_store import SQLiteMemoryStore
    return SQLiteMemoryStore(tmp_path, scope_id="project-a")


def _context(operation):
    return {"capability": "manage", "scope_id": "project-a", "operation_namespace": "retrieval-test",
            "operation_id": operation}


def _create(store, *, operation="create", **fields):
    result = store.mutate({"action": "create", "name": "offline cache", "type": "project",
                          "description": "local data", "content": "reuse local cache", **fields}, _context(operation))
    assert result["status"] == "saved"
    return result["memory_id"]


def test_explicit_reindex_enables_semantic_candidates_without_lexical_overlap(tmp_path):
    api, store, provider = _api(), _store(tmp_path), _Provider()
    target = _create(store)
    _create(store, operation="unrelated", name="format", description="spacing", content="two spaces")
    assert hasattr(api, "reindex_memory"), "缺少显式向量重建入口"
    before = api.hybrid_search(store, "deployment", provider, 5)
    assert before["items"] == []
    assert before["semantic_status"] == "not_indexed"
    indexed = api.reindex_memory(store, provider)
    assert indexed["status"] == "ready"
    assert indexed["indexed"] == 2
    result = api.hybrid_search(store, "deployment", provider, 5)
    assert [item["id"] for item in result["items"]] == [target]
    assert result["semantic_status"] == "ready"
    assert result["encoder_id"] == provider.identity


def test_reindex_does_not_encode_unchanged_versions_or_mutate_content_generation(tmp_path):
    api, store, provider = _api(), _store(tmp_path), _Provider()
    _create(store)
    assert hasattr(api, "reindex_memory"), "缺少显式向量重建入口"
    generation = store.generation()[0]
    assert api.reindex_memory(store, provider)["indexed"] == 1
    revision = store.generation()[1]
    assert api.reindex_memory(store, provider)["indexed"] == 0
    assert len(provider.encoded_texts) == 1
    assert store.generation() == (generation, revision)


def test_updated_record_never_uses_its_old_vector(tmp_path):
    api, store, provider = _api(), _store(tmp_path), _Provider()
    target = _create(store)
    assert hasattr(api, "reindex_memory"), "缺少显式向量重建入口"
    api.reindex_memory(store, provider)
    result = store.mutate({"action": "update", "memory_id": target, "expected_revision": 1,
                          "name": "format", "description": "spacing", "content": "two spaces"}, _context("update"))
    assert result["status"] == "saved"
    assert api.hybrid_search(store, "deployment", provider, 5)["items"] == []
    assert len(provider.encoded_texts) == 1


@pytest.mark.parametrize("action", ["archive", "delete"])
def test_archived_or_deleted_records_leave_both_searches(tmp_path, action):
    api, store, provider = _api(), _store(tmp_path), _Provider()
    target = _create(store)
    assert hasattr(api, "reindex_memory"), "缺少显式向量重建入口"
    api.reindex_memory(store, provider)
    assert store.mutate({"action": action, "memory_id": target, "expected_revision": 1}, _context(action))["status"] == "saved"
    assert api.hybrid_search(store, "cache", provider, 5)["items"] == []


def test_encoder_switch_and_dimension_change_do_not_mix_old_vector_space(tmp_path):
    api, store, provider = _api(), _store(tmp_path), _Provider()
    _create(store)
    assert hasattr(api, "reindex_memory"), "缺少显式向量重建入口"
    api.reindex_memory(store, provider)
    switched = _Provider()
    switched.identity = "fixture/local-other-model"
    assert api.hybrid_search(store, "deployment", switched, 5)["items"] == []

    class ThreeDimensions(_Provider):
        def embed(self, texts):
            return [[1.0, 0.0, 0.0] for _ in texts]

    mismatch = api.hybrid_search(store, "deployment", ThreeDimensions(), 5)
    assert mismatch["items"] == []
    assert mismatch["semantic_status"] == "dimension_mismatch"


def test_reindex_drops_vector_when_record_changes_during_encoding(tmp_path):
    api, store = _api(), _store(tmp_path)
    target = _create(store)
    assert hasattr(api, "reindex_memory"), "缺少显式向量重建入口"

    class UpdatingProvider(_Provider):
        def embed(self, texts):
            assert store.mutate({"action": "update", "memory_id": target, "expected_revision": 1,
                                 "content": "new content"}, _context("during-encode"))["status"] == "saved"
            return super().embed(texts)

    result = api.reindex_memory(store, UpdatingProvider())
    assert result["status"] == "partial"
    assert result["indexed"] == 0
    assert result["skipped"] == 1
    assert store.embeddings(_Provider.identity) == []
    assert api.reindex_memory(store, _Provider())["indexed"] == 1


def test_query_does_not_return_a_record_deleted_during_embedding(tmp_path):
    api, store, provider = _api(), _store(tmp_path), _Provider()
    target = _create(store)
    assert api.reindex_memory(store, provider)["status"] == "ready"

    class DeletingProvider(_Provider):
        def embed(self, texts):
            result = store.mutate({"action": "delete", "memory_id": target,
                                  "expected_revision": 1}, _context("during-query"))
            assert result["status"] == "saved"
            return super().embed(texts)

    result = api.hybrid_search(store, "cache", DeletingProvider(), 5)
    assert result["items"] == []
    assert result["semantic_status"] == "source_changed"
    assert tuple(result["generation"]) == store.generation()


def test_memory_local_encoder_forbids_implicit_model_download(tmp_path, monkeypatch):
    import sys
    from types import ModuleType
    from nz_coder.intelligence import semantic
    from nz_coder.state.memory import local_embedding_provider

    model_path = tmp_path / "existing-model"
    model_path.mkdir()
    constructed = []

    class LocalEncoder:
        def __init__(self, model, **options):
            constructed.append((model, options))
            assert options == {"local_files_only": True}

        def encode(self, texts, **options):
            return [[1.0, 0.0] for _ in texts]

    module = ModuleType("sentence_transformers")
    module.SentenceTransformer = LocalEncoder
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(semantic.importlib.util, "find_spec", lambda name: object())
    provider = local_embedding_provider(model_path)
    assert provider.available is True
    assert constructed == [(str(model_path.resolve()), {"local_files_only": True})]


def test_unprepared_provider_is_never_loaded_by_search_or_reindex(tmp_path):
    api, store = _api(), _store(tmp_path)
    target = _create(store)
    assert hasattr(api, "reindex_memory"), "缺少显式向量重建入口"

    class UnpreparedProvider(_Provider):
        available = False

        def embed(self, texts):
            pytest.fail("读取不得初始化/下载未就绪的模型")

    result = api.hybrid_search(store, "cache", UnpreparedProvider(), 5)
    assert [item["id"] for item in result["items"]] == [target]
    assert result["semantic_status"] == "unavailable"
    assert api.reindex_memory(store, UnpreparedProvider())["status"] == "unavailable"


def test_semantic_failure_keeps_lexical_results(tmp_path):
    api, store, provider = _api(), _store(tmp_path), _Provider()
    target = _create(store)
    assert hasattr(api, "reindex_memory"), "缺少显式向量重建入口"
    api.reindex_memory(store, provider)

    class FailingProvider(_Provider):
        def embed(self, texts):
            raise RuntimeError("offline encoder failed")

    result = api.hybrid_search(store, "cache", FailingProvider(), 5)
    assert [item["id"] for item in result["items"]] == [target]
    assert result["semantic_status"] == "unavailable"


def test_hybrid_fuses_ranks_instead_of_adding_incompatible_scores():
    api = _api()

    class Store:
        def generation(self):
            return (1, 0)

        def search_snapshot(self, query, limit):
            return {"items": self.search(query, limit), "generation": (1, 0)}

        def search(self, query, limit):
            return [{"id": "lexical", "revision": 1, "score": -99999},
                    {"id": "both", "revision": 1, "score": -1}]

        def embedding_candidates(self, encoder_id, limit=1):
            return []

        def embeddings(self, encoder_id, limit=1000):
            return [{"id": "semantic", "revision": 1, "vector": [1, 0], "dimensions": 2},
                    {"id": "both", "revision": 1, "vector": [0.8, 0.2], "dimensions": 2}]

    result = api.hybrid_search(Store(), "deployment", _Provider(), 3)
    assert [item["id"] for item in result["items"]] == ["both", "lexical", "semantic"]
    assert all("vector" not in item for item in result["items"])


def test_exact_name_remains_a_direct_match_when_fusing_candidates():
    api = _api()

    class Store:
        def generation(self):
            return (1, 0)

        def search_snapshot(self, query, limit):
            return {"items": self.search(query, limit), "generation": (1, 0)}

        def search(self, query, limit):
            return [{"id": "exact", "name": "deployment", "revision": 1},
                    {"id": "both", "name": "background", "revision": 1}]

        def embedding_candidates(self, encoder_id, limit=1):
            return []

        def embeddings(self, encoder_id, limit=1000):
            return [{"id": "both", "name": "background", "revision": 1,
                     "vector": [1, 0], "dimensions": 2}]

    result = api.hybrid_search(Store(), "deployment", _Provider(), 2)
    assert [item["id"] for item in result["items"]] == ["exact", "both"]


@pytest.mark.parametrize("vector", [[], [0, 0], [float("nan"), 1], [float("inf"), 0]])
def test_invalid_provider_vectors_do_not_publish_an_index(tmp_path, vector):
    api, store = _api(), _store(tmp_path)
    _create(store)
    assert hasattr(api, "reindex_memory"), "缺少显式向量重建入口"

    class InvalidProvider(_Provider):
        def embed(self, texts):
            return [vector for _ in texts]

    result = api.reindex_memory(store, InvalidProvider())
    assert result["status"] == "error"
    assert result["indexed"] == 0
    assert store.embeddings(_Provider.identity) == []


def test_partial_reindex_is_visible_and_can_resume(tmp_path):
    api, store, provider = _api(), _store(tmp_path), _Provider()
    _create(store)
    _create(store, operation="second", name="format", content="spacing")
    assert api.reindex_memory(store, provider, limit=1)["indexed"] == 1
    result = api.hybrid_search(store, "cache", provider, 5)
    assert result["semantic_status"] == "partial"
    assert api.reindex_memory(store, provider, limit=1)["indexed"] == 1
    assert api.hybrid_search(store, "cache", provider, 5)["semantic_status"] == "ready"
    assert len(provider.encoded_texts) == 4


def test_hidden_delivered_result_cannot_bind_metadata(tmp_path):
    api = _api()
    messages = _messages(tmp_path)
    messages[1]["_nz_visible"] = False
    assert api.resolve_source_refs([_ref()], messages)[0]["binding"] == "unbound"
