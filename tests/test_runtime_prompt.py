"""Behavioral contracts carried by the product system prompt."""
from __future__ import annotations


class _NoRequestClient:
    """Provider-shaped test double that rejects accidental model calls."""

    class _Completions:
        def create(self, **_kwargs):
            raise AssertionError("LLM should not be called in prompt contract tests")

    class _Chat:
        def __init__(self) -> None:
            self.completions = _NoRequestClient._Completions()

    def __init__(self) -> None:
        self.chat = self._Chat()


def test_simple_repository_orientation_has_a_strict_exploration_budget(tmp_path):
    from nz_coder.runtime.conversation.prompt import build
    from nz_coder.runtime.process.workdir import scoped_workdir

    with scoped_workdir(tmp_path):
        prompt = build()

    assert "simple directory-orientation request" in prompt
    assert "at most 2 turns and 4 tool calls" in prompt
    assert "Do not scan product state" in prompt


def test_syntax_alias_tests_use_existing_behavior_as_the_oracle(tmp_path):
    from nz_coder.runtime.conversation.prompt import build
    from nz_coder.runtime.process.workdir import scoped_workdir

    with scoped_workdir(tmp_path):
        prompt = build()

    assert "syntax alias or named form" in prompt
    assert "equivalent existing canonical or numeric form" in prompt
    assert "use its observed result as the test oracle" in prompt
    assert "ordering, deduplication, and errors" in prompt
    assert "do not invent new range, step, or scheduler semantics" in prompt


def test_api_edits_require_exact_nearby_call_pattern_evidence(tmp_path):
    """Catches an API edit that ignores a meaningful sibling method choice."""
    from nz_coder.runtime.conversation.prompt import build
    from nz_coder.runtime.process.workdir import scoped_workdir

    with scoped_workdir(tmp_path):
        prompt = build()

    assert "repo or framework API" in prompt
    assert "nearest analogous working call" in prompt
    assert "exact method" in prompt


def test_integrity_conflict_repairs_preserve_existing_data_by_default(tmp_path):
    """Catches resolving uniqueness failures by silently deleting user data."""
    from nz_coder.runtime.conversation.prompt import build
    from nz_coder.runtime.process.workdir import scoped_workdir

    with scoped_workdir(tmp_path):
        prompt = build()

    assert "uniqueness or integrity conflict" in prompt
    assert "deleting existing persisted data" in prompt
    assert "explicitly authorizes" in prompt


def test_agent_builds_first_turn_implementation_bundle_from_planner_contract(
    tmp_path,
    monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.agent.task_contract import TaskContract

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "parser.py").write_text(
        "def parse(value):\n    return int(value)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_parser.py").write_text(
        "def test_parse():\n    assert True\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Parser\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=_NoRequestClient(),
        trace_enabled=False,
    )
    contract = TaskContract.from_dict({
        "objective": "Add named parser values",
        "requirements": [
            {"id": "R1", "description": "Change parser", "kind": "behavior", "expected_artifacts": ["src/parser.py"]},
            {"id": "R2", "description": "Add tests", "kind": "test", "expected_artifacts": ["tests/test_parser.py"]},
            {"id": "R3", "description": "Update docs", "kind": "docs", "expected_artifacts": ["README.md"]},
        ],
    }, workspace=tmp_path)
    agent.runtime_state.set_task_contract(contract)
    agent.runtime_state.initial_plan_complexity = "moderate"
    agent.runtime_state.task_mode = "feature"
    agent.runtime_state.turn_count = 1

    block = agent._implementation_bundle_block("Add named parser values")
    routing = agent._repo_retrieval_block("Add named parser values")

    assert "<implementation-bundle>" in block
    assert "src/parser.py" in block
    assert "project_root=" + str(tmp_path.resolve()) in block
    assert "Declared target paths already resolve the initial workset" in routing
    assert "src/parser.py" in routing
    assert "tests/test_parser.py" in routing

    agent.runtime_state.turn_count = 2
    assert agent._implementation_bundle_block("Add named parser values") == block
    parser = tmp_path / "src" / "parser.py"
    parser.write_text(
        "def parse(value):\n    return int(value) + 1\n",
        encoding="utf-8",
    )
    agent.repo_intelligence._apply_incremental(("src/parser.py",), 100)
    refreshed = agent._implementation_bundle_block("Add named parser values")
    assert "return int(value) + 1" in refreshed
    assert refreshed != block

    agent.close()


