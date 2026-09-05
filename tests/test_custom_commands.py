"""Product contracts for inert Markdown prompt commands."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import pytest

from nz_coder.foundation.project_control import capture_project_control_snapshot


def _write(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(body, encoding="utf-8")


def _trusted_snapshot(workspace: Path):  # noqa: ANN202
    return replace(capture_project_control_snapshot(workspace), trusted=True)


def test_command_discovery_precedence_and_provenance(tmp_path):
    from nz_coder.interface.custom_commands import CommandCatalog

    project = tmp_path / ".nz-coder" / "commands"
    user = tmp_path / "user"
    bundled = tmp_path / "bundled"
    _write(bundled, "review", "---\ndescription: bundled\n---\nbundled $ARGUMENTS")
    _write(user, "review", "---\ndescription: user\n---\nuser $ARGUMENTS")
    _write(project, "review", "---\ndescription: project\n---\nproject $ARGUMENTS")

    catalog = CommandCatalog.discover(
        project_dir=project, user_dir=user, bundled_dir=bundled,
        project_trusted=True,
        project_control_snapshot=_trusted_snapshot(tmp_path),
    )

    command = catalog.get("review")
    assert command is not None
    assert command.source == "project"
    assert command.description == "project"
    assert catalog.expand("review", "src/runtime").prompt == "project src/runtime"


def test_command_expansion_is_inert_and_supports_raw_and_positional_args(tmp_path):
    from nz_coder.interface.custom_commands import CommandCatalog

    project = tmp_path / ".nz-coder" / "commands"
    _write(project, "fix-tests", """---
description: Fix selected tests
allowed_tools:
  - read_file
  - grep_search
  - bash
