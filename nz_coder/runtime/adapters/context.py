"""Legacy Agent host adapter for the focused Context Runtime contract."""
from __future__ import annotations

from pathlib import Path

from nz_coder.runtime.core.context import ContextExecutionContext
from nz_coder.runtime.process.workdir import current_workdir


def context_from_legacy_host(host) -> ContextExecutionContext:
    """Snapshot Context capabilities from a compatibility Agent host."""
    tracer = host.tracer

    def report_pressure(payload: dict) -> None:
        run_context = getattr(host, "active_run_context", None)
        metadata = getattr(run_context, "metadata", None)
        if isinstance(metadata, dict):
            metadata["context_pressure"] = dict(payload)

    return ContextExecutionContext(
        workspace=Path(getattr(host, "workdir", current_workdir())),
        budget=host._prompt_budget(),
        projected_tokens=host._projected_request_tokens,
        compact=host._compact_messages,
        stamp_auto_compaction=host._stamp_auto_compaction,
        trace=tracer.log,
        report_pressure=report_pressure,
        projected_replay_tokens=getattr(host, "_projected_replay_tokens", None),
        cancel_compaction=getattr(host, "_cancel_compaction", None),
    )