def test_task_contract_owns_progress_tool_unless_user_requests_todo(
    tmp_path,
    monkeypatch,
):
    """A runtime ledger must not expose a second model-owned plan state."""
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.agent.task_contract import TaskContract

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=_NoRequestClient(),
        trace_enabled=False,
    )
    contract = TaskContract.from_dict({
        "objective": "Change parser and tests",
        "requirements": [
            {
                "id": "R1",
                "description": "Change parser",
                "kind": "behavior",
                "expected_artifacts": ["src/parser.py"],
            },
            {
                "id": "R2",
                "description": "Add tests",
                "kind": "test",
                "expected_artifacts": ["tests/test_parser.py"],
            },
        ],
    }, workspace=tmp_path)
    agent.runtime_state.set_task_contract(contract)
    agent.runtime_state.initial_task_text = "Change parser and tests."

    names = {
        spec["function"]["name"] for spec in agent._active_tool_specs()
    }
    assert "todo" not in names

    agent.runtime_state.initial_task_text = (
        "Change parser and tests, and maintain a todo checklist."
    )
    explicit_names = {
        spec["function"]["name"] for spec in agent._active_tool_specs()
    }
    assert "todo" in explicit_names

    agent.close()


def test_repo_evidence_refreshes_after_ready_and_generation_change(tmp_path, monkeypatch):
    """Ready structural evidence remains visible after turn one and updates on edits."""
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "pricing.py"
    source.write_text("def calculate_total(value):\n    return value + 5\n", encoding="utf-8")
    (tmp_path / "checkout.py").write_text(
        "from src.pricing import calculate_total\n\n"
        "def checkout(value):\n    return calculate_total(value)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_checkout.py").write_text(
        "from checkout import checkout\n\n"
        "def test_checkout_total():\n    assert checkout(10) == 15\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop("test", permission_mode="auto", client=_NoRequestClient(), trace_enabled=False)
    try:
        agent.repo_retrieval_strategy = "auto-context"
        agent.repo_intelligence_mode = "lookup"
        service = agent.repo_intelligence
        assert service.wait_ready(timeout=5).status == "ready"
        before = source.read_text(encoding="utf-8")
        after = before.replace("return value + 5", "return value + 6")
        agent.change_tracker.record_before("src/pricing.py", True, before)
        source.write_text(after, encoding="utf-8")
        agent.change_tracker.record_after("src/pricing.py", True, after)
        service._apply_incremental(("src/pricing.py",), 100)

        agent.runtime_state.turn_count = 1
        first = agent._repo_retrieval_block("review the current changes")
        assert "src/pricing.py" in first
        assert "related_tests=test_checkout.py" in first
        first_request = agent._build_api_messages([
            {"role": "user", "content": "review the current changes"},
        ])
        assert any(
            "related_tests=test_checkout.py" in str(message.get("content", ""))
            for message in first_request
        )
        query_count = service.metrics()["query_count"]

        agent.runtime_state.turn_count = 2
        second = agent._repo_retrieval_block("review the current changes")
        assert "src/pricing.py" in second
        assert second == first
        second_request = agent._build_api_messages([
            {"role": "user", "content": "review the current changes"},
        ])
        assert any(
            "related_tests=test_checkout.py" in str(message.get("content", ""))
            for message in second_request
        )
        assert service.metrics()["query_count"] == query_count

        updated = after.replace("calculate_total", "calculate_discount")
        agent.change_tracker.record_before("src/pricing.py", True, before)
        source.write_text(updated, encoding="utf-8")
        agent.change_tracker.record_after("src/pricing.py", True, updated)
        service._apply_incremental(("src/pricing.py",), 100)
        third = agent._repo_retrieval_block("review the current changes")
        assert "calculate_discount" in third
        assert third != second
        third_request = agent._build_api_messages([
            {"role": "user", "content": "review the current changes"},
        ])
        assert any(
            "calculate_discount" in str(message.get("content", ""))
            for message in third_request
        )
    finally:
        agent.close()


def test_repo_retrieval_explicit_modes_keep_their_contracts(tmp_path, monkeypatch):
    """Refresh logic must not turn off/tool-only/guidance into auto-context."""
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    source = tmp_path / "app.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop("test", permission_mode="auto", client=_NoRequestClient(), trace_enabled=False)
    try:
        assert agent.repo_intelligence.wait_ready(timeout=5).status == "ready"
        before = source.read_text(encoding="utf-8")
        after = before.replace("return 1", "return 2")
        agent.change_tracker.record_before("app.py", True, before)
        source.write_text(after, encoding="utf-8")
        agent.change_tracker.record_after("app.py", True, after)
        agent.repo_intelligence._apply_incremental(("app.py",), 100)
        agent.runtime_state.turn_count = 2

        agent.repo_retrieval_strategy = "auto-context"
        agent.repo_intelligence_mode = "off"
        assert agent._repo_retrieval_block("review the current changes") == ""

        agent.repo_intelligence_mode = "lookup"
        agent.repo_retrieval_strategy = "tool-only"
        assert agent._repo_retrieval_block("review the current changes") == ""

        agent.repo_retrieval_strategy = "guidance"
        guidance = agent._repo_retrieval_block("review the current changes")
        assert "Retrieval routing:" in guidance
        assert "High-confidence bounded" not in guidance

        agent.repo_retrieval_strategy = "auto-context"
        no_match = agent._repo_retrieval_block("module does_not_exist")
        assert "no-high-confidence-candidates" in no_match
        assert "not evidence that the repository has no match" in no_match
    finally:
        agent.close()


def test_repo_evidence_warming_falls_back_then_ready_is_consumed(tmp_path, monkeypatch):
    """A cold local index never blocks the request; its ready result reaches the next one."""
    from nz_coder.foundation import config
    from nz_coder.intelligence.service import RepoIntelligenceService
    from nz_coder.loop import AgentLoop

    source = tmp_path / "app.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop("test", permission_mode="auto", client=_NoRequestClient(), trace_enabled=False)
    service = RepoIntelligenceService(tmp_path)
    agent.repo_intelligence = service
    try:
        agent.repo_retrieval_strategy = "auto-context"
        agent.repo_intelligence_mode = "lookup"
        before = source.read_text(encoding="utf-8")
        agent.change_tracker.record_before("app.py", True, before)
        source.write_text(before.replace("return 1", "return 2"), encoding="utf-8")
        agent.change_tracker.record_after("app.py", True, source.read_text(encoding="utf-8"))
        agent.runtime_state.turn_count = 1

        warming = agent._repo_retrieval_block("review the current changes")
        assert "High-confidence bounded" not in warming
        assert "ready" not in warming

        assert service.prewarm(max_files=20).result(timeout=5).status == "ready"
        agent.runtime_state.turn_count = 2
        ready = agent._repo_retrieval_block("review the current changes")
        assert "High-confidence bounded changed_scope candidates" in ready
        assert "freshness=indexed" in ready
    finally:
        agent.close()


def test_structural_repo_evidence_reaches_production_prompt_request(tmp_path, monkeypatch):
    """The main prompt path carries module, symbol, caller and read evidence."""
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    (tmp_path / "billing").mkdir()
    (tmp_path / "billing" / "api.py").write_text(
        "def charge(order):\n    return order\n",
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text(
        "from billing.api import charge\n"
        "def checkout(order):\n    return charge(order)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop("test", permission_mode="auto", client=_NoRequestClient(), trace_enabled=False)
    try:
        agent.repo_retrieval_strategy = "auto-context"
        agent.repo_intelligence_mode = "lookup"
        assert agent.repo_intelligence.wait_ready(timeout=5).status == "ready"
        agent.runtime_state.turn_count = 2
        request = agent._build_api_messages([
            {"role": "user", "content": "module billing"},
        ])
        visible = "\n".join(str(message.get("content", "")) for message in request)
        assert "module:billing" in visible
        assert "definition=billing/api.py:1" in visible
        assert "callers=checkout" in visible
        assert "Next read: read_file path=billing/api.py" in visible
    finally:
        agent.close()
