"""Opaque, session-owned tool-result artifact contracts."""
from __future__ import annotations

import os
import json

import pytest


def _put_artifact_in_process(arguments):
    workspace, index = arguments
    from nz_coder.tool_platform.artifacts import ArtifactStore

    return ArtifactStore(
        workspace,
        "session-process",
        max_session_files=64,
    ).put(f"process-{index}", kind="tool-result")


def test_current_session_can_read_opaque_tool_result_only(tmp_path):
    from nz_coder.state.sessions import scoped_session
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.artifacts import ArtifactStore

    with scoped_workdir(tmp_path), scoped_session("session-a"):
        store = ArtifactStore(tmp_path, "session-a")
        artifact_id = store.put("visible output", kind="tool-result")

        assert artifact_id.startswith("artifact_")
        assert ".nz-coder" not in artifact_id
        assert store.read(artifact_id) == "visible output"
        assert os.stat(store.directory).st_mode & 0o077 == 0


def test_other_session_and_arbitrary_paths_cannot_read_artifact(tmp_path):
    from nz_coder.tool_platform.artifacts import ArtifactAccessError, ArtifactStore

    owner = ArtifactStore(tmp_path, "session-a")
    artifact_id = owner.put("private session output", kind="tool-result")
    other = ArtifactStore(tmp_path, "session-b")

    with pytest.raises(ArtifactAccessError, match="not owned"):
        other.read(artifact_id)
    with pytest.raises(ArtifactAccessError, match="invalid artifact id"):
        owner.read("../../.nz-coder/sessions/private.json")


def test_provider_private_metadata_is_not_an_allowed_model_artifact(tmp_path):
    from nz_coder.tool_platform.artifacts import ArtifactAccessError, ArtifactStore

    store = ArtifactStore(tmp_path, "session-a")

    with pytest.raises(ArtifactAccessError, match="artifact type"):
        store.put("sentinel-provider-private", kind="provider-private")


def test_projector_persists_opaque_reference_readable_by_registered_tool(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session
    from nz_coder.tool_platform.results import ToolResultBudget, ToolResultProjector
    from nz_coder.tools.artifacts import read_tool_result

    with scoped_workdir(tmp_path), scoped_session("session-a"):
        projected = ToolResultProjector(budget=ToolResultBudget(32)).project(
            "call-1", "sentinel-output-" * 200, tool_name="bash"
        )
        artifact_id = projected.artifact_path
        assert artifact_id is not None
        assert artifact_id.startswith("artifact_")
        assert ".nz-coder" not in projected.text
        result = read_tool_result(artifact_id, offset=0, max_bytes=64)

    assert "sentinel-output" in result
    assert "next_offset" in result


def test_artifact_single_read_is_bounded(tmp_path):
    from nz_coder.tool_platform.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path, "session-a", max_read_bytes=16)
    artifact_id = store.put("x" * 100, kind="tool-result")

    chunk = store.read_chunk(artifact_id, offset=0, max_bytes=10)

    assert chunk.text == "x" * 10
    assert chunk.next_offset == 10
    assert chunk.has_more is True


def test_artifact_quotas_fail_without_echoing_output(tmp_path):
    from nz_coder.tool_platform.artifacts import ArtifactQuotaError, ArtifactStore

    secret = "sentinel-artifact-secret"
    store = ArtifactStore(
        tmp_path,
        "session-a",
        max_result_bytes=16,
        max_session_bytes=20,
    )
    with pytest.raises(ArtifactQuotaError) as single:
        store.put(secret * 10, kind="tool-result")
    assert secret not in str(single.value)

    store.put("x" * 12, kind="tool-result")
    with pytest.raises(ArtifactQuotaError) as session:
        store.put("y" * 12, kind="tool-result")
    assert "yyyy" not in str(session.value)


def test_workspace_lru_cleanup_never_deletes_current_session_entries(tmp_path):
    from nz_coder.tool_platform.artifacts import ArtifactAccessError, ArtifactStore

    old = ArtifactStore(
        tmp_path,
        "old-session",
        max_workspace_files=1,
        max_workspace_bytes=1024,
        clock=lambda: 1.0,
    )
    old_id = old.put("old", kind="tool-result")
    current = ArtifactStore(
        tmp_path,
        "current-session",
        max_workspace_files=1,
        max_workspace_bytes=1024,
        clock=lambda: 100.0,
    )
    current_id = current.put("current", kind="tool-result")

    assert current.read(current_id) == "current"
    assert current.last_cleanup
    with pytest.raises(ArtifactAccessError):
        old.read(old_id)


def test_concurrent_artifact_writes_remain_manifest_consistent(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from nz_coder.tool_platform.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path, "session-a", max_session_files=64)
    with ThreadPoolExecutor(max_workers=8) as pool:
        artifact_ids = list(pool.map(
            lambda index: store.put(f"value-{index}", kind="tool-result"),
            range(32),
        ))

    assert len(set(artifact_ids)) == 32
    assert [store.read(item) for item in artifact_ids] == [
        f"value-{index}" for index in range(32)
    ]


def test_cross_process_artifact_writes_remain_manifest_consistent(tmp_path):
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing

    from nz_coder.tool_platform.artifacts import ArtifactStore

    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        artifact_ids = list(pool.map(
            _put_artifact_in_process,
            [(tmp_path, index) for index in range(12)],
        ))

    store = ArtifactStore(tmp_path, "session-process", max_session_files=64)
    assert len(set(artifact_ids)) == 12
    assert sorted(store.read(item) for item in artifact_ids) == sorted(
        f"process-{index}" for index in range(12)
    )


def test_workspace_cleanup_preserves_artifact_referenced_by_saved_session(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import save_session, scoped_session
    from nz_coder.tool_platform.artifacts import ArtifactQuotaError, ArtifactStore

    with scoped_workdir(tmp_path), scoped_session("old-session"):
        old = ArtifactStore(
            tmp_path,
            "old-session",
            max_workspace_files=1,
            max_workspace_bytes=1024,
            clock=lambda: 1.0,
        )
        artifact_id = old.put("durable", kind="tool-result")
        save_session(
            [{"role": "tool", "content": f"truncated\n[full:{artifact_id}]"}],
            session_id="old-session",
            activate=False,
        )

    current = ArtifactStore(
        tmp_path,
        "current-session",
        max_workspace_files=1,
        max_workspace_bytes=1024,
        clock=lambda: 100.0,
    )
    with pytest.raises(ArtifactQuotaError, match="workspace file quota"):
        current.put("current", kind="tool-result")

    assert old.read(artifact_id) == "durable"


def test_session_quota_uses_file_stat_not_manifest_size(tmp_path):
    from nz_coder.tool_platform.artifacts import ArtifactQuotaError, ArtifactStore

    store = ArtifactStore(
        tmp_path,
        "session-a",
        max_result_bytes=100,
        max_session_bytes=100,
    )
    store.put("x" * 60, kind="tool-result")
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    next(iter(manifest["entries"].values()))["size"] = 0
    store.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactQuotaError, match="Session byte quota"):
        store.put("y" * 50, kind="tool-result")


def test_artifact_store_rejects_workspace_local_configured_session_directory(
    tmp_path,
    monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.artifacts import ArtifactAccessError, ArtifactStore

    configured = tmp_path / "private-sessions"
    monkeypatch.setattr(config, "SESSION_DIR", configured)
    with scoped_workdir(tmp_path):
        with pytest.raises(ArtifactAccessError, match="outside the workspace"):
            ArtifactStore(tmp_path, "session-a")
