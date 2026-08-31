"""Production contracts for deterministic routing and optional semantic retrieval."""
from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path

import pytest


class _FixtureEmbeddingProvider:
    """Deterministic vectors keep semantic mechanics testable without a model download."""

    identity = "fixture/intent-v1"

    def embed(self, texts):
        return [self._vector(str(text).casefold()) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        if any(token in text for token in (
            "durably retained", "commit_record", "archive.store", "transaction archive",
        )):
            return [1.0, 0.0, 0.0]
        if any(token in text for token in (
            "customer notified", "dispatch_receipt", "messaging.outbound",
        )):
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


def _service(root: Path):
    from nz_coder.intelligence.service import RepoIntelligenceService

    service = RepoIntelligenceService(root)
    service.prewarm(max_files=100)
    assert service.wait_ready(timeout=5).status == "ready"
    return service


def _semantic_fixture(root: Path) -> None:
    (root / "archive").mkdir()
    (root / "messaging").mkdir()
    (root / "archive" / "store.py").write_text(
        "def commit_record(cart):\n    return {'id': cart['id']}\n",
        encoding="utf-8",
    )
    (root / "messaging" / "outbound.py").write_text(
        "def dispatch_receipt(record):\n    return {'accepted': record['id']}\n",
        encoding="utf-8",
    )


def test_semantic_search_reuses_structural_chunks_and_identities(tmp_path) -> None:
    _semantic_fixture(tmp_path)
    service = _service(tmp_path)
    try:
        service.configure_semantic(_FixtureEmbeddingProvider())

        result = service.semantic_search(
            "where is the transaction durably retained?", limit=2,
        )

        assert result["embedding"] is True
        assert result["items"][0]["file"] == "archive/store.py"
        assert "commit_record" in result["items"][0]["symbol_id"]
        assert result["items"][0]["module_id"]
        assert result["items"][0]["source"] == "embedding:fixture/intent-v1"
    finally:
        service.close()


def test_semantic_cache_invalidates_on_index_generation(tmp_path) -> None:
    _semantic_fixture(tmp_path)
    service = _service(tmp_path)
    try:
        service.configure_semantic(_FixtureEmbeddingProvider())
        first = service.semantic_search("durably retained", limit=1)
        cached = service.semantic_search("durably retained", limit=1)
        generation = first["generation"]
        assert cached["cache_hit"] is True

        (tmp_path / "archive" / "store.py").write_text(
            "def commit_record(cart):\n    return {'id': cart['id'], 'durable': True}\n",
            encoding="utf-8",
        )
        service._apply_incremental(("archive/store.py",), 100)
        refreshed = service.semantic_search("durably retained", limit=1)

        assert refreshed["generation"] > generation
        assert refreshed["cache_hit"] is False
        assert service.metrics()["semantic_builds"] == 2
        semantic_metrics = service.metrics()["semantic_index"]
        assert semantic_metrics["last_embedded_chunks"] == 2
        assert semantic_metrics["total_embedded_chunks"] == 4
        assert semantic_metrics["experimental"] is True
    finally:
        service.close()


def test_policy_routes_vocabulary_mismatch_to_optional_semantic(tmp_path) -> None:
    from nz_coder.intelligence.retrieval_policy import RepoRetrievalPolicy

    _semantic_fixture(tmp_path)
    service = _service(tmp_path)
    try:
        service.configure_semantic(_FixtureEmbeddingProvider())
        decision = RepoRetrievalPolicy(hot_path_ms=500).decide(
            "After a customer has paid, find how the transaction is durably retained "
            "and how the customer is notified without using implementation names.",
            service=service, strategy="policy", semantic_available=True,
        )

        assert decision.signal.task_class == "business-intent"
        assert decision.signal.recommended_operation == "semantic_search"
        assert decision.signal.candidate_files[0] == "archive/store.py"
        assert "High-confidence bounded semantic_search candidates" in decision.auto_context
        assert "semantic_search" in decision.guidance
        assert decision.signal.routing_confidence >= 0.8
        assert decision.signal.evidence_confidence > 0
        assert decision.signal.candidate_count == len(decision.signal.candidate_files)
    finally:
        service.close()


def test_policy_routes_short_business_intent_without_identifier_threshold(tmp_path) -> None:
    from nz_coder.intelligence.retrieval_policy import RepoRetrievalPolicy

    _semantic_fixture(tmp_path)
    service = _service(tmp_path)
    try:
        service.configure_semantic(_FixtureEmbeddingProvider())
        decision = RepoRetrievalPolicy(hot_path_ms=500).decide(
            "fix duplicate invoice retries",
            service=service, strategy="guidance", semantic_available=True,
        )
        assert decision.signal.task_class == "business-intent"
        assert decision.signal.recommended_operation == "semantic_search"
        assert decision.signal.routing_confidence > decision.signal.evidence_confidence
    finally:
        service.close()


def test_policy_language_metadata_does_not_materialize_full_snapshot(
    tmp_path, monkeypatch,
) -> None:
    """First-turn routing stays cheap when the full symbol graph is expensive."""
    from nz_coder.intelligence.retrieval_policy import RepoRetrievalPolicy

    (tmp_path / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
    (tmp_path / "main.go").write_text(
        "package main\nfunc main() {}\n",
        encoding="utf-8",
    )
    service = _service(tmp_path)
    monkeypatch.setattr(
        service.index,
        "snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("full snapshot is not a metadata query")
        ),
    )
    monkeypatch.setattr(
        service.index,
        "languages",
        lambda: (_ for _ in ()).throw(
            AssertionError("routing metadata must not wait for the index lock")
        ),
    )
    try:
        decision = RepoRetrievalPolicy(hot_path_ms=500).decide(
            "fix duplicate invoice retries",
            service=service,
            strategy="guidance",
        )

        assert decision.signal.languages == ("go", "python")
    finally:
        service.close()


def test_policy_routes_declared_contract_artifacts_as_known_locations(tmp_path) -> None:
    """Planner-owned artifact paths must outrank a vague natural-language query."""
    from nz_coder.intelligence.retrieval_policy import RepoRetrievalPolicy

    (tmp_path / "cron_engine").mkdir()
    (tmp_path / "cron_engine" / "parser.py").write_text(
        "def parse(value): return value\n",
        encoding="utf-8",
    )
    service = _service(tmp_path)
    try:
        decision = RepoRetrievalPolicy(hot_path_ms=500).decide(
            "完善名称范围支持并补充测试",
            service=service,
            strategy="guidance",
            semantic_available=False,
            known_paths=(
                "cron_engine/parser.py",
                "cron_engine/tests/test_parser.py",
            ),
        )

        assert decision.signal.task_class == "known-location"
        assert decision.signal.recommended_operation == "read"
        assert decision.signal.recommended_tools == ("read_file",)
        assert decision.signal.candidate_files == (
            "cron_engine/parser.py",
            "cron_engine/tests/test_parser.py",
        )
        assert "skip broad repository orientation" in decision.guidance
    finally:
        service.close()


@pytest.mark.parametrize(("query", "expected"), [
    ("fix src/auth/api.py", "known-location"),
    ("update packages/billing/service.ts", "known-location"),
    ("inspect cmd/server/main.go", "known-location"),
    ("rename `format_product`", "known-symbol"),
    ("find callers of `login`", "known-symbol"),
    ("change `TokenCache.refresh`", "known-symbol"),
    ("find the literal `token expired`", "exact-literal"),
    ("find callers of login", "structural"),
    ("explain the call chain for login", "structural"),
    ("show module dependencies for auth", "structural"),
    ("what is the impact of changing login", "structural"),
    ("locate the service entrypoint", "structural"),
    ("fix duplicate invoice retries", "business-intent"),
    ("where is account recovery handled?", "business-intent"),
    ("websocket reconnect loses pending jobs", "business-intent"),
    ("change token refresh behavior", "business-intent"),
    ("why do child changes conflict when applied?", "business-intent"),
    ("fix duplicate payment retries", "business-intent"),
    ("find failed order recovery logic", "business-intent"),
    ("customer notification stops after checkout", "business-intent"),
    ("stale authorization remains cached", "business-intent"),
    ("restore disconnected client work", "business-intent"),
    ("billing reconciliation sometimes duplicates records", "business-intent"),
    ("where is user onboarding handled?", "business-intent"),
    ("find session expiry behavior", "business-intent"),
    ("why are delivery receipts missing?", "business-intent"),
    ("format output", "simple"),
    ("hello", "simple"),
    ("list files", "simple"),
    ("run tests", "simple"),
    ("check style", "simple"),
    ("修复重复支付重试", "business-intent"),
    ("账号恢复在哪里处理？", "business-intent"),
    ("解释登录调用链", "structural"),
    ("修改 src/auth/api.py", "known-location"),
])
def test_retrieval_policy_confusion_matrix(query, expected) -> None:
    from nz_coder.intelligence.retrieval_policy import RepoRetrievalPolicy

    actual, _operation, _tools, _confidence = RepoRetrievalPolicy._route(
        query, changed_paths=(), semantic_available=True,
    )
    assert actual == expected


@pytest.mark.parametrize("query", [
    "review the current changes",
    "what is impacted by this refactor?",
    "fix the regression in the edited code",
])
def test_retrieval_policy_routes_changed_scope_when_changes_exist(query) -> None:
    from nz_coder.intelligence.retrieval_policy import RepoRetrievalPolicy

    actual, operation, _tools, _confidence = RepoRetrievalPolicy._route(
        query, changed_paths=("src/auth/api.py",), semantic_available=True,
    )
    assert actual == "changed-code"
    assert operation == "changed_scope"


def test_policy_does_not_inject_low_confidence_structural_candidates(tmp_path) -> None:
    from nz_coder.intelligence.retrieval_policy import RepoRetrievalPolicy

    (tmp_path / "unrelated.py").write_text(
        "def rotate_logs(): return None\n", encoding="utf-8",
    )
    service = _service(tmp_path)
    try:
        decision = RepoRetrievalPolicy(hot_path_ms=500).decide(
            "Explain the complete call chain for customer payment retention and notification",
            service=service, strategy="policy", semantic_available=False,
        )

        assert decision.signal.recommended_operation == "lookup"
        assert decision.signal.candidate_files == ()
        assert decision.auto_context == ""
        assert "repo_context lookup" in decision.guidance
    finally:
        service.close()


def test_auto_context_respects_hot_path_wait_budget(tmp_path, monkeypatch) -> None:
    from nz_coder.intelligence.retrieval_policy import RepoRetrievalPolicy

    (tmp_path / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
    service = _service(tmp_path)
    pending = Future()
    monkeypatch.setattr(service, "submit_bounded_query", lambda _callback: pending)
    try:
        decision = RepoRetrievalPolicy(hot_path_ms=10).decide(
            "Explain the complete call chain and module dependency for this behavior",
            service=service, strategy="auto-context",
        )

        assert decision.fallback == "hot-path-timeout"
        assert decision.auto_context == ""
        assert decision.elapsed_ms < 250
    finally:
        service.close()


def test_semantic_query_receives_capacity_from_outer_hot_path(tmp_path, monkeypatch) -> None:
    from nz_coder.intelligence.retrieval_policy import RepoRetrievalPolicy

    (tmp_path / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
    service = _service(tmp_path)
    captured = {}

    def semantic_search(_query, **options):
        captured.update(options)
        return {"items": [], "embedding": True}

    monkeypatch.setattr(service, "semantic_search", semantic_search)
    try:
        RepoRetrievalPolicy(hot_path_ms=250).decide(
            "fix duplicate invoice retries", service=service,
            strategy="policy", semantic_available=True,
        )
        assert captured["wait_budget_ms"] == 112.5
    finally:
        service.close()


def test_semantic_auto_context_uses_relative_score_separation(tmp_path, monkeypatch) -> None:
    from nz_coder.intelligence.retrieval_policy import RepoRetrievalPolicy

    (tmp_path / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
    service = _service(tmp_path)

    def separated(_query, **_options):
        return {"items": [
            {"file": "ledger/reservation.py", "score": 0.123},
            {"file": "domains/unrelated.py", "score": 0.102},
            {"file": "ledger/intake.py", "score": 0.099},
        ], "embedding": True}

    monkeypatch.setattr(service, "semantic_search", separated)
    try:
        decision = RepoRetrievalPolicy(hot_path_ms=250).decide(
            "fix duplicate invoice retries", service=service,
            strategy="policy", semantic_available=True,
        )
        assert decision.signal.candidate_files == ("ledger/reservation.py",)
        assert decision.signal.candidate_count == 1
        assert decision.signal.evidence_confidence > 0.72
        assert decision.auto_context
    finally:
        service.close()


def test_semantic_auto_context_rejects_close_runner_up(tmp_path, monkeypatch) -> None:
    from nz_coder.intelligence.retrieval_policy import RepoRetrievalPolicy

    (tmp_path / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
    service = _service(tmp_path)

    def ambiguous(_query, **_options):
        return {"items": [
            {"file": "maybe/one.py", "score": 0.123},
            {"file": "maybe/two.py", "score": 0.115},
        ], "embedding": True}

    monkeypatch.setattr(service, "semantic_search", ambiguous)
    try:
        decision = RepoRetrievalPolicy(hot_path_ms=250).decide(
            "fix duplicate invoice retries", service=service,
            strategy="policy", semantic_available=True,
        )
        assert decision.signal.candidate_count == 0
        assert decision.signal.evidence_confidence == 0.0
        assert decision.auto_context == ""
    finally:
        service.close()


def test_semantic_unavailable_is_an_explicit_structural_fallback(tmp_path) -> None:
    (tmp_path / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
    service = _service(tmp_path)
    try:
        result = service.semantic_search("business intent")

        assert result["freshness"] == "ready"
        assert result["confidence_score"] == 0.0
        assert result["fallback"] is True
        assert "optional" in result["warnings"][-1].casefold()
    finally:
        service.close()


def test_semantic_provider_timeout_returns_fallback_without_blocking(tmp_path) -> None:
    from threading import Event

    (tmp_path / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
    service = _service(tmp_path)
    entered = Event()

    class SlowProvider(_FixtureEmbeddingProvider):
        identity = "fixture/slow"

        def embed(self, texts):
            entered.set()
            Event().wait(2)
            return super().embed(texts)

    try:
        service.configure_semantic(SlowProvider())
        result = service.semantic_search("find the retained transaction", wait_budget_ms=10)

        assert entered.wait(timeout=1)
        assert result["confidence_score"] == 0.0
        assert "wait budget" in result["warnings"][-1]
    finally:
        service.close()


def test_sentence_transformer_provider_records_load_failure(monkeypatch) -> None:
    from nz_coder.intelligence.semantic import SentenceTransformerEmbeddingProvider

    provider = SentenceTransformerEmbeddingProvider("fixture/missing")
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", None)
    assert provider.status == "configured"
    assert provider.available is False
    try:
        provider.embed(["query"])
    except RuntimeError as exc:
        assert "semantic retrieval" in str(exc)
    assert provider.available is False
    assert provider.status == "unavailable"
    assert provider.load_error


def test_semantic_tool_schema_is_hidden_from_run_without_ready_backend():
    from nz_coder.runtime.execution.loop import ProductRunEnvironment
    from nz_coder.tools import get_specs

    host = object.__new__(ProductRunEnvironment)
    host._structured_output_active_repair = ""
    host.tool_allowlist = None
    host.agent_graph = None
    host.repo_intelligence = type("RepoService", (), {"semantic_available": False})()
    names = {item["function"]["name"] for item in host._active_tool_specs()}
    assert "semantic_search" not in names
    assert "semantic_search" in {
        item["function"]["name"] for item in get_specs()
    }


def test_semantic_tool_schema_is_exposed_only_for_ready_backend():
    from nz_coder.runtime.execution.loop import ProductRunEnvironment

    host = object.__new__(ProductRunEnvironment)
    host._structured_output_active_repair = ""
    host.tool_allowlist = None
    host.agent_graph = None
    host.repo_intelligence = type("RepoService", (), {"semantic_available": True})()
    names = {item["function"]["name"] for item in host._active_tool_specs()}
    assert "semantic_search" in names


def test_explicit_semantic_tool_uses_query_budget_not_hot_path_budget(monkeypatch, tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.semantic_search import semantic_search

    captured = {}

    class Service:
        def semantic_search(self, query, **options):
            captured.update(options)
            return {"query": query, "items": [], "embedding": True}

    monkeypatch.setattr(
        "nz_coder.tools.semantic_search.workspace_repo_intelligence",
        lambda *_args, **_kwargs: Service(),
    )
    with scoped_workdir(tmp_path):
        semantic_search("business intent")
    assert captured["wait_budget_ms"] == 2_000
