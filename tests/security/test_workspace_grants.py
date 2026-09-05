"""Security contracts for user-owned, workspace-scoped permission grants."""
from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

from nz_coder.foundation import config


def _configure_store(tmp_path, monkeypatch) -> Path:
    user_config = tmp_path / "user" / "config.env"
    monkeypatch.setenv("NZ_CODER_USER_CONFIG", str(user_config))
    return user_config.with_name("workspace-grants.json")


def _persist_in_process(arguments):
    workspace, user_config, command = arguments
    import os

    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.permissioning.rules import (
        persist_allow_rule,
        scoped_allow_rule,
    )

    os.environ["NZ_CODER_USER_CONFIG"] = str(user_config)
    with scoped_workdir(workspace):
        persist_allow_rule(scoped_allow_rule("bash", {"command": command}))


def test_always_allow_is_stored_outside_workspace(tmp_path, monkeypatch):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "repo"
    workspace.mkdir()
    grant_path = _configure_store(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "WORKDIR", workspace)
    with scoped_workdir(workspace):
        manager = PermissionManager("default", asker=lambda *_args: "always")
        assert manager.ask_user("edit_file", {"path": "app.py"}) is True

    assert grant_path.is_file()
    assert workspace not in grant_path.parents
    assert not (workspace / ".nz-coder" / "settings.json").exists()


def test_always_allow_survives_new_permission_manager(tmp_path, monkeypatch):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "repo"
    workspace.mkdir()
    _configure_store(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "WORKDIR", workspace)
    with scoped_workdir(workspace):
        PermissionManager("default", asker=lambda *_args: "always").ask_user(
            "bash", {"command": "git status"}
        )
        reloaded = PermissionManager("default")

    assert reloaded.check("bash", {"command": "git status --short"})["behavior"] == "allow"
    assert reloaded.check("bash", {"command": "git commit -m unsafe"})["behavior"] == "ask"


def test_user_grant_is_bound_to_exact_workspace_identity(tmp_path, monkeypatch):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.process.workdir import scoped_workdir

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _configure_store(tmp_path, monkeypatch)
    with scoped_workdir(first):
        PermissionManager("default", asker=lambda *_args: "always").ask_user(
            "edit_file", {"path": "app.py"}
        )
    with scoped_workdir(second):
        other = PermissionManager("default")

    assert other.check("edit_file", {"path": "app.py"})["behavior"] == "ask"


def test_user_grant_does_not_override_explicit_deny(tmp_path, monkeypatch):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "repo"
    settings = workspace / ".nz-coder" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"permissions":{"deny":["edit_file"]}}', encoding="utf-8")
    _configure_store(tmp_path, monkeypatch)
    with scoped_workdir(workspace):
        PermissionManager("default", asker=lambda *_args: "always").ask_user(
            "edit_file", {"path": "app.py"}
        )
        reloaded = PermissionManager("default", workspace_trusted=False)

    assert reloaded.check("edit_file", {"path": "app.py"})["behavior"] == "deny"


def test_corrupt_user_grant_store_fails_closed(tmp_path, monkeypatch):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "repo"
    workspace.mkdir()
    grant_path = _configure_store(tmp_path, monkeypatch)
    grant_path.parent.mkdir(parents=True)
    grant_path.write_text("{broken", encoding="utf-8")
    with scoped_workdir(workspace):
        manager = PermissionManager("default")
    assert manager.check("edit_file", {"path": "app.py"})["behavior"] == "ask"


def test_user_grant_store_rejects_symlink(tmp_path, monkeypatch):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "repo"
    workspace.mkdir()
    grant_path = _configure_store(tmp_path, monkeypatch)
    grant_path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text('{"sentinel":true}', encoding="utf-8")
    grant_path.symlink_to(outside)
    with scoped_workdir(workspace):
        manager = PermissionManager("default", asker=lambda *_args: "always")
        assert manager.ask_user("edit_file", {"path": "app.py"}) is True
        reloaded = PermissionManager("default")
    assert json.loads(outside.read_text(encoding="utf-8")) == {"sentinel": True}
    assert reloaded.check("edit_file", {"path": "app.py"})["behavior"] == "ask"


def test_concurrent_user_grant_writes_remain_consistent(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    user_config = tmp_path / "user" / "config.env"
    monkeypatch.setenv("NZ_CODER_USER_CONFIG", str(user_config))
    context = multiprocessing.get_context("spawn")
    commands = [f"git status --short path-{index}" for index in range(6)]
    with context.Pool(3) as pool:
        pool.map(
            _persist_in_process,
            [(workspace, user_config, command) for command in commands],
        )

    payload = json.loads(user_config.with_name("workspace-grants.json").read_text())
    entries = next(iter(payload["workspaces"].values()))["allow"]
    assert len(entries) == len(commands)


def test_external_project_settings_change_does_not_gain_user_authority(
    tmp_path, monkeypatch,
):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "repo"
    workspace.mkdir()
    _configure_store(tmp_path, monkeypatch)
    with scoped_workdir(workspace):
        PermissionManager("default", asker=lambda *_args: "always").ask_user(
            "edit_file", {"path": "app.py"}
        )
        settings = workspace / ".nz-coder" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text('{"permissions":{"allow":["bash"]}}', encoding="utf-8")
        reloaded = PermissionManager("auto", workspace_trusted=False)

    assert reloaded.check("edit_file", {"path": "app.py"})["behavior"] == "allow"
    assert reloaded.check("bash", {"command": "python attack.py"})["behavior"] == "ask"
