"""Governed Skill metadata, enforcement, and run isolation."""
from __future__ import annotations

from dataclasses import replace

import pytest

from nz_coder.foundation.project_control import capture_project_control_snapshot
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


def _trusted_loader(tmp_path) -> SkillLoader:
    snapshot = replace(capture_project_control_snapshot(tmp_path), trusted=True)
    return SkillLoader(
        project_dir=tmp_path / ".nz-coder" / "skills",
        user_dir=tmp_path / "user",
        bundled_dir=tmp_path / "bundled",
        project_control_snapshot=snapshot,
    )


def test_skill_preserves_model_provenance_and_resource_base(tmp_path) -> None:
    directory = _write_skill(tmp_path / ".nz-coder" / "skills", "review")
    loader = _trusted_loader(tmp_path)

    info = loader.get_skill_info("review")
    result = loader.load("review")

    assert info.model == "gpt-review"
    assert info.source == "project"
    assert info.base_directory == directory.resolve()
    assert result.metadata["model"] == "gpt-review"
    assert result.metadata["source"] == "project"


def test_loaded_skill_allowed_tools_are_enforced_by_tool_policy(tmp_path) -> None:
    _write_skill(
        tmp_path / ".nz-coder" / "skills",
        "review",
        allowed="read_file, grep_search",
    )
    loader = _trusted_loader(tmp_path)
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
    _write_skill(tmp_path / ".nz-coder" / "skills", "read-only", allowed="read_file")
    loader = _trusted_loader(tmp_path)
    write_call = [{"function": {"name": "write_file", "arguments": {}}}]

    with bind_skill_loader(loader):
        loader.load("read-only")
        first = ProductionToolPolicy().agent_tool_rejections(_policy_context(), write_call)
    with bind_skill_loader(loader):
        second = ProductionToolPolicy().agent_tool_rejections(_policy_context(), write_call)

    assert 0 in first
    assert second == {}


def test_invalid_skill_metadata_is_excluded(tmp_path) -> None:
    project = tmp_path / ".nz-coder" / "skills"
    _write_skill(project, "invalid", allowed="read file")
    _write_skill(project, "valid")
    loader = _trusted_loader(tmp_path)

    assert loader.get_skill_info("invalid") is None
    assert loader.get_skill_info("valid") is not None
    assert loader.diagnostics()["parse_errors"] == [
        {"source": "project", "directory": "invalid", "reason": "invalid_metadata"}
    ]


@pytest.mark.parametrize("frontmatter", [False, True])
@pytest.mark.parametrize("body", ["Snapshot instructions", ""])
@pytest.mark.parametrize("change", ["rewrite", "delete"])
def test_project_skill_body_is_pinned_before_first_load(
    tmp_path, frontmatter, body, change,
) -> None:
    path = tmp_path / ".nz-coder" / "skills" / "snapshot" / "SKILL.md"
    path.parent.mkdir(parents=True)
    header = "---\nname: snapshot\n---\n" if frontmatter else ""
    path.write_text(header + body, encoding="utf-8")
    loader = _trusted_loader(tmp_path)

    if change == "rewrite":
        path.write_text("Changed after snapshot", encoding="utf-8")
    else:
        path.unlink()

    skill = loader.get_skill_info("snapshot")
    assert skill is not None
    if not frontmatter and body == "":
        assert skill._body == ""
    assert skill.get_body() == body
    loaded = loader.load("snapshot")
    assert "<skill_content" in loaded
    assert "Changed after snapshot" not in loaded


def test_untrusted_legacy_project_skill_is_not_loaded(tmp_path) -> None:
    path = tmp_path / ".nz-coder" / "skills" / "legacy" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("Untrusted instructions", encoding="utf-8")
    snapshot = replace(capture_project_control_snapshot(tmp_path), trusted=False)
    loader = SkillLoader(
        project_dir=path.parent.parent, user_dir=tmp_path / "user",
        bundled_dir=tmp_path / "bundled", project_control_snapshot=snapshot,
    )
    assert loader.get_skill_info("legacy") is None
    assert loader.load("legacy").startswith("Error:")


@pytest.mark.parametrize("source", ["user", "bundled"])
def test_legacy_nonproject_skill_retains_lazy_body_loading(tmp_path, source) -> None:
    path = tmp_path / source / "legacy" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("Original instructions", encoding="utf-8")
    loader = _trusted_loader(tmp_path)
    path.write_text("Current instructions", encoding="utf-8")
    skill = loader.get_skill_info("legacy")
    assert skill.source == source
    assert skill._body is None
    assert skill.get_body() == "Current instructions"


def test_untrusted_project_skill_cannot_override_bundled_governance(tmp_path) -> None:
    _write_skill(
        tmp_path / "project",
        "review",
        allowed="bash, write_file",
        model="repo-expensive-model",
    )
    _write_skill(
        tmp_path / "bundled",
        "review",
        allowed="read_file",
        model="bundled-safe-model",
    )

    loader = SkillLoader(
        project_dir=tmp_path / "project",
        user_dir=tmp_path / "user",
        bundled_dir=tmp_path / "bundled",
        workspace_trusted=False,
    )
    info = loader.get_skill_info("review")

    assert info is not None
    assert info.source == "bundled"
    assert info.allowed_tools == ["read_file"]
    assert info.model == "bundled-safe-model"


def test_untrusted_project_skill_settings_remain_ignored_after_reload(tmp_path) -> None:
    _write_skill(tmp_path / "bundled", "review", allowed="read_file", model="safe")
    settings = tmp_path / "project" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"disabled_skills":["review"]}', encoding="utf-8")
    loader = SkillLoader(
        project_dir=tmp_path / "project" / "skills",
        user_dir=tmp_path / "user",
        bundled_dir=tmp_path / "bundled",
        workspace_trusted=False,
    )

    loader.reload(loader._project_control_snapshot)

    assert loader.get_skill_info("review") is not None


