"""Product contracts for inert Markdown prompt commands."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _write(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(body, encoding="utf-8")


def test_command_discovery_precedence_and_provenance(tmp_path):
    from nz_coder.interface.custom_commands import CommandCatalog

    project = tmp_path / "project"
    user = tmp_path / "user"
    bundled = tmp_path / "bundled"
    _write(bundled, "review", "---\ndescription: bundled\n---\nbundled $ARGUMENTS")
    _write(user, "review", "---\ndescription: user\n---\nuser $ARGUMENTS")
    _write(project, "review", "---\ndescription: project\n---\nproject $ARGUMENTS")

    catalog = CommandCatalog.discover(
        project_dir=project, user_dir=user, bundled_dir=bundled,
    )

    command = catalog.get("review")
    assert command is not None
    assert command.source == "project"
    assert command.description == "project"
    assert catalog.expand("review", "src/runtime").prompt == "project src/runtime"


def test_command_expansion_is_inert_and_supports_raw_and_positional_args(tmp_path):
    from nz_coder.interface.custom_commands import CommandCatalog

    project = tmp_path / "commands"
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
    catalog = CommandCatalog.discover(project_dir=project)

    expanded = catalog.expand("fix-tests", "auth tests/unit")

    assert expanded.prompt == (
        "Area=auth Rest=auth tests/unit Second=tests/unit "
        "Literal=$(touch /tmp/unsafe)"
    )
    assert expanded.allowed_tools == ("read_file", "grep_search", "bash")
    assert expanded.model == "provider/model"


def test_command_parser_rejects_unsafe_names_and_invalid_frontmatter(tmp_path):
    from nz_coder.interface.custom_commands import CommandCatalog, CommandParseError

    project = tmp_path / "commands"
    _write(project, "bad name", "body")
    _write(project, "broken", "---\nallowed_tools: bash\n---\nbody")

    catalog = CommandCatalog.discover(project_dir=project)

    assert catalog.list() == ()
    assert len(catalog.errors) == 2
    assert all(isinstance(error, CommandParseError) for error in catalog.errors)


def test_custom_commands_register_for_completion_without_overriding_builtins(tmp_path):
    from nz_coder.interface.commands import build_default_registry
    from nz_coder.interface.custom_commands import CommandCatalog, register_command_completion

    project = tmp_path / "commands"
    _write(project, "review", "---\ndescription: Review changes\n---\nReview $ARGUMENTS")
    _write(project, "help", "---\ndescription: shadow help\n---\nshadow")
    catalog = CommandCatalog.discover(project_dir=project)
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
