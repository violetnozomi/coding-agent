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
