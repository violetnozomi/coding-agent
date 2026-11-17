"""Tool surface for workspace-scoped persistent processes."""
from __future__ import annotations

import json

from nz_coder.foundation import config
from nz_coder.protocol.public_error import format_public_error
from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.runtime.core.execution_context import strict_local_tools
from nz_coder.runtime.process.process_service import (
    ProcessService,
    ProcessNotFoundError,
    ProcessOwnershipError,
    ProcessStateError,
    workspace_process_service,
)
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.runtime.process.command_channel import current_command_client
from nz_coder.protocol.session_events import current_session_event_bus
from nz_coder.state.sessions import active_session_id
from nz_coder.tools import ToolOutput, current_tool_cancel_event, register
from nz_coder.tools.bash import _command_title, _resolve_bash_workdir
from nz_coder.runtime.verification.domain.models import ExecutionReceipt


_OPERATIONS = frozenset({
    "start", "read", "write", "status", "list", "resize", "kill",
})


def _owner() -> tuple[str, str, object | None]:
    bus = current_session_event_bus()
    session_id = active_session_id() or getattr(bus, "session_id", "") or "direct-tool"
    agent_id = getattr(bus, "agent_id", "") if bus is not None else ""
    return str(session_id), str(agent_id), bus


def _json_output(payload: dict, *, title: str, metadata: dict | None = None, execution_facts: tuple = ()) -> ToolOutput:
    return ToolOutput(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        title=title,
        metadata=dict(metadata or {}),
        execution_facts=execution_facts,
    )


def run_process(
    operation: str,
    command: str | None = None,
    process_id: str | None = None,
    data: str | None = None,
    cursor: int | None = None,
    tail_bytes: int | None = None,
    max_bytes: int | None = None,
    wait_seconds: float = 0.0,
    workdir: str | None = None,
    tty: bool = True,
    rows: int = 24,
    cols: int = 80,
    append_newline: bool = False,
) -> str:
    arguments = dict(locals())
    client = current_command_client()
    if client is not None:
        return client.call("process", arguments, cancel_event=current_tool_cancel_event())
    workspace = current_workdir().resolve()
    from nz_coder.runtime.adapters.verification_runtime import current_tool_verification
    start = current_tool_verification()
    session_id, agent_id, event_bus = _owner()
    return execute_process_local(
        arguments, service=workspace_process_service(workspace),
        owner_session_id=session_id, owner_agent_id=agent_id, event_bus=event_bus,
        launch_id=start.execution_id if start is not None else "",
    )


