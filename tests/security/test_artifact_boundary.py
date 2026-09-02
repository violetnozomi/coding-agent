"""Opaque, session-owned tool-result artifact contracts."""
from __future__ import annotations

import os

import pytest


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