model: provider/model
---
Area=$1 Rest=$ARGUMENTS Second=$2 Literal=$(touch /tmp/unsafe)
""")
    catalog = CommandCatalog.discover(
        project_dir=project,
        project_trusted=True,
        project_control_snapshot=_trusted_snapshot(tmp_path),
    )

    expanded = catalog.expand("fix-tests", "auth tests/unit")

    assert expanded.prompt == (
        "Area=auth Rest=auth tests/unit Second=tests/unit "
        "Literal=$(touch /tmp/unsafe)"
    )
    assert expanded.allowed_tools == ("read_file", "grep_search", "bash")
    assert expanded.model == "provider/model"


def test_command_parser_rejects_unsafe_names_and_invalid_frontmatter(tmp_path):
    from nz_coder.interface.custom_commands import CommandCatalog, CommandParseError

    project = tmp_path / ".nz-coder" / "commands"
    _write(project, "bad name", "body")
    _write(project, "broken", "---\nallowed_tools: bash\n---\nbody")

    catalog = CommandCatalog.discover(
        project_dir=project,
        project_trusted=True,
        project_control_snapshot=_trusted_snapshot(tmp_path),
    )

    assert catalog.list() == ()
    assert len(catalog.errors) == 2
    assert all(isinstance(error, CommandParseError) for error in catalog.errors)


def test_command_frontmatter_accepts_windows_newlines(tmp_path):
    from nz_coder.interface.custom_commands import CommandCatalog

    command = tmp_path / "review.md"
    command.write_bytes(
        b"---\r\ndescription: Review a path\r\n"
        b"allowed_tools:\r\n  - read_file\r\n"
        b"---\r\nReview $ARGUMENTS\r\n"
    )

    catalog = CommandCatalog.discover(user_dir=tmp_path)

    parsed = catalog.get("review")
    assert parsed is not None
    assert parsed.description == "Review a path"
    assert parsed.allowed_tools == ("read_file",)
    assert parsed.template == "Review $ARGUMENTS"


def test_custom_commands_register_for_completion_without_overriding_builtins(tmp_path):
    from nz_coder.interface.commands import build_default_registry
    from nz_coder.interface.custom_commands import CommandCatalog, register_command_completion

    project = tmp_path / ".nz-coder" / "commands"
    _write(project, "review", "---\ndescription: Review changes\n---\nReview $ARGUMENTS")
    _write(project, "help", "---\ndescription: shadow help\n---\nshadow")
    catalog = CommandCatalog.discover(
        project_dir=project,
        project_trusted=True,
        project_control_snapshot=_trusted_snapshot(tmp_path),
    )
    registry = build_default_registry()

    register_command_completion(registry, catalog)

    commands = {command.name: command for command in registry.visible_commands()}
    assert commands["review"].description == "Review changes"
    assert commands["review"].category == "Custom"
    assert commands["help"].category == "General"


def test_unknown_command_does_not_expand(tmp_path):
    from nz_coder.interface.custom_commands import CommandCatalog

    catalog = CommandCatalog.discover(project_dir=tmp_path)
    with pytest.raises(KeyError):
        catalog.expand("missing", "")


def test_custom_command_tool_policy_can_only_narrow_host_allowlist(tmp_path):
    from nz_coder.runtime.adapters.runner import run_request_from_legacy_host

    host = SimpleNamespace(
        runtime_profile="main",
        current_agent_name="worker",
        tool_allowlist=("read_file", "grep_search"),
        permissions=SimpleNamespace(mode="default"),
        system_prompt="Coding agent",
        provider_id="offline",
        model_id="model",
        model_variant=None,
        workdir=tmp_path,
        session_id="session-command",
        parent_session_id=None,
    )

    request = run_request_from_legacy_host(
        host,
        [{"role": "user", "content": "review"}],
        True,
        allowed_tools=("read_file", "bash"),
    )

    assert request.tool_names == ("read_file",)
    assert request.agent.allowed_tools == ("read_file",)


def test_custom_command_model_override_is_per_run_and_does_not_mutate_host(tmp_path):
    from nz_coder.runtime.adapters.runner import run_request_from_legacy_host

    host = SimpleNamespace(
        runtime_profile="main", current_agent_name="worker", tool_allowlist=(),
        permissions=SimpleNamespace(mode="default"), system_prompt="Coding agent",
        provider_id="default-provider", model_id="default-model", model_variant=None,
        workdir=tmp_path, session_id="session-command", parent_session_id=None,
    )
    request = run_request_from_legacy_host(
        host,
        [{"role": "user", "content": "review"}],
        True,
        provider_override="command-provider",
        model_override="command-model",
    )

    assert (request.provider, request.model) == ("command-provider", "command-model")
    assert (host.provider_id, host.model_id) == ("default-provider", "default-model")


def test_installed_product_exposes_a_bundled_review_command(tmp_path):
    from nz_coder.interface.custom_commands import default_command_catalog

    catalog = default_command_catalog(tmp_path)

    command = catalog.get("review")
    assert command is not None
    assert command.source == "bundled"
    assert "$ARGUMENTS" in command.template


def _trust_project_control(workspace: Path, monkeypatch) -> None:
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )

    trust_path = workspace.parent / f"{workspace.name}-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    snapshot = load_config_snapshot(workspace)
    WorkspaceTrustStore(trust_path).trust(
        workspace, "workspace-control", snapshot.control_fingerprint
    )


def test_untrusted_project_command_is_not_discovered(tmp_path, monkeypatch):
    from nz_coder.interface.custom_commands import default_command_catalog

    _write(tmp_path / ".nz-coder" / "commands", "repo-only", "repo prompt")
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path.parent / "empty-trust.json")
    )

    assert default_command_catalog(tmp_path).get("repo-only") is None


def test_untrusted_project_command_cannot_override_bundled_or_select_model(
    tmp_path, monkeypatch,
):
    from nz_coder.interface.custom_commands import default_command_catalog

    _write(
        tmp_path / ".nz-coder" / "commands",
        "review",
        "---\nmodel: attacker/expensive\nallowed_tools:\n  - bash\n---\nrepo prompt",
    )
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path.parent / "empty-trust.json")
    )

    command = default_command_catalog(tmp_path).get("review")
    assert command is not None
    assert command.source == "bundled"
    assert command.model is None
    assert "repo prompt" not in command.template


def test_trusted_project_command_can_override_after_exact_trust(tmp_path, monkeypatch):
    from nz_coder.interface.custom_commands import default_command_catalog

    command_path = tmp_path / ".nz-coder" / "commands" / "review.md"
    _write(command_path.parent, "review", "trusted project prompt")
    _trust_project_control(tmp_path, monkeypatch)

    trusted = default_command_catalog(tmp_path).get("review")
    assert trusted is not None and trusted.source == "project"

    command_path.write_text("changed project prompt", encoding="utf-8")
    changed = default_command_catalog(tmp_path).get("review")
    assert changed is not None and changed.source == "bundled"


def test_tui_newly_trusted_command_executes_without_restart(tmp_path):
    from nz_coder.interface.cli import _resolve_submission_command
    from nz_coder.interface.commands import build_default_registry
    from nz_coder.interface.custom_commands import CommandCatalog

    startup_registry = build_default_registry()
    _write(tmp_path, "new", "Run $ARGUMENTS")
    current_catalog = CommandCatalog.discover(user_dir=tmp_path)

    expanded, unknown = _resolve_submission_command(
        "/new target", startup_registry, current_catalog,
    )

    assert unknown is False
    assert expanded is not None and expanded.prompt == "Run target"


def test_tui_removed_command_is_unknown_without_restart(tmp_path):
    from nz_coder.interface.cli import _resolve_submission_command
    from nz_coder.interface.commands import build_default_registry
    from nz_coder.interface.custom_commands import CommandCatalog, register_command_completion

    _write(tmp_path, "old", "Old prompt")
    startup = CommandCatalog.discover(user_dir=tmp_path)
    registry = build_default_registry()
    register_command_completion(registry, startup)

    expanded, unknown = _resolve_submission_command(
        "/old", registry, CommandCatalog(),
    )

    assert expanded is None
    assert unknown is True


def test_builtin_command_wins_over_dynamic_custom_command(tmp_path):
    from nz_coder.interface.cli import _resolve_submission_command
    from nz_coder.interface.commands import build_default_registry
    from nz_coder.interface.custom_commands import CommandCatalog

    _write(tmp_path, "help", "shadow built-in")
    catalog = CommandCatalog.discover(user_dir=tmp_path)

    expanded, unknown = _resolve_submission_command(
        "/help", build_default_registry(), catalog,
    )

    assert expanded is None
    assert unknown is False


def test_command_model_and_allowed_tools_rotate_with_snapshot(tmp_path):
    from nz_coder.interface.cli import _resolve_submission_command
    from nz_coder.interface.commands import build_default_registry
    from nz_coder.interface.custom_commands import CommandCatalog

    command = tmp_path / "rotate.md"
    command.write_text(
        "---\nmodel: provider/one\nallowed_tools: [read_file]\n---\none",
        encoding="utf-8",
    )
    first = CommandCatalog.discover(user_dir=tmp_path)
    command.write_text(
        "---\nmodel: provider/two\nallowed_tools: [grep_search]\n---\ntwo",
        encoding="utf-8",
    )
    second = CommandCatalog.discover(user_dir=tmp_path)
    registry = build_default_registry()

    one, _ = _resolve_submission_command("/rotate", registry, first)
    two, _ = _resolve_submission_command("/rotate", registry, second)

    assert one is not None and (one.model, one.allowed_tools) == (
        "provider/one", ("read_file",),
    )
    assert two is not None and (two.model, two.allowed_tools) == (
        "provider/two", ("grep_search",),
    )


def test_tui_command_change_requires_new_trust(tmp_path, monkeypatch):
    from nz_coder.interface.cli import _resolve_submission_command
    from nz_coder.interface.commands import build_default_registry
    from nz_coder.interface.custom_commands import default_command_catalog

    command = tmp_path / ".nz-coder" / "commands" / "dynamic.md"
    _write(command.parent, "dynamic", "old prompt")
    _trust_project_control(tmp_path, monkeypatch)
    trusted = default_command_catalog(tmp_path)
    command.write_text("changed prompt", encoding="utf-8")
    changed = default_command_catalog(tmp_path)

    old, old_unknown = _resolve_submission_command(
        "/dynamic", build_default_registry(), trusted,
    )
    new, new_unknown = _resolve_submission_command(
        "/dynamic", build_default_registry(), changed,
    )

    assert old is not None and old.prompt == "old prompt" and old_unknown is False
    assert new is None and new_unknown is True


def test_command_expansion_and_run_share_submission_snapshot(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.interface.cli import _resolve_submission_command
    from nz_coder.interface.commands import build_default_registry
    from nz_coder.interface.custom_commands import default_command_catalog

    command = tmp_path / ".nz-coder" / "commands" / "epoch.md"
    _write(command.parent, "epoch", "epoch-one")
    _trust_project_control(tmp_path, monkeypatch)
    submission_snapshot = load_config_snapshot(tmp_path)
    command.write_text("epoch-two", encoding="utf-8")
    catalog = default_command_catalog(
        tmp_path, config_snapshot=submission_snapshot,
    )

    expanded, unknown = _resolve_submission_command(
        "/epoch", build_default_registry(), catalog,
    )

    assert unknown is False
    assert expanded is not None and expanded.prompt == "epoch-one"
    assert submission_snapshot.project_control.get(
        ".nz-coder/commands/epoch.md"
    ).content == b"epoch-one"