def test_skill_diagnostics_reports_sources_and_empty_roots(tmp_path) -> None:
    loader = SkillLoader(
        project_dir=tmp_path / ".nz-coder" / "skills",
        user_dir=tmp_path / "user",
        bundled_dir=tmp_path / "bundled",
    )

    assert loader.diagnostics() == {
        "schema": "skill-loader-diagnostics.v1",
        "sources": [
            {"source": "project"}, {"source": "user"}, {"source": "bundled"},
        ],
        "available": [], "conditional": [], "disabled": [],
        "shadowed": [], "parse_errors": [],
    }


def test_skill_diagnostics_preserves_precedence_and_disabled_selection(tmp_path) -> None:
    project = tmp_path / ".nz-coder" / "skills"
    user = tmp_path / "user"
    bundled = tmp_path / "bundled"
    _write_skill(project, "review", allowed="read_file")
    _write_skill(user, "review", allowed="grep_search")
    _write_skill(bundled, "review", allowed="write_file")
    _write_skill(user, "conditional", allowed="read_file")
    (user / "conditional" / "SKILL.md").write_text(
        "---\nname: conditional\npaths: src/**\n---\nbody", encoding="utf-8"
    )
    settings = tmp_path / ".nz-coder" / "settings.json"
    settings.write_text('{"disabled_skills": ["review"]}', encoding="utf-8")
    loader = _trusted_loader(tmp_path)

    report = loader.diagnostics()
    assert report["available"] == []
    assert report["conditional"] == [{"name": "conditional", "source": "user"}]
    assert report["disabled"] == [{"name": "review", "source": "project"}]
    assert report["shadowed"] == [
        {"name": "review", "selected_source": "project", "shadowed_source": "user"},
        {"name": "review", "selected_source": "project", "shadowed_source": "bundled"},
    ]


def test_skill_diagnostics_records_malformed_metadata_without_aborting(tmp_path) -> None:
    project = tmp_path / ".nz-coder" / "skills"
    _write_skill(project, "valid")
    malformed = project / "malformed"
    malformed.mkdir(parents=True)
    (malformed / "SKILL.md").write_text(
        "---\nname: malformed\nallowed_tools: read file\n---\nbody", encoding="utf-8"
    )
    missing = project / "unreadable"
    missing.mkdir()
    (missing / "SKILL.md").write_bytes(b"\xff")
    loader = _trusted_loader(tmp_path)

    report = loader.diagnostics()
    assert report["available"] == [{"name": "valid", "source": "project"}]
    assert report["parse_errors"] == [
        {"source": "project", "directory": "malformed", "reason": "invalid_metadata"},
        {"source": "project", "directory": "unreadable", "reason": "unreadable"},
    ]


def test_skill_diagnostics_is_stable_and_does_not_load_bodies(tmp_path, monkeypatch) -> None:
    project = tmp_path / ".nz-coder" / "skills"
    _write_skill(project, "review")
    loader = _trusted_loader(tmp_path)
    monkeypatch.setattr(
        "nz_coder.state.skills.Skill.get_body",
        lambda _self: (_ for _ in ()).throw(AssertionError("body")),
    )

    first = loader.diagnostics()
    loader.reload(loader._project_control_snapshot)
    second = loader.diagnostics()
    assert first == second


def test_skill_without_frontmatter_remains_legacy_compatible(tmp_path) -> None:
    project = tmp_path / ".nz-coder" / "skills"
    directory = project / "legacy"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("Legacy instructions", encoding="utf-8")
    loader = _trusted_loader(tmp_path)

    assert loader.get_skill_info("legacy") is not None
    assert loader.load("legacy")
    assert loader.get_skill_info("legacy").get_body() == "Legacy instructions"
    assert loader.list_skills() == [
        {
            "name": "legacy",
            "description": "",
            "source": "project",
            "allowed_tools": [],
            "paths": [],
            "model": "",
            "status": "available",
        }
    ]
    assert loader.diagnostics()["parse_errors"] == []


def test_skill_frontmatter_line_without_separator_is_invalid_metadata(tmp_path) -> None:
    project = tmp_path / ".nz-coder" / "skills"
    directory = project / "malformed"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: malformed\nthis line has no separator\n---\nbody",
        encoding="utf-8",
    )
    loader = _trusted_loader(tmp_path)

    assert loader.get_skill_info("malformed") is None
    assert loader.diagnostics()["parse_errors"] == [
        {"source": "project", "directory": "malformed", "reason": "invalid_metadata"}
    ]


def test_skill_missing_frontmatter_closing_delimiter_is_invalid(tmp_path) -> None:
    project = tmp_path / ".nz-coder" / "skills"
    directory = project / "missing-close"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: missing-close\nbody", encoding="utf-8"
    )
    loader = _trusted_loader(tmp_path)

    assert loader.get_skill_info("missing-close") is None
    assert loader.diagnostics()["parse_errors"] == [
        {"source": "project", "directory": "missing-close", "reason": "invalid_metadata"}
    ]


def test_skill_empty_frontmatter_is_invalid(tmp_path) -> None:
    project = tmp_path / ".nz-coder" / "skills"
    directory = project / "empty"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\n---\nbody", encoding="utf-8")
    loader = _trusted_loader(tmp_path)

    assert loader.get_skill_info("empty") is None
    assert loader.diagnostics()["parse_errors"] == [
        {"source": "project", "directory": "empty", "reason": "invalid_metadata"}
    ]
