"""Model tool for bounded reads of current-Session tool-result artifacts."""
from __future__ import annotations

import json

from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.state.sessions import active_session_id
from nz_coder.tool_platform.artifacts import ArtifactError, ArtifactStore
from nz_coder.tools import ToolOutput, register


def read_tool_result(
    artifact_id: str,
    offset: int = 0,
    max_bytes: int = 64 * 1024,
) -> str:
    """Read a bounded chunk from an opaque artifact owned by this Session."""
    session_id = active_session_id()
    if not session_id:
        return "Error: No active Session owns this artifact request"
    try:
        chunk = ArtifactStore(current_workdir(), session_id).read_chunk(
            artifact_id,
            offset=offset,
            max_bytes=max_bytes,
        )
    except (ArtifactError, ValueError) as exc:
        return f"Error: {exc}"
    trailer = json.dumps(
        {
            "artifact_id": artifact_id,
            "next_offset": chunk.next_offset,
            "has_more": chunk.has_more,
            "total_bytes": chunk.total_bytes,
        },
        sort_keys=True,
    )
    return ToolOutput(
        f"{chunk.text}\n\n<artifact-read>{trailer}</artifact-read>",
        title=f"Read artifact {artifact_id[-8:]}",
        metadata={
            "artifact_id": artifact_id,
            "next_offset": chunk.next_offset,
            "has_more": chunk.has_more,
        },
    )


register(
    name="read_tool_result",
    description=(
        "Read a bounded chunk of a persisted tool result by its opaque artifact ID. "
        "Only artifacts owned by the current Session are accessible."
    ),
    parameters={
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 65536},
        },
        "required": ["artifact_id"],
    },
    handler=read_tool_result,
    execution="read",
)
