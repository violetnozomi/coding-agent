"""Legacy host adapter for focused memory execution."""
from __future__ import annotations

from nz_coder.runtime.core.memory_context import MemoryExecutionContext, MemoryRecallState


def memory_context_from_legacy_host(host) -> MemoryExecutionContext:
    """Snapshot only the capabilities required by ProductionMemoryService."""
    state = MemoryRecallState(
        last_query=str(getattr(host, "_last_memory_query", "") or ""),
        last_block=str(getattr(host, "_last_memory_block", "") or ""),
    )

    def commit(recall: MemoryRecallState) -> None:
        host._last_memory_query = recall.last_query
        host._last_memory_block = recall.last_block

    return MemoryExecutionContext(
        manager=host._mm,
        session_id=str(host.session_id),
        client=getattr(host, "client", None),
        model_id=str(host._active_model_id()),
        tracer=getattr(host, "tracer", None),
        lineage=getattr(host, "lineage", None),
        recall=state,
        commit_recall=commit,
    )
