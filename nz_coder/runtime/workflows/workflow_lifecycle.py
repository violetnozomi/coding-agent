"""Read and recoverably archive persisted workflow run products."""
from __future__ import annotations

import json
import time
from pathlib import Path

from nz_coder.runtime.workflows.workflow_run_store import (
    archive_workflow_run,
    list_workflow_run_records,
    read_workflow_run_artifact,
    read_workflow_run_record,
    WorkflowRunStore,
)
from nz_coder.runtime.workflows.workflow_host import validate_workflow_display_name
from nz_coder.protocol.public_error import format_public_error
from nz_coder.tools import ToolOutput, register


def _runs_root(manager):
    return manager._workflow.root / "runs"


def workflow_runs(
    action: str,
    run_id: str = "",
    artifact: str = "",
    limit: int = 100,
) -> str:
    """Read terminal run records and declared artifacts without scanning workspace."""
    try:
        from nz_coder.runtime.agent.agent_manager import _current_manager

        manager = _current_manager()
        normalized = str(action or "").strip().lower()
        if normalized == "list":
            bounded = max(0, min(int(limit), 1000))
            records = list_workflow_run_records(_runs_root(manager), bounded)
            active = manager.workflow_run_snapshots()
            active_ids = {str(item.get("run_id") or "") for item in active}
            summaries = [{
                "run_id": item.get("run_id"),
                "workflow_name": item.get("workflow_name"),
                "display_name": item.get("display_name"),
                "status": item.get("status"),
                "ended_at": item.get("ended_at"),
                "phase_names": item.get("phase_names") or [],
                "artifacts": item.get("artifacts") or [],
            } for item in records if str(item.get("run_id") or "") not in active_ids]
            summaries = [
                {
                    "run_id": item.get("run_id"),
                    "display_name": item.get("name"),
                    "status": item.get("status"),
                    "started_at": item.get("started_at"),
                    "active": item.get("status") in {"running", "paused"},
                }
                for item in active
            ] + summaries
            summaries = summaries[:bounded]
            return ToolOutput(
                f"Persisted workflow runs: {len(summaries)}.",
                title="Workflow run history",
                metadata={"workflow_runs": summaries},
            )
        if normalized == "read":
            record = read_workflow_run_record(_runs_root(manager), run_id)
            return ToolOutput(
                json.dumps(record, ensure_ascii=False, indent=2),
                title=f"Workflow run: {run_id}",
                metadata={"workflow_run": record},
            )
        if normalized == "artifact":
            value = read_workflow_run_artifact(
                _runs_root(manager), run_id, artifact
            )
            return ToolOutput(
                json.dumps(value, ensure_ascii=False, indent=2),
                title=f"Workflow artifact: {artifact}",
                metadata={"workflow_artifact": value, "run_id": run_id},
            )
        if normalized == "result":
            record = read_workflow_run_record(_runs_root(manager), run_id)
            summary = str(record.get("result_summary") or "").strip()
            if not summary:
                return f"Error: workflow result summary not found: {run_id}"
            return ToolOutput(
                summary,
                title=f"Workflow result: {run_id}",
                metadata={"run_id": run_id, "result_summary": summary},
            )
        return "Error: action must be list, read, artifact, or result"
    except Exception as exc:
        return format_public_error(exc)


def workflow_run_archive(
    run_ids: list[str],
    confirm: bool = False,
    keep: int = 0,
    older_than_days: int = 0,
    dry_run: bool = False,
) -> str:
    """Move exact terminal runs into recoverable workflow trash."""
    try:
        from nz_coder.runtime.agent.agent_manager import _current_manager

        if not confirm and not dry_run:
            return "Error: confirm=true is required to archive workflow runs"
        manager = _current_manager()
        explicit = list(dict.fromkeys(str(item) for item in run_ids if str(item)))
        requested = list(explicit)
        records = list_workflow_run_records(_runs_root(manager), 1000)
        records_by_id = {str(item.get("run_id")): item for item in records}
        if keep > 0:
            requested.extend(
                str(item.get("run_id")) for item in records[max(0, keep):]
            )
        if older_than_days > 0:
            cutoff = time.time() - min(older_than_days, 36500) * 86400
            requested.extend(
                str(item.get("run_id")) for item in records
                if float(item.get("ended_at") or 0) < cutoff
            )
        requested = list(dict.fromkeys(requested))
        active = {
            item["run_id"] for item in manager.workflow_run_snapshots()
            if item["status"] in {"running", "paused"}
        }
        blocked = sorted(active.intersection(requested))
        if blocked:
            return "Error: active workflow runs cannot be archived: " + ", ".join(blocked)
        unknown = sorted(item for item in explicit if item not in records_by_id)
        if unknown:
            return "Error: workflow run records not found: " + ", ".join(unknown)
        # Resolve every exact target before moving the first directory. This
        # keeps a bad later ID from producing a partial archive operation.
        for run_id in requested:
            read_workflow_run_record(_runs_root(manager), run_id)
        if dry_run:
            return ToolOutput(
                f"Workflow archive preview: {len(requested)} candidate(s).",
                title="Workflow archive preview",
                metadata={
                    "workflow_archive_candidates": requested,
                    "dry_run": True,
                    "recoverable": True,
                },
            )
        archived = []
        for run_id in requested:
            destination = archive_workflow_run(_runs_root(manager), run_id)
            archived.append({"run_id": run_id, "trash_path": str(destination)})
        return ToolOutput(
            f"Archived {len(archived)} workflow run(s) to recoverable trash.",
            title="Workflow runs archived",
            metadata={"archived_workflow_runs": archived, "recoverable": True},
        )
    except Exception as exc:
        return format_public_error(exc)


def workflow_run_rename(run_id: str, display_name: str) -> str:
    """Persist a printable alias for one terminal run without changing identity."""
    try:
        from nz_coder.runtime.agent.agent_manager import _current_manager

        manager = _current_manager()
        name = validate_workflow_display_name(display_name)
        record = read_workflow_run_record(_runs_root(manager), run_id)
        record["display_name"] = name
        WorkflowRunStore(Path(_runs_root(manager)) / run_id).write_terminal(record)
        manager.record_workflow_event(
            "workflow_run_renamed",
            data={"run_id": run_id, "display_name": name},
        )
        return ToolOutput(
            f"Workflow run renamed to {name}.",
            title="Workflow run renamed",
            metadata={"run_id": run_id, "display_name": name},
        )
    except Exception as exc:
        return format_public_error(exc)


register(
    name="workflow_runs",
    description="List or read persisted workflow run records and JSON artifacts.",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "read", "artifact", "result"]},
            "run_id": {"type": "string"},
            "artifact": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["action"],
    },
    handler=workflow_runs,
    execution="read",
)

register(
    name="workflow_run_archive",
    description="Move exact terminal workflow runs into recoverable private trash.",
    parameters={
        "type": "object",
        "properties": {
            "run_ids": {"type": "array", "items": {"type": "string"}},
            "confirm": {"type": "boolean"},
            "keep": {"type": "integer"},
            "older_than_days": {"type": "integer"},
            "dry_run": {"type": "boolean"},
        },
        "required": ["run_ids", "confirm"],
    },
    handler=workflow_run_archive,
    execution="write",
    side_effect="mutates-state",
)

register(
    name="workflow_run_rename",
    description="Assign a printable display alias to one terminal workflow run.",
    parameters={
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "display_name": {"type": "string"},
        },
        "required": ["run_id", "display_name"],
    },
    handler=workflow_run_rename,
    execution="write",
    side_effect="mutates-state",
)