def execute_process_local(
    arguments: dict, *, service: ProcessService, owner_session_id: str,
    owner_agent_id: str, event_bus: object | None,
    launch_id: str = "",
) -> str:
    """Use the same tool rules with an explicit, trusted service and owner."""
    from nz_coder.tool_platform.command_policy import assess_shell_command
    from nz_coder.tool_platform.permissioning.command_authorization import (
        authorization_failure, command_authorization_present, command_is_authorized,
    )

    operation = arguments.get('operation', None)
    command = arguments.get('command', None)
    process_id = arguments.get('process_id', None)
    data = arguments.get('data', None)
    cursor = arguments.get('cursor', None)
    tail_bytes = arguments.get('tail_bytes', None)
    max_bytes = arguments.get('max_bytes', None)
    wait_seconds = arguments.get('wait_seconds', 0.0)
    workdir = arguments.get('workdir', None)
    tty = arguments.get('tty', True)
    rows = arguments.get('rows', 24)
    cols = arguments.get('cols', 80)
    append_newline = arguments.get('append_newline', False)
    session_id, agent_id = owner_session_id, owner_agent_id
    workspace = service.workspace
    selected = str(operation or "").strip().lower()
    if selected not in _OPERATIONS:
        return "Error: operation must be start, read, write, status, list, resize, or kill"
    if selected == "list":
        selected = "status"
    try:
        if selected == "start":
            selected_command = str(command or "").strip()
            if not selected_command:
                return "Error: command is required for process start"
            if strict_local_tools():
                from nz_coder.swebench.policy import (
                    strict_bash_guidance,
                    strict_bash_violation,
                )

                violation = strict_bash_violation(selected_command)
                if violation:
                    return (
                        f"Error: {violation}. "
                        f"{strict_bash_guidance(selected_command, violation)}"
                    )
            resolved, error = _resolve_bash_workdir(workdir, workspace=workspace)
            if error:
                return error
            assert resolved is not None
            assessment = assess_shell_command(
                selected_command, workspace, command_cwd=resolved,
                allow_package_installs=current_run_settings().allow_package_installs,
            )
            if assessment.rejection:
                return f"Error: {assessment.rejection}"
            if (assessment.requires_approval or command_authorization_present()) and not command_is_authorized(
                "process", arguments, workspace=workspace, session_id=session_id,
            ):
                return authorization_failure("Denied: Command authorization is missing or no longer matches this invocation.")
            handle = service.start(
                selected_command,
                cwd=resolved,
                owner_session_id=session_id,
                owner_agent_id=agent_id,
                tty=bool(tty),
                rows=rows,
                cols=cols,
                event_bus=event_bus,
                launch_id=launch_id,
            )
            if handle.status == "failed":
                failure = service.read(
                    handle.process_id,
                    owner_session_id=session_id,
                    event_bus=event_bus,
                )
                return f"Error: {failure.output.strip() or 'persistent process failed to start'}"
            payload = {
                "operation": "start",
                "process": handle.to_dict(),
                "next": (
                    "Use process(operation='read', process_id=..., cursor=0, "
                    "wait_seconds=...) to wait for readiness. Use only this "
                    "returned process_id; caller-provided aliases are ignored."
                ),
            }
            return _json_output(
                payload,
                title=_command_title(selected_command),
                metadata={"process_id": handle.process_id, "status": handle.status},
                execution_facts=(ExecutionReceipt("process", handle.command, handle.cwd, "started",
                                                   execution_id=handle.process_id),),
            )

        if selected == "status" and not str(process_id or "").strip():
            processes = [
                item.to_dict()
                for item in service.list(owner_session_id=session_id)
            ]
            return _json_output(
                {"operation": "status", "processes": processes},
                title=f"{len(processes)} persistent processes",
                metadata={"process_count": len(processes)},
            )

        selected_id = str(process_id or "").strip()
        if not selected_id:
            return f"Error: process_id is required for process {selected}"

        if selected == "read":
            result = service.read(
                selected_id,
                owner_session_id=session_id,
                cursor=cursor,
                tail_bytes=tail_bytes,
                max_bytes=max_bytes,
                wait_seconds=wait_seconds,
                cancel_event=current_tool_cancel_event(),
                event_bus=event_bus,
            )
            payload = {"operation": "read", **result.to_dict()}
            return _json_output(
                payload,
                title=f"Read {selected_id}",
                metadata={
                    "process_id": selected_id,
                    "status": result.status,
                    "cursor": result.next_cursor,
                    "buffer_bytes": result.buffer_end_cursor - result.buffer_start_cursor,
                    "truncated": result.truncated_before_cursor or result.has_more,
                    "cancelled": result.cancelled,
                },
            )

        if selected == "write":
            if data is None:
                return "Error: data is required for process write"
            if not command_is_authorized("process", arguments, workspace=workspace, session_id=session_id):
                return authorization_failure("Denied: Command authorization required for process stdin write.")
            payload_data = str(data) + ("\n" if append_newline else "")
            handle = service.write(
                selected_id,
                payload_data,
                owner_session_id=session_id,
                event_bus=event_bus,
            )
            return _json_output(
                {
                    "operation": "write",
                    "process": handle.to_dict(),
                    "bytes_written": len(payload_data.encode("utf-8")),
                },
                title=f"Write {selected_id}",
                metadata={"process_id": selected_id, "status": handle.status},
            )

        if selected == "resize":
            handle = service.resize(
                selected_id,
                rows=rows,
                cols=cols,
                owner_session_id=session_id,
                event_bus=event_bus,
            )
            return _json_output(
                {
                    "operation": "resize",
                    "process": handle.to_dict(),
                    "rows": rows,
                    "cols": cols,
                },
                title=f"Resize {selected_id}",
                metadata={"process_id": selected_id, "status": handle.status},
            )

        if selected == "kill":
            handle = service.kill(
                selected_id,
                owner_session_id=session_id,
                event_bus=event_bus,
            )
            return _json_output(
                {"operation": "kill", "process": handle.to_dict()},
                title=f"Stop requested: {selected_id}",
                metadata={"process_id": selected_id, "status": handle.status},
            )

        handle = service.get(
            selected_id,
            owner_session_id=session_id,
            event_bus=event_bus,
        )
        return _json_output(
            {"operation": "status", "process": handle.to_dict()},
            title=f"Status {selected_id}",
            metadata={"process_id": selected_id, "status": handle.status},
        )
    except (ValueError, ProcessNotFoundError, ProcessOwnershipError, ProcessStateError) as exc:
        return format_public_error(exc)


register(
    name="process",
    description=(
        "Start and control a persistent long-running process across Agent turns. "
        "Use this, not bash, for dev servers, watch mode, REPLs, and live logs. "
        "Operations: start, read by byte cursor, write stdin, status/list, resize, kill. "
        "Kill requests a stop; inspect process.status for completion. "
        "A running/starting result means resource cleanup is still pending."
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["start", "read", "write", "status", "list", "resize", "kill"],
            },
            "command": {"type": "string", "description": "Command for start."},
            "process_id": {
                "type": "string",
                "description": "Exact stable proc_* ID returned by start. Never invent an alias. Omit for status/list of all Session processes.",
            },
            "data": {"type": "string", "description": "UTF-8 stdin data for write."},
            "append_newline": {
                "type": "boolean",
                "description": "Append a newline to write data.",
            },
            "cursor": {
                "type": "integer",
                "description": "Read from this byte cursor; use next_cursor on later reads. -1 starts at current end.",
            },
            "tail_bytes": {
                "type": "integer",
                "description": "When cursor is omitted, read only this many tail bytes.",
            },
            "max_bytes": {
                "type": "integer",
                "description": f"Maximum bytes returned, up to {config.PROCESS_READ_MAX_BYTES}.",
            },
            "wait_seconds": {
                "type": "number",
                "description": "Wait up to 30 seconds for new output without killing the process.",
            },
            "workdir": {
                "type": "string",
                "description": "Workspace-relative cwd for start.",
            },
            "tty": {
                "type": "boolean",
                "description": "Request a PTY on POSIX; Windows uses persistent pipes.",
            },
            "rows": {"type": "integer", "description": "PTY rows for start/resize."},
            "cols": {"type": "integer", "description": "PTY columns for start/resize."},
        },
        "required": ["operation"],
    },
    handler=run_process,
    execution="serial",
    side_effect="mutates-shell",
)
