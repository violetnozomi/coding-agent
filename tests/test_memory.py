"""Tests for persistent memory retrieval and consolidation."""
from __future__ import annotations


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


def test_recall_matches_code_identifier_parts_and_word_variants(tmp_path):
    from nz_coder.memory import MemoryManager

    mgr = MemoryManager(tmp_path)
    mgr.save(
        "http_date_timezone",
        "parse_http_date timezone handling",
        "project",
        "Function parse_http_date handles HTTP date strings and timezone offsets.",
    )
    mgr.save(
        "form_rendering",
        "Django form rendering",
        "project",
        "Form widgets render HTML attributes.",
    )

    results = mgr.recall("HTTP date parsing bug", top_k=1)

    assert [item["name"] for item in results] == ["http_date_timezone"]


def test_recall_does_not_return_only_fresh_unrelated_memory(tmp_path):
    from nz_coder.memory import MemoryManager

    mgr = MemoryManager(tmp_path)
    mgr.save(
        "style_pref",
        "Prefer concise Chinese responses",
        "user",
        "The user prefers concise Chinese explanations.",
    )

    assert mgr.recall("numpy dtype promotion", top_k=5) == []


def test_save_merges_similar_memory_instead_of_duplicating(tmp_path):
    from nz_coder.memory import MemoryManager

    mgr = MemoryManager(tmp_path)
    mgr.save(
        "django_version",
        "Project uses Django 3.2",
        "project",
        "This project uses Django 3.2. Avoid async views.",
    )
    result = mgr.save(
        "django_project_version",
        "Django 3.2 is the project version",
        "project",
        "Django 3.2 is the project version. Prefer sync class-based views.",
    )

    assert "Merged memory" in result
    assert list(mgr.memories) == ["django_version"]
    assert "Prefer sync class-based views" in mgr.memories["django_version"]["content"]
    index = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert index.count("- [") == 1
    assert "django_project_version" not in index


def test_save_does_not_merge_ultra_short_token_overlap(tmp_path):
    from nz_coder.memory import MemoryManager

    mgr = MemoryManager(tmp_path)
    mgr.save("cache_rule", "cache timeout", "project", "Cache timeout defaults to 30s.")
    result = mgr.save("cache_retry", "cache timeout", "project", "Cache timeout retry should stay separate.")

    assert "Saved memory" in result
    assert set(mgr.memories) == {"cache_rule", "cache_retry"}


def test_build_prompt_block_always_includes_user_preferences(tmp_path):
    from nz_coder.memory import MemoryManager

    mgr = MemoryManager(tmp_path)
    mgr.save(
        "response_style",
        "User prefers direct Chinese replies",
        "user",
        "Use Chinese and keep explanations concise unless detail is requested.",
    )
    mgr.save(
        "http_date_timezone",
        "parse_http_date timezone handling",
        "project",
        "Function parse_http_date handles HTTP date strings and timezone offsets.",
    )

    block = mgr.build_prompt_block(query="HTTP date parsing bug", max_items=2)

    assert "response_style" in block
    assert "http_date_timezone" in block
    assert "User preference memories" in block


def test_extract_session_learnings_uses_optional_llm_json(tmp_path):
    from nz_coder.memory import extract_session_learnings

    client = _FakeClient(
        '[{"name":"django_version","description":"Project uses Django 3.2",'
        '"type":"project","content":"Do not use async views in this Django 3.2 project."}]'
    )
    messages = [{"role": "user", "content": "This project uses Django 3.2, do not use async views."}]

    candidates = extract_session_learnings(messages, client=client, model="fake-model")

    assert candidates == [{
        "name": "django_version",
        "description": "Project uses Django 3.2",
        "type": "project",
        "content": "Do not use async views in this Django 3.2 project.",
    }]
    assert client.chat.completions.calls[0]["response_format"] == {"type": "json_object"}


def test_rerank_memories_uses_optional_llm_order():
    from nz_coder.memory import rerank_memories

    candidates = [
        {"name": "first", "type": "project", "description": "less relevant", "content": "alpha"},
        {"name": "second", "type": "project", "description": "more relevant", "content": "beta"},
    ]
    client = _FakeClient('["second", "first"]')

    ranked = rerank_memories("query", candidates, client=client, model="fake-model", top_k=2)

    assert [item["name"] for item in ranked] == ["second", "first"]
