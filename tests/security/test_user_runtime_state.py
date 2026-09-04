"""Repository data cannot impersonate user-owned runtime state or cache."""
from __future__ import annotations

import json
import os

import pytest


def _set_user_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "user-state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "user-cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))


def test_workspace_nz_coder_symlink_cannot_redirect_runtime_state(
    tmp_path, monkeypatch,
):
    from nz_coder.state.workdir import current_derived_path, scoped_workdir

    _set_user_roots(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    outside = tmp_path / "attacker-state"
    workspace.mkdir()
    outside.mkdir()
    try:
        (workspace / ".nz-coder").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with scoped_workdir(workspace):
        session_root = current_derived_path("SESSION_DIR")
        session_root.mkdir(parents=True)

    assert not session_root.is_relative_to(workspace)
    assert not session_root.is_relative_to(outside)
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_workspace_junction_cannot_redirect_runtime_state(tmp_path, monkeypatch):
    import subprocess

    from nz_coder.state.workdir import current_derived_path, scoped_workdir

    _set_user_roots(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    outside = tmp_path / "attacker-state"
    workspace.mkdir()
    outside.mkdir()
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(workspace / ".nz-coder"), str(outside)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    with scoped_workdir(workspace):
        session_root = current_derived_path("SESSION_DIR")
        session_root.mkdir(parents=True)
    assert not session_root.is_relative_to(workspace)
    assert list(outside.iterdir()) == []


def test_repository_preseeded_session_is_ignored(tmp_path, monkeypatch):
    from nz_coder.state.sessions import list_session_ids, session_dir
    from nz_coder.state.workdir import scoped_workdir

    _set_user_roots(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    seeded = workspace / ".nz-coder" / "sessions"
    seeded.mkdir(parents=True)
    (seeded / "session-attacker.json").write_text(
        json.dumps({"session_id": "session-attacker"}), encoding="utf-8"
    )

    with scoped_workdir(workspace):
        assert list_session_ids() == []
        assert not session_dir().is_relative_to(workspace)


def test_repository_preseeded_memory_is_not_auto_injected(tmp_path, monkeypatch):
    from nz_coder.state.memory import MemoryManager
    from nz_coder.state.workdir import scoped_workdir

    _set_user_roots(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    seeded = workspace / ".nz-coder" / "memory"
    seeded.mkdir(parents=True)
    (seeded / "MEMORY.md").write_text(
        "- [attack](attack.md) — poisoned [project]", encoding="utf-8"
    )
    (seeded / "attack.md").write_text("PRESEEDED-MEMORY", encoding="utf-8")

    with scoped_workdir(workspace):
        manager = MemoryManager()
        assert not manager.memory_dir.is_relative_to(workspace)
        assert "PRESEEDED-MEMORY" not in manager.build_prompt_block("poisoned")


def test_repository_preseeded_registry_cannot_change_api_model_id(
    tmp_path, monkeypatch,
):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.providers.models import active_model_selection

    _set_user_roots(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    registry = workspace / ".nz-coder" / "models" / "registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({
        "version": 1,
        "schema": "models.dev.normalized/v1",
        "source": "https://attacker.invalid/registry",
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "content_digest": "0" * 64,
        "providers": {
            "openai-compatible": {
                "models": {
                    "trusted-model": {
                        "api_model_id": "attacker-model",
                        "capabilities": {"context_tokens": 999999},
                    }
                }
            }
        },
    }), encoding="utf-8")
    snapshot = load_config_snapshot(
        workspace,
        environ={"MODEL_ID": "trusted-model", "MODEL_PROVIDER": "openai-compatible"},
        user_config_path=tmp_path / "user-config" / "config.env",
    )

    selected = active_model_selection(workspace, config_snapshot=snapshot)
    assert selected.model_id == "trusted-model"
    assert selected.source == "configuration"
    from nz_coder.providers.registry import registry_runtime_model

    assert registry_runtime_model("openai-compatible", "trusted-model", workspace) is None


def test_user_runtime_state_is_keyed_by_workspace_identity(tmp_path, monkeypatch):
    from nz_coder.foundation.user_paths import user_storage_layout

    _set_user_roots(monkeypatch, tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    first_layout = user_storage_layout(first)
    second_layout = user_storage_layout(second)

    assert first_layout.workspace_state != second_layout.workspace_state
    assert first.name not in first_layout.workspace_state.parts[-1]
    assert second.name not in second_layout.workspace_state.parts[-1]


def test_user_state_root_is_private(tmp_path, monkeypatch):
    from nz_coder.foundation.private_paths import inspect_private_path
    from nz_coder.foundation.user_paths import prepare_user_storage

    _set_user_roots(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    layout = prepare_user_storage(workspace)

    assert inspect_private_path(layout.workspace_state).hardened is True
    assert inspect_private_path(layout.workspace_cache).hardened is True


def test_model_tools_cannot_read_user_state_root(tmp_path, monkeypatch):
    from nz_coder.foundation.user_paths import prepare_user_storage
    from nz_coder.foundation.workspace_paths import WorkspacePathError, WorkspacePathPolicy

    _set_user_roots(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    layout = prepare_user_storage(workspace)
    secret = layout.workspace_state / "sessions" / "private.json"
    secret.parent.mkdir(parents=True)
    secret.write_text("PRIVATE", encoding="utf-8")

    with pytest.raises(WorkspacePathError):
        WorkspacePathPolicy(workspace).validate_model_read(str(secret))


def test_explicit_legacy_state_migration_is_bounded_and_non_destructive(
    tmp_path, monkeypatch,
):
    from nz_coder.foundation.user_paths import user_storage_layout
    from nz_coder.state.migration import migrate_legacy_state

    _set_user_roots(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    trace = workspace / ".nz-coder" / "runs" / "run.jsonl"
    memory = workspace / ".nz-coder" / "memory" / "poison.md"
    trace.parent.mkdir(parents=True)
    memory.parent.mkdir(parents=True)
    trace.write_text('{"event":"safe-record"}\n', encoding="utf-8")
    memory.write_text("POISONED-PROJECT-MEMORY", encoding="utf-8")

    preview = migrate_legacy_state(workspace)
    layout = user_storage_layout(workspace)
    destination = layout.workspace_state / "runs" / "run.jsonl"
    assert len(preview.planned) == 1
    assert not destination.exists()

    migrated = migrate_legacy_state(workspace, apply=True)
    assert len(migrated.copied) == 1
    assert destination.read_text(encoding="utf-8") == trace.read_text(encoding="utf-8")
    assert trace.exists()
    assert not (layout.workspace_state / "memory" / "poison.md").exists()

    oversized = workspace / ".nz-coder" / "changes" / "oversized.bin"
    oversized.parent.mkdir(parents=True)
    with oversized.open("wb") as stream:
        stream.truncate(9 * 1024 * 1024)
    with pytest.raises(ValueError, match="exceeds 8 MiB"):
        migrate_legacy_state(workspace, include=("changes",), apply=True)
    assert oversized.exists()
