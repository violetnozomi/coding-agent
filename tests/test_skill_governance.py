"""Governed Skill metadata, enforcement, and run isolation."""
from __future__ import annotations

from nz_coder.runtime.verification.recovery import RecoveryState
from nz_coder.runtime.core.tool_context import ToolPolicyContext
from nz_coder.runtime.tool_runtime.policy import ProductionToolPolicy
from nz_coder.state.skills import (
    SkillLoader,
    bind_skill_loader,
    current_skill_execution_context,
)


class _Permissions:
    pass


class _RuntimeState:
    investigation_calls_since_edit = 0
    mutation_generation = 0
    strict_progress_blocks = 0


def _policy_context() -> ToolPolicyContext:
    return ToolPolicyContext(
        agent_name="worker", agent_graph=None, tool_allowlist=None,
        admission_handle=None, runtime_state=_RuntimeState(), recovery=RecoveryState(),
        permissions=_Permissions(), stall_orchestrator=None,
        parse_input=lambda value: value if isinstance(value, dict) else {},
        trace=lambda *_args, **_kwargs: None,
    )


def _write_skill(root, name: str, *, allowed="read_file", model="gpt-review"):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: governed\nallowed_tools: {allowed}\n"
        f"model: {model}\n---\nUse the allowed tools only.",
        encoding="utf-8",
    )
    return directory


def test_skill_preserves_model_provenance_and_resource_base(tmp_path) -> None:
    directory = _write_skill(tmp_path / "project", "review")
    loader = SkillLoader(
        project_dir=tmp_path / "project", user_dir=tmp_path / "user",
        bundled_dir=tmp_path / "bundled",
    )

    info = loader.get_skill_info("review")
    result = loader.load("review")

    assert info.model == "gpt-review"
    assert info.source == "project"
    assert info.base_directory == directory.resolve()
    assert result.metadata["model"] == "gpt-review"
    assert result.metadata["source"] == "project"


def test_loaded_skill_allowed_tools_are_enforced_by_tool_policy(tmp_path) -> None:
    _write_skill(tmp_path / "project", "review", allowed="read_file, grep_search")
    loader = SkillLoader(
        project_dir=tmp_path / "project", user_dir=tmp_path / "user",
        bundled_dir=tmp_path / "bundled",
    )
    calls = [
        {"function": {"name": "read_file", "arguments": {}}},
        {"function": {"name": "write_file", "arguments": {"path": "x.py"}}},
    ]

    with bind_skill_loader(loader):
        loader.load("review")
        rejected = ProductionToolPolicy().agent_tool_rejections(_policy_context(), calls)
        execution = current_skill_execution_context()

    assert list(rejected) == [1]
    assert rejected[1].metadata["guardrail"] == "skill_allowed_tools"
    assert execution.active_skills == ("review",)
    assert execution.model_preferences == ("gpt-review",)


def test_skill_enforcement_is_isolated_between_bound_sessions(tmp_path) -> None:
    _write_skill(tmp_path / "project", "read-only", allowed="read_file")
    loader = SkillLoader(
        project_dir=tmp_path / "project", user_dir=tmp_path / "user",
        bundled_dir=tmp_path / "bundled",
    )
    write_call = [{"function": {"name": "write_file", "arguments": {}}}]

    with bind_skill_loader(loader):
        loader.load("read-only")
        first = ProductionToolPolicy().agent_tool_rejections(_policy_context(), write_call)
    with bind_skill_loader(loader):
        second = ProductionToolPolicy().agent_tool_rejections(_policy_context(), write_call)

    assert 0 in first
    assert second == {}


def test_invalid_skill_metadata_is_excluded(tmp_path) -> None:
    _write_skill(tmp_path / "project", "invalid", allowed="read file")
    loader = SkillLoader(
        project_dir=tmp_path / "project", user_dir=tmp_path / "user",
        bundled_dir=tmp_path / "bundled",
    )

    assert loader.get_skill_info("invalid") is None
