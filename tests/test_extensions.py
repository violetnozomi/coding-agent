"""Tests for the unified extension metadata contract and CLI."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nz_coder.extensions.cli import extensions_main
from nz_coder.extensions.registry import ExtensionDescriptor, ExtensionRegistry
from nz_coder.mcp.config import MCPServerConfig
from nz_coder.mcp.runtime import MCPRuntime, MCPServerStatus
from nz_coder.state.skills import SkillLoader


def _write_skill(root: Path, name: str, frontmatter: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\n{frontmatter}\n---\nInstructions.",
        encoding="utf-8",
    )


def _registry(tmp_path, *, skill_loader=None, hook_loader=None, mcp_loader=None):
    return ExtensionRegistry(
        workspace=tmp_path,
        skill_loader=skill_loader or SkillLoader(
            bundled_dir=tmp_path / "missing-bundled",
            user_dir=tmp_path / "missing-user",
            project_dir=tmp_path / "missing-project",
        ),
        hook_loader=hook_loader or (lambda _path: []),
        mcp_config_loader=mcp_loader or (lambda **_kwargs: []),
    )


def test_descriptor_validates_identity_scope_lifecycle_and_effect():
    with pytest.raises(ValueError, match="KIND:NAME"):
        ExtensionDescriptor("invalid", "skill", "x", "bundled", "global", "loaded", "static", True)
    with pytest.raises(ValueError, match="Unknown extension effect"):
        ExtensionDescriptor(
            "skill:x",
            "skill",
            "x",
            "bundled",
            "global",
            "loaded",
            "static",
            True,
            effects=(("tool", "network"),),
        )


def test_skill_projection_preserves_precedence_condition_and_declared_tools(tmp_path):
    bundled = tmp_path / "bundled"
    project = tmp_path / "project"
    user = tmp_path / "user"
    _write_skill(bundled, "review", "description: bundled\nallowed_tools: read_file")
    _write_skill(
        project,
        "review",
        "description: project\nallowed_tools: read_file, grep_search\npaths: src/**",
    )
    loader = SkillLoader(bundled_dir=bundled, user_dir=user, project_dir=project)

    item = _registry(tmp_path, skill_loader=loader).get("skill:review")
    assert item is not None
    assert (item.source, item.scope, item.status, item.lifecycle) == (
        "project",
        "workspace",
        "conditional",
        "reloadable",
    )
    assert item.trusted is False
    assert item.permissions == ("grep_search", "read_file")


def test_hook_projection_includes_core_and_schema_limited_project_hook(tmp_path):
    hook = SimpleNamespace(
        id="guard",
        event="pre_tool_use",
        action=SimpleNamespace(type="prompt"),
        reject=True,
        continue_run=False,
        on_error="reject",
    )
    registry = _registry(tmp_path, hook_loader=lambda _path: [hook])

    core = registry.get("hook:core")
    project = registry.get("hook:guard")
    assert core is not None and core.trusted is True and core.lifecycle == "static"
    assert project is not None
    assert project.trusted is False
    assert project.scope == "workspace"
    assert "decision:reject" in project.capabilities
    assert "schema_limited_prompt" in project.permissions


def test_optional_pack_projection_is_lazy_and_lists_expected_tools(tmp_path):
    items = _registry(tmp_path).snapshot()
    packs = {item.name: item for item in items if item.kind == "tool_pack"}

    assert {"python_ast", "lsp"} <= set(packs)
    assert packs["python_ast"].lifecycle == "lazy"
    assert "python_symbol_check" in packs["python_ast"].capabilities
    assert packs["python_ast"].effects == (
        ("python_symbol_check", "read"),
        ("python_structural_edit", "write"),
    )
    assert packs["lsp"].effects == (("lsp", "read"),)


def test_mcp_config_projection_is_secret_free_and_preserves_trust(tmp_path):
    server = MCPServerConfig(
        name="project-tools",
        command=("python", "server.py", "super-secret"),
        cwd=tmp_path,
        environment=(("TOKEN", "super-secret"),),
        source="project",
        trusted=False,
    )
    registry = _registry(tmp_path, mcp_loader=lambda **_kwargs: [server])

    item = registry.get("mcp_server:project-tools")
    assert item is not None
    assert (item.scope, item.status, item.trusted, item.lifecycle) == (
        "workspace",
        "untrusted",
        False,
        "live",
    )
    assert "super-secret" not in json.dumps(item.to_dict())


def test_live_mcp_projection_exposes_names_effects_and_counts_only(tmp_path):
    server = MCPServerConfig(
        name="live",
        command=("python", "server.py"),
        cwd=tmp_path,
        tool_effects=(("search", "read"),),
    )
    runtime = MCPRuntime([server], workspace=tmp_path)
    runtime.statuses["live"] = MCPServerStatus("live", "connected", tool_count=1)
    runtime._definitions["live"] = [{"name": "search", "description": "find"}]
    runtime._prompts["live"] = [{"name": "review"}]
    runtime._resources["live"] = [{"uri": "file:///docs"}]
    registry = _registry(tmp_path)
    registry.mcp_runtime = runtime

    item = registry.get("mcp_server:live")
    assert item is not None
    assert item.status == "connected"
    assert item.effects == (("search", "read"),)
    assert item.capabilities == ("search", "prompts:1", "resources:1")


def test_broken_source_becomes_error_without_hiding_other_extensions(tmp_path):
    def broken_hooks(_path):
        raise ValueError("bad hook config")

    items = _registry(tmp_path, hook_loader=broken_hooks).snapshot()
    assert any(item.extension_id == "error:hooks" for item in items)
    assert any(item.kind == "tool_pack" for item in items)


def test_invalid_real_hook_settings_are_visible_as_failed_source(tmp_path):
    settings = tmp_path / ".nz-coder" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"hooks": "not-a-list"}', encoding="utf-8")

    items = ExtensionRegistry(
        workspace=tmp_path,
        skill_loader=SkillLoader(
            bundled_dir=tmp_path / "missing-bundled",
            user_dir=tmp_path / "missing-user",
            project_dir=tmp_path / "missing-project",
        ),
        mcp_config_loader=lambda **_kwargs: [],
    ).snapshot()
    error = next(item for item in items if item.extension_id == "error:hooks")
    assert error.status == "failed"
    assert "hooks' must be a list" in error.error


def test_extensions_cli_json_filter_and_status(tmp_path, capsys):
    registry = _registry(tmp_path)
    assert extensions_main(["list", "--kind", "hook", "--json"], registry=registry) == 0
    data = json.loads(capsys.readouterr().out)
    assert [item["extension_id"] for item in data] == ["hook:core"]

    assert extensions_main(["status", "hook:core", "--json"], registry=registry) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["contract_version"] == 1
    assert status["lifecycle"] == "static"


def test_extensions_cli_unknown_id_is_explicit_error(tmp_path, capsys):
    assert extensions_main(["status", "skill:missing"], registry=_registry(tmp_path)) == 1
    assert "Unknown extension 'skill:missing'" in capsys.readouterr().out


def test_extensions_cli_reload_projects_a_fresh_metadata_snapshot(tmp_path, capsys):
    registry = _registry(tmp_path)

    assert extensions_main(["reload"], registry=registry) == 0
    output = capsys.readouterr().out
    assert "Reloaded extension metadata" in output
    assert "hook:core" in output

    assert extensions_main(["reload", "--json"], registry=registry) == 0
    assert '"extension_id": "hook:core"' in capsys.readouterr().out


def test_skill_enable_disable_is_user_owned_and_runtime_effective(tmp_path, monkeypatch):
    user_config = tmp_path.parent / f"{tmp_path.name}-user" / "config.env"
    monkeypatch.setenv("NZ_CODER_USER_CONFIG", str(user_config))
    project = tmp_path / ".nz-coder" / "skills"
    _write_skill(project, "review", "description: project")
    loader = SkillLoader(
        bundled_dir=tmp_path / "missing-bundled",
        user_dir=tmp_path / "missing-user",
        project_dir=project,
    )
    registry = _registry(tmp_path, skill_loader=loader)

    disabled = registry.set_enabled("skill:review", False)

    assert disabled["status"] == "disabled"
    assert registry.get("skill:review").enabled is False
    assert "review" not in loader.descriptions()
    assert not (tmp_path / ".nz-coder" / "settings.json").exists()
    state = json.loads(user_config.with_name("workspace-grants.json").read_text())
    assert next(iter(state["workspaces"].values()))["disabled_skills"] == ["review"]

    enabled = registry.set_enabled("skill:review", True)
    assert enabled["status"] == "available"
    assert registry.get("skill:review").enabled is True
    assert "review" in loader.descriptions()


def test_extension_reload_delegates_to_real_owners_and_reports_restart_truth(tmp_path):
    loader = SkillLoader(
        bundled_dir=tmp_path / "missing-bundled",
        user_dir=tmp_path / "missing-user",
        project_dir=tmp_path / "missing-project",
    )

    class Runtime:
        def __init__(self):
            self.calls = 0

        def reload_config(self):
            self.calls += 1
            return True

        def extension_snapshot(self):
            return []

    runtime = Runtime()
    registry = _registry(tmp_path, skill_loader=loader)
    registry.mcp_runtime = runtime

    results = registry.reload()
    pack = registry.set_enabled("tool_pack:python_ast", False)

    assert {item["kind"] for item in results} == {"skill", "hook", "tool_pack", "mcp_server"}
    assert next(item for item in results if item["kind"] == "skill")["status"] == "reloaded"
    assert next(item for item in results if item["kind"] == "mcp_server")["status"] == "reloaded"
    assert runtime.calls == 1
    assert pack["status"] == "restart_required"


def test_extensions_cli_enable_disable_and_owner_reload(tmp_path, capsys):
    project = tmp_path / ".nz-coder" / "skills"
    _write_skill(project, "review", "description: project")
    registry = _registry(tmp_path, skill_loader=SkillLoader(
        bundled_dir=tmp_path / "missing-bundled",
        user_dir=tmp_path / "missing-user",
        project_dir=project,
    ))

    assert extensions_main(["disable", "skill:review", "--json"], registry=registry) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"
    assert extensions_main(["enable", "skill:review"], registry=registry) == 0
    assert "available" in capsys.readouterr().out
    assert extensions_main(["reload", "--json"], registry=registry) == 0
    payload = json.loads(capsys.readouterr().out)
    assert next(item for item in payload if item["kind"] == "skill")["enabled"] is True


def test_terminal_extension_controls_delegate_to_registry_owner(monkeypatch):
    from nz_coder.interface.commands.handlers.core import handle_extensions

    calls = []

    class Registry:
        def __init__(self, **_kwargs):
            pass

        def snapshot(self):
            return []

        def reload(self):
            calls.append(("reload",))
            return [{"kind": "skill", "status": "reloaded"}]

        def set_enabled(self, extension_id, enabled):
            calls.append(("set", extension_id, enabled))
            return {
                "extension_id": extension_id,
                "enabled": enabled,
                "status": "available" if enabled else "disabled",
                "restart_required": False,
            }

    output = []
    console = SimpleNamespace(print=lambda value, **_kwargs: output.append(str(value)))
    monkeypatch.setattr("nz_coder.extensions.registry.ExtensionRegistry", Registry)

    handle_extensions(SimpleNamespace(args="reload", console=console))
    handle_extensions(SimpleNamespace(args="disable skill:review", console=console))
    handle_extensions(SimpleNamespace(args="enable skill:review", console=console))

    assert calls == [
        ("reload",),
        ("set", "skill:review", False),
        ("set", "skill:review", True),
    ]
    assert any("reloaded" in item for item in output)
    assert any("disabled" in item for item in output)
