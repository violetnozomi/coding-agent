"""长期记忆门面与可选Gateway重排的行为测试。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import uuid

from nz_coder.state.memory import MemoryManager


def create(manager, name="note", description="buffer parser", content="Buffers bytes"):
    return manager.mutate({"action": "create", "name": name, "description": description,
                           "type": "project", "content": content},
                          trusted_context={"scope_id": manager.scope_id, "capability": "manage",
                              "session_id": "test", "operation_namespace": "test",
                              "operation_id": uuid.uuid4().hex})


def test_concurrent_manager_operations_preserve_all_records(tmp_path):
    manager = MemoryManager(tmp_path)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda i: create(manager, name=f"note-{i}"), range(15)))
    assert all(item["status"] == "saved" for item in results)
    assert len(manager.list_records()["items"]) == 15


def test_workspace_factory_shares_only_same_scope(tmp_path):
    from nz_coder.state.memory import workspace_memory_manager
    first = workspace_memory_manager(tmp_path / "a")
    assert first is workspace_memory_manager(tmp_path / "a")
    assert first is not workspace_memory_manager(tmp_path / "b")


def test_unicode_names_and_multiline_descriptions_are_data_not_frontmatter(tmp_path):
    manager = MemoryManager(tmp_path)
    result = create(manager, name="中文记忆", description="第一行\n第二行")
    assert result["status"] == "saved"
    assert MemoryManager(tmp_path).read(result["memory_id"])["description"] == "第一行\n第二行"


def test_search_handles_chinese_fragments_and_code_identifiers(tmp_path):
    manager = MemoryManager(tmp_path)
    target = create(manager, description="调查解析缓冲数据", content="StreamBuffer.parse_chunk preserves data")
    for query in ("解析", "buffer", "parse_chunk"):
        assert manager.search(query)[0]["id"] == target["memory_id"]


def test_unrelated_memory_is_not_injected_for_freshness(tmp_path):
    manager = MemoryManager(tmp_path)
    create(manager, description="database connection", content="A database note")
    assert manager.build_prompt_block(query="astronomy telescope") == ""


def test_conditions_are_not_removed_to_fit_prompt_budget(tmp_path):
    manager = MemoryManager(tmp_path)
    result = manager.mutate({"action": "create", "name": "complex", "description": "parser works",
                             "type": "project", "content": "works", "applicability": "x" * 3000},
                            trusted_context={"capability": "manage", "scope_id": manager.scope_id,
                                "operation_namespace": "x", "operation_id": "long-condition"})
    text = manager.build_prompt_block(query="parser", max_chars=600)
    assert result["memory_id"] in text
    assert "details omitted" in text
    assert "Description: parser works" not in text


def test_optional_rerank_keeps_distinct_ids_with_same_display_name(monkeypatch):
    from nz_coder.state import memory
    candidates = [{"id": "id-a", "name": "same", "type": "project", "description": "a"},
                  {"id": "id-b", "name": "same", "type": "project", "description": "b"}]
    monkeypatch.setattr(memory, "_create_chat_completion", lambda *a, **kw: '["id-b", "id-a"]')
    ranked = memory.rerank_memories("query", candidates, client=object(), model="fake", top_k=2)
    assert [item["id"] for item in ranked] == ["id-b", "id-a"]

class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content

class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)

class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]

class _FakeCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self.content)

class _FakeChat:
    def __init__(self, content: str) -> None:
        self.completions = _FakeCompletions(content)

class _FakeClient:
    def __init__(self, content: str) -> None:
        self.chat = _FakeChat(content)

def test_rerank_memories_uses_optional_llm_order():
    from nz_coder.state.memory import rerank_memories

    candidates = [
        {"id": "first", "name": "first", "type": "project", "description": "less relevant", "content": "alpha"},
        {"id": "second", "name": "second", "type": "project", "description": "more relevant", "content": "beta"},
    ]
    client = _FakeClient('["second", "first"]')

    ranked = rerank_memories("query", candidates, client=client, model="fake-model", top_k=2)

    assert [item["name"] for item in ranked] == ["second", "first"]

def test_memory_completion_uses_injected_provider_and_observer():
    """Native providers and memory usage must not disappear behind OpenAI bridge."""
    from types import SimpleNamespace

    from nz_coder.state.memory import _create_chat_completion
    from nz_coder.providers.capabilities import ModelCapabilities

    requests = []
    observed = []

    class Provider:
        name = "native-test"

        def capabilities(self, model_id):
            return ModelCapabilities(provider=self.name, model_id=model_id)

        def create_client(self):
            return object()

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"memories": []}')
            )])

    content = _create_chat_completion(
        object(),
        provider=Provider(),
        model="memory-model",
        messages=[{"role": "user", "content": "extract durable facts"}],
        max_tokens=123,
        response_format={"type": "json_object"},
        observer=lambda name, payload: observed.append((name, payload)),
    )

    assert content == '{"memories": []}'
    assert requests[0]["model"] == "memory-model"
    finishes = [payload for name, payload in observed if name == "model_call_finish"]
    assert len(finishes) == 1
    assert finishes[0]["purpose"] == "memory"
